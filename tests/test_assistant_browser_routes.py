"""Human browser settings never admit assistant principals or reflect secrets.

Browser v2 (WI-4) re-keyed the credential PUT by site and account label and
retired the Phase 6 task routes (410); the v2 cases live in
test_assistant_browser_routes_v2.py."""
import base64
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.deps import get_current_user, require_writer
from app.routers import assistant_browser as routes
from app.services import assistant_browser as service
from app.services import assistant_browser_credentials as credentials
from app.services import assistant_browser_vault as vault


@pytest.fixture
def client(monkeypatch):
    user = SimpleNamespace(id=str(uuid4()), via_assistant_run_id=None)
    config = SimpleNamespace(assistant_browser_enabled=True, assistant_browser_v2_enabled=True,
                             procore_login_email='', user_cred_kek=base64.b64encode(b'a'*32).decode(),
                             user_cred_kek_id='u1', user_cred_old_keys='')
    monkeypatch.setattr(routes, 'get_settings', lambda: config)
    monkeypatch.setattr(service, 'get_settings', lambda: config)
    monkeypatch.setattr(vault, 'get_settings', lambda: config)
    monkeypatch.setattr(credentials, 'get_settings', lambda: config)
    monkeypatch.setattr(routes, '_require_assistant', lambda user: None)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_writer] = lambda: user
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app), user, config


@pytest.mark.parametrize('body', [None, ['SENTINEL_SECRET'], {'password': 'SENTINEL_SECRET'},
    {'password': {'nested': 'SENTINEL_SECRET'}}, 'SENTINEL_SECRET'])
def test_malformed_secret_body_is_never_reflected(client, body):
    http, _, _ = client
    result = http.put('/assistant/browser-credentials/procore', json=body)
    assert result.status_code == 422
    assert 'SENTINEL_SECRET' not in result.text
    assert result.headers['cache-control'] == 'no-store'


def test_save_returns_metadata_and_encrypts_password(client, monkeypatch):
    http, user, _ = client
    captured = {}
    monkeypatch.setattr(credentials.repo, 'browser_site', lambda *a: {
        'site_key': 'procore', 'label': 'Procore', 'kind': 'curated', 'enabled': True,
        'login_origins': ['https://login.procore.com']})
    monkeypatch.setattr(credentials.repo, 'browser_credential_v2', lambda *a, **k: None)
    monkeypatch.setattr(credentials.repo, 'browser_credentials', lambda *a: [])
    monkeypatch.setattr(credentials.repo, 'browser_credentials_v2_all', lambda *a: [])
    def put(owner, site, label, expected, row, request_id=None):
        captured.update(row, user_id=owner, version=1, status='active', site_key=site, account_label=label)
        return {'ok': True, 'credential': captured}
    monkeypatch.setattr(credentials.repo, 'browser_credential_put_v2', put)
    result = http.put('/assistant/browser-credentials/procore', json={
        'account_label':'My account', 'username':'user@example.test', 'password':'SENTINEL_SECRET',
        'acknowledge_personal_password':True, 'expected_version':0,
    })
    assert result.status_code == 200
    assert 'SENTINEL_SECRET' not in result.text
    assert not {'ciphertext','wrapped_dek','nonce','key_id','password','username'} & result.json()['credential'].keys()
    assert vault.decrypt_credential(captured) == 'SENTINEL_SECRET'
    assert captured['user_id'] == user.id


def test_stale_replacement_never_writes(client, monkeypatch):
    http, _, _ = client
    monkeypatch.setattr(credentials.repo, 'browser_site', lambda *a: {
        'site_key': 'procore', 'label': 'Procore', 'kind': 'curated', 'enabled': True,
        'login_origins': ['https://login.procore.com']})
    monkeypatch.setattr(credentials.repo, 'browser_credential_v2', lambda *a, **k: {'version':2,'id':str(uuid4())})
    monkeypatch.setattr(credentials.repo, 'browser_credential_put_v2', lambda *a: pytest.fail('must not write'))
    result = http.put('/assistant/browser-credentials/procore', json={
        'account_label':'My account','username':'u','password':'secret','acknowledge_personal_password':True,
        'expected_version':1})
    assert result.status_code == 409


def test_assistant_cannot_manage_credentials(client):
    http, user, _ = client
    user.via_assistant_run_id = str(uuid4())
    assert http.get('/assistant/browser-credentials').status_code == 403


def test_task_owner_is_bound_and_content_omitted(client, monkeypatch):
    from app.services import assistant_browser_records
    http, user, _ = client
    job_id = str(uuid4())
    calls = []
    def get(owner, job):
        calls.append((owner,job))
        return {'id':job,'input':{'service':'procore'},'checkpoint':{'secret':'secret'},'result':'private'}
    monkeypatch.setattr(service.repo, 'browser_job', get)
    monkeypatch.setattr(assistant_browser_records, 'latest_result', lambda *a: None)
    monkeypatch.setattr(service.repo, 'browser_help_state', lambda *a: None)
    result = http.get(f'/assistant/browser-tasks/{job_id}')
    assert result.status_code == 200
    assert calls == [(user.id,job_id)]
    assert result.json()['result'] is None
    assert 'private' not in result.text and 'secret' not in result.text


def test_human_cannot_enqueue_without_approved_assistant_principal(client, monkeypatch):
    http, _, _ = client
    result = http.post('/assistant/browser-tasks', json={'principal_kind':'org'})
    assert result.status_code == 410  # retired by browser v2; tasks start through /browser-requests
    monkeypatch.setattr(routes.repo, 'browser_site_grants', lambda *a, **k: [])
    monkeypatch.setattr(routes.repo, 'browser_sites', lambda *a, **k: [])
    sites = http.get('/assistant/browser-sites').json()
    assert sites['enabled'] is True


def test_metadata_handles_malformed_persisted_input():
    assert service.task_metadata({'id':'1','input':['bad']})['url'] is None

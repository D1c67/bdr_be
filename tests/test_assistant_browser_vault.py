"""Personal secret envelopes must reject ownership/generation/purpose substitution."""
import base64
from types import SimpleNamespace

import pytest

from app.services import assistant_browser_sites as sites
from app.services import assistant_browser_vault as vault


@pytest.fixture
def keys(monkeypatch):
    config = SimpleNamespace(user_cred_kek=base64.b64encode(b'a' * 32).decode(),
                             user_cred_kek_id='u1', user_cred_old_keys='')
    monkeypatch.setattr(vault, 'get_settings', lambda: config)
    return config


def test_envelope_is_random_and_round_trips(keys):
    a = vault.encrypt_payload('owner', 'credential', 1, 'credential', b'private password')
    b = vault.encrypt_payload('owner', 'credential', 1, 'credential', b'private password')
    assert a != b
    assert 'private password' not in str(a)
    assert vault.decrypt_payload('owner', 'credential', 1, 'credential', a) == b'private password'


@pytest.mark.parametrize('owner,object_id,version,purpose', [
    ('other', 'credential', 1, 'credential'), ('owner', 'other', 1, 'credential'),
    ('owner', 'credential', 2, 'credential'), ('owner', 'credential', 1, 'session'),
])
def test_envelope_substitution_fails(keys, owner, object_id, version, purpose):
    envelope = vault.encrypt_payload('owner', 'credential', 1, 'credential', b'secret')
    with pytest.raises(vault.VaultUnavailable):
        vault.decrypt_payload(owner, object_id, version, purpose, envelope)


def test_rotation_requires_explicit_old_key(keys):
    envelope = vault.encrypt_payload('u', 'c', 1, 'credential', b'secret')
    old = keys.user_cred_kek
    keys.user_cred_kek_id = 'u2'
    keys.user_cred_kek = base64.b64encode(b'b' * 32).decode()
    with pytest.raises(vault.VaultUnavailable):
        vault.decrypt_payload('u', 'c', 1, 'credential', envelope)
    keys.user_cred_old_keys = f'u1:{old}'
    assert vault.decrypt_payload('u', 'c', 1, 'credential', envelope) == b'secret'


@pytest.mark.parametrize('url', [
    'https://app.procore.com.evil.test/1/project/home',
    'https://app.procore.com@evil.test/1/project/home',
    'https://app.procore.com:443/1/project/home',
    'https://app.procore.com/1/project/home?redirect=https://evil.test',
    'https://app.procore.com/1/project/home#fragment',
    'https://app.procore.com/1/project/rfi/show/../submit',
    'http://app.procore.com/1/project/home',
    'https://login.microsoftonline.com/common',
    'https://app.procore.com/1/project/home\n',
])
def test_exact_read_ceiling_rejects_unsafe_destinations(url):
    with pytest.raises(ValueError):
        sites.validate_url('procore', url)


def test_fixed_read_ceiling_accepts_rfi():
    url = 'https://app.procore.com/123/project/rfi/show/456'
    assert sites.validate_url('procore', url) == url
    with pytest.raises(ValueError):
        sites.site('buildingconnected')


def test_missing_key_never_falls_back(keys):
    keys.user_cred_kek = ''
    with pytest.raises(vault.VaultUnavailable):
        vault.encrypt_payload('u', 'c', 1, 'credential', b'secret')


def test_personal_key_must_differ_from_org_keys():
    from app.core.config import Settings
    key = base64.b64encode(b'a' * 32).decode()
    settings = Settings.model_construct(assistant_browser_enabled=True, assistant_enabled=True,
        browser_agents_enabled=True, user_cred_kek=key, browser_agent_secrets_key=key)
    with pytest.raises(ValueError, match='must differ'):
        settings._validate_personal_browser()


def test_personal_browser_flag_requires_both_platforms():
    from app.core.config import Settings
    settings = Settings.model_construct(assistant_browser_enabled=True, assistant_enabled=True,
                                        browser_agents_enabled=False)
    with pytest.raises(ValueError, match='needs assistant and browser'):
        settings._validate_personal_browser()


def test_projecthome_read_ceiling_is_exact():
    value='https://app.procore.com/webclients/host/companies/5/projects/123/tools/projecthome'
    assert sites.validate_url('procore',value)==value
    for suffix in ('/edit','/settings','/rfis'):
        with pytest.raises(ValueError):
            sites.validate_url('procore',value+suffix)

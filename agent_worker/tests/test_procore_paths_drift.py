"""The worker's vendored Procore paths must match app/services/procore_client.py
(spec 10.2 "The paths.py drift test"). The backend module is loaded by file
path; it imports only stdlib and httpx."""

import importlib.util
import sys
from pathlib import Path

import pytest

from bdr_agent.agents.pm_apps.procore import paths

BACKEND = Path(__file__).resolve().parents[2] / "app" / "services" / "procore_client.py"


@pytest.fixture(scope="module")
def backend():
    if not BACKEND.exists():
        pytest.skip("backend source not present")
    spec = importlib.util.spec_from_file_location("_bdr_procore_client_for_drift", BACKEND)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


def _patterns(items):
    return [p.pattern for p in items]


def test_rfi_allowed_paths_match(backend):
    assert _patterns(paths.RFI_ALLOWED_PATHS) == _patterns(backend.RFI_ALLOWED_PATHS)


def test_login_login_paths_match(backend):
    assert _patterns(paths.LOGIN_LOGIN_PATHS) == _patterns(backend._LOGIN_LOGIN_PATHS)


def test_login_app_paths_are_a_subset(backend):
    assert set(_patterns(paths.LOGIN_APP_PATHS)) <= set(_patterns(backend._LOGIN_APP_PATHS))


def test_project_home_and_hosts_match(backend):
    assert paths.PROJECT_HOME_RE.pattern == backend._PROJECT_HOME_RE.pattern
    assert paths.WEBCLIENT_RE.pattern == backend._WEBCLIENT_RE.pattern
    assert paths.COMPANY_PROJECT_RE.pattern == backend._COMPANY_PROJECT_RE.pattern
    assert paths.SESSION_COOKIE == backend._SESSION_COOKIE
    assert paths.MAX_JSON_BYTES == backend._MAX_JSON_BYTES
    from bdr_agent import protocol
    assert tuple(protocol.PROCORE_HOSTS) == tuple(backend.APP_HOSTS)
    assert protocol.PROCORE_LOGIN_HOST == backend.LOGIN_HOST


def test_rest_allowlist_refuses_writes_and_other_paths():
    assert paths.rest_path_allowed("/rest/v1.0/projects/3167322/rfis/27914290")
    assert not paths.rest_path_allowed("/rest/v1.0/projects/3167322/rfis/27914290/replies")
    assert not paths.rest_path_allowed("/rest/v1.0/companies/8089/bids/1")
    assert not paths.rest_path_allowed("/rest/v1.1/projects/3167322/rfis")

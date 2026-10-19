"""The guard's pure policy (spec 4.3, D12): no browser needed."""

import pytest

from bdr_agent.agents import ceiling_for
from bdr_agent.browser.guard import (
    check_login_navigation,
    check_navigation,
    check_request,
    host_matches,
    token_query_keys,
)

RFI = ceiling_for("rfi_from_email")
CANARY = ceiling_for("canary", "bdr.test")


@pytest.mark.parametrize("host,pattern,ok", [
    ("app.procore.com", "app.procore.com", True),
    ("APP.procore.com.", "app.procore.com", True),
    ("cdn.procore.com", "*.procore.com", True),
    ("a.b.procore.com", "*.procore.com", True),
    ("procore.com", "*.procore.com", False),
    ("evilprocore.com", "*.procore.com", False),
    ("procore.com.evil.example", "*.procore.com", False),
    ("", "*.procore.com", False),
])
def test_host_matches(host, pattern, ok):
    assert host_matches(host, pattern) is ok


def test_token_query_keys():
    assert token_query_keys("https://x/y?token=1&page=2") == ["token"]
    assert set(token_query_keys("https://x/y?X-Amz-Signature=a&sig=b&session_id=c&passport=d")) == {
        "X-Amz-Signature", "sig", "session_id", "passport"}
    assert token_query_keys("https://x/y?company_id=8089&page=2") == []


@pytest.mark.parametrize("url,reason", [
    ("https://app.procore.com/3167322/project/home", None),
    ("https://us02.procore.com/3167322/project/rfi/show/27914290", None),
    ("https://app.procore.com/3167322/project/rfi/list", None),
    ("https://app.procore.com/webclients/host/companies/8089/projects/3167322/tools/rfis/27914290", None),
    ("about:blank", None),
    ("http://app.procore.com/3167322/project/home", "scheme_not_allowed"),
    ("https://evil.example/3167322/project/home", "host_not_allowed"),
    ("https://login.procore.com/", "host_not_allowed"),
    ("https://app.procore.com/3167322/project/rfi/show/1/edit", "path_not_allowed"),
    ("https://app.procore.com/3167322/company/planroom", "path_not_allowed"),
    ("https://app.procore.com/3167322/project/home?token=abc", "token_query"),
    ("https://app.procore.com/3167322/project/home?auth_code=1", "token_query"),
    ("https://user:pw@app.procore.com/3167322/project/home", "credentials_in_url"),
    ("https://app.procore.com:8443/3167322/project/home", "port_not_allowed"),
    ("file:///etc/passwd", "scheme_not_allowed"),
    ("chrome://settings", "scheme_not_allowed"),
])
def test_model_navigation(url, reason):
    v = check_navigation(RFI, url)
    assert v.ok is (reason is None) and v.reason == reason


def test_host_narrowing_only_shrinks():
    narrowed = RFI.narrowed(["us02.procore.com", "evil.example"])
    assert narrowed.nav_hosts == ("us02.procore.com",)
    assert not check_navigation(narrowed, "https://app.procore.com/1/project/home")
    assert not check_navigation(narrowed, "https://evil.example/1/project/home")
    assert check_navigation(narrowed, "https://us02.procore.com/1/project/home")
    assert RFI.narrowed(None) is RFI


def test_canary_ceiling_is_its_page_only():
    assert check_navigation(CANARY, "https://bdr.test/agent-worker/v1/canary/page")
    assert not check_navigation(CANARY, "https://bdr.test/agent-worker/v1/claim")
    assert not check_navigation(CANARY, "https://app.procore.com/1/project/home")


@pytest.mark.parametrize("method,url,login,main,ok", [
    # Model page: reads only.
    ("GET", "https://app.procore.com/assets/app.js", False, False, True),
    ("GET", "https://cdn.procorecdn.com/x.css", False, False, True),
    ("HEAD", "https://app.procore.com/x", False, False, True),
    ("POST", "https://app.procore.com/rest/v1.0/telemetry", False, False, False),
    ("PUT", "https://app.procore.com/rest/v1.0/projects/1/rfis/2", False, False, False),
    ("DELETE", "https://app.procore.com/rest/v1.0/projects/1/rfis/2", False, False, False),
    ("PATCH", "https://app.procore.com/rest/v1.0/projects/1/rfis/2", False, False, False),
    ("POST", "https://login.procore.com/sessions/submit_login_password", False, False, False),
    ("GET", "https://www.google.com/recaptcha/api.js", False, False, False),
    ("GET", "https://evil.example/pixel.gif", False, False, False),
    ("GET", "https://storage.procore.com/file?sig=abc", False, False, False),
    ("GET", "https://app.procore.com/1/project/rfi/show/2/edit", False, True, False),
    ("GET", "data:image/png;base64,AAAA", False, False, True),
    # Login page object: the login chain may POST, nothing else.
    ("POST", "https://login.procore.com/sessions/submit_login_email", True, True, True),
    ("POST", "https://login.procore.com/sessions/submit_login_password", True, True, True),
    ("POST", "https://login.procore.com/account/delete", True, True, False),
    ("POST", "https://app.procore.com/auth/procore", True, True, False),
    ("GET", "https://app.procore.com/auth/procore/callback?code=abc&state=s", True, True, True),
    ("GET", "https://login.procore.com/oauth/authorize?client_id=x&response_type=code", True, True, True),
    ("GET", "https://login.procore.com/settings", True, True, False),
    ("POST", "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/x", True, False, True),
    ("GET", "https://www.gstatic.com/recaptcha/x.js", True, False, True),
    ("POST", "https://app.procore.com/rest/v1.0/telemetry", True, False, False),
    ("GET", "https://evil.example/x", True, False, False),
])
def test_request_rule(method, url, login, main, ok):
    assert bool(check_request(RFI, method, url, from_login_page=login, main_frame_navigation=main)) is ok


def test_login_navigation_rule():
    assert check_login_navigation(RFI, "https://login.procore.com/login/password")
    assert check_login_navigation(RFI, "https://app.procore.com/auth/procore")
    assert not check_login_navigation(RFI, "https://login.procore.com/sso/start")
    assert not check_login_navigation(RFI, "https://sso.example.com/saml/login")

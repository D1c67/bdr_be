import socket

import pytest

from app.services import assistant_web as web


@pytest.mark.parametrize("url", ["http://example.com", "https://localhost", "https://127.0.0.1", "https://[::1]", "https://169.254.169.254/latest", "https://10.0.0.1", "https://user:pass@example.com", "https://example.com:444", "https://metadata.google.internal", "https://x.local"])
def test_private_and_unsafe_fetch_destinations_refused(url):
    with pytest.raises(web.WebUnavailable):
        web.public_url(url, resolve=False)


def test_mixed_dns_private_address_refused(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, ("93.184.216.34", 443)), (None, None, None, None, ("10.0.0.1", 443))])
    with pytest.raises(web.WebUnavailable):
        web.public_url("https://example.com")


def test_unbound_or_quoted_link_never_reaches_provider(monkeypatch):
    calls = []
    monkeypatch.setattr(web, "_request", lambda *a: calls.append(a))
    for texts in [[], ["> https://example.com"], ["```\nhttps://example.com\n```"], ["From: other\nhttps://example.com"]]:
        with pytest.raises(web.WebUnavailable):
            web.fetch("https://example.com", user_texts=texts)
    assert calls == []


def test_redirect_and_missing_provider_attestation_refused(monkeypatch):
    url = "https://example.com/path"
    monkeypatch.setattr(web, "public_url", lambda u: u)
    for answer in [{"url": "https://other.com", "redirects": [], "public_destination": True, "text": "private"},
                   {"url": url, "redirects": ["https://other.com"], "public_destination": True},
                   {"url": url, "redirects": [], "text": "missing attestation"}]:
        monkeypatch.setattr(web, "_request", lambda *a: answer)
        with pytest.raises(web.WebUnavailable):
            web.fetch(url, user_texts=[url])


def test_bound_fetch_is_bounded_and_cited(monkeypatch):
    url = "https://example.com/path"
    monkeypatch.setattr(web, "public_url", lambda u: u)
    monkeypatch.setattr(web, "_request", lambda *a: {"url": url, "redirects": [], "public_destination": True, "text": "x" * 20000})
    out = web.fetch(url, user_texts=[url])
    assert len(out["text"]) == 12000 and out["truncated"] and out["tainted"] and out["citation"] == url


@pytest.mark.parametrize("url", ["https://224.0.0.1", "https://239.255.255.250", "https://[ff02::1]", "https://[ff0e::1]"])
def test_multicast_is_not_a_public_web_destination(url):
    with pytest.raises(web.WebUnavailable):
        web.public_url(url, resolve=False)


def test_search_requires_exact_typed_provenance_and_valid_limit(monkeypatch):
    calls = []
    monkeypatch.setattr(web, "_request", lambda *a: calls.append(a))
    for query, limit, messages in [("private bid", 5, ["search weather"]),
                                  ("private bid", 5, ["> private bid"]),
                                  ("weather", True, ["weather"]),
                                  ("weather", 11, ["weather"]),
                                  ("weather\nsecret", 5, ["weather secret"])]:
        with pytest.raises(web.WebUnavailable):
            web.search(query, limit, user_texts=messages)
    assert calls == []


def test_gateway_transport_is_only_configured_endpoint_with_no_redirects(monkeypatch):
    import httpx
    import json
    from types import SimpleNamespace
    settings = SimpleNamespace(assistant_enabled=True, assistant_web_enabled=True,
        assistant_web_api_key="fake-key", assistant_web_api_url="https://gateway.example.test")
    monkeypatch.setattr(web, "get_settings", lambda: settings)
    monkeypatch.setattr(web, "public_url", lambda value: value)
    observed = []
    original = httpx.Client
    def handler(request):
        observed.append(request)
        return httpx.Response(200, json={"policy": web.POLICY, "results": []})
    options = []
    def client(**kwargs):
        options.append(kwargs)
        return original(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(web.httpx, "Client", client)
    assert web._request("fetch", {"url": "https://target.example.test"})["policy"] == web.POLICY
    assert str(observed[0].url) == "https://gateway.example.test/fetch"
    assert observed[0].headers["authorization"] == "Bearer fake-key"
    assert json.loads(observed[0].content)["url"] == "https://target.example.test"
    assert options[0]["follow_redirects"] is False and options[0]["trust_env"] is False


@pytest.mark.parametrize("status,body", [(302, b"provider secret"), (500, b"provider secret"),
    (200, b"not json"), (200, b'{"policy":"different"}'), (200, b"x" * (web.MAX_BYTES + 1))])
def test_gateway_bad_status_schema_or_oversize_is_sanitized(monkeypatch, status, body):
    import httpx
    from types import SimpleNamespace
    monkeypatch.setattr(web, "get_settings", lambda: SimpleNamespace(
        assistant_enabled=True, assistant_web_enabled=True, assistant_web_api_key="key",
        assistant_web_api_url="https://gateway.example.test"))
    monkeypatch.setattr(web, "public_url", lambda value: value)
    original = httpx.Client
    monkeypatch.setattr(web.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda req: httpx.Response(status, content=body)), **kwargs))
    with pytest.raises(web.WebUnavailable) as error:
        web._request("search", {"query": "weather"})
    assert str(error.value) == "Web access is unavailable."


def test_unconfigured_web_never_opens_a_socket(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(web, "get_settings", lambda: SimpleNamespace(assistant_enabled=False))
    monkeypatch.setattr(web.httpx, "Client", lambda **kwargs: pytest.fail("Disabled provider must not request"))
    with pytest.raises(web.WebUnavailable):
        web._request("fetch", {"url": "https://example.com"})

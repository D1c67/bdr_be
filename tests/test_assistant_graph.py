"""Phase 5 transport boundaries, with no network or Microsoft credentials."""

import httpx
import pytest

from app.services.assistant_graph import GraphFailure, GraphSendUncertain, GraphTransport, graph_url


@pytest.mark.parametrize("path", [
    "http://graph.microsoft.com/v1.0/me", "https://evil.test/v1.0/me",
    "https://graph.microsoft.com.evil.test/v1.0/me",
    "https://user@graph.microsoft.com/v1.0/me", "//evil.test/v1.0/me",
    "https://graph.microsoft.com:443/v1.0/me", "/beta/me", "/v1.0/../beta/me",
    "/v1.0/%2e%2e/beta/me", "/v1.0/me#secret", "/v1.0/me\n",
    "/v1.0/me\\evil", "file:///v1.0/me",
])
def test_forbidden_resource_before_request(path):
    with pytest.raises(ValueError):
        graph_url(path)


def test_origin_pinned_pagination_and_token():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"value": []})

    graph = GraphTransport("fake-token", transport=httpx.MockTransport(handler))
    url = "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=abc%2Bdef"
    assert graph.read_json(url, prefer='outlook.body-content-type="text"') == {"value": []}
    assert str(requests[0].url) == url
    assert requests[0].headers["authorization"] == "Bearer fake-token"
    assert "fake-token" not in repr(graph)


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500, 503])
def test_no_redirects_retries_or_provider_error_leak(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="secret mailbox and token", headers={
            "Location": "https://evil.test", "Retry-After": "0"})

    graph = GraphTransport("fake-token", transport=httpx.MockTransport(handler))
    with pytest.raises(GraphFailure) as error:
        graph.read_json("/v1.0/me")
    assert str(error.value) == "Outlook request failed."
    assert error.value.status_code == status
    assert len(calls) == 1


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"null", b"\xff"])
def test_non_object_json_refused(body):
    graph = GraphTransport("token", sender_mailbox="owner@example.test", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=body)))
    with pytest.raises(GraphFailure):
        graph.read_json("/v1.0/me")


def test_response_limit():
    graph = GraphTransport("token", sender_mailbox="owner@example.test", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=b"x" * 101)))
    with pytest.raises(GraphFailure):
        graph.read_json("/v1.0/me", max_bytes=100)


def test_send_uses_server_bound_mailbox_and_is_accepted_once():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(202)

    GraphTransport("token", sender_mailbox="owner@example.test", transport=httpx.MockTransport(handler)).send_mail({
        "message": {}, "saveToSentItems": True})
    assert len(calls) == 1
    assert str(calls[0].url) == "https://graph.microsoft.com/v1.0/users/owner@example.test/sendMail"
    assert calls[0].method == "POST"


@pytest.mark.parametrize("status", [200, 204, 302, 400, 429, 503])
def test_send_nonaccepted_status_uncertain_no_retry(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status)

    graph = GraphTransport("token", sender_mailbox="owner@example.test", transport=httpx.MockTransport(handler))
    with pytest.raises(GraphSendUncertain):
        graph.send_mail({"message": {}, "saveToSentItems": True})
    assert len(calls) == 1


def test_timeout_during_send_is_uncertain_and_sanitized():
    def handler(_request):
        raise httpx.ReadTimeout("secret token")

    graph = GraphTransport("token", sender_mailbox="owner@example.test", transport=httpx.MockTransport(handler))
    with pytest.raises(GraphSendUncertain) as error:
        graph.send_mail({"message": {}, "saveToSentItems": True})
    assert "secret" not in str(error.value)
    assert error.value.__suppress_context__


def test_reader_send_and_missing_sent_record_refused_before_io():
    def handler(_request):
        pytest.fail("No network call allowed")

    transport = httpx.MockTransport(handler)
    with pytest.raises(ValueError):
        GraphTransport("token", sender_mailbox="owner@example.test", reader=True, transport=transport).send_mail({"saveToSentItems": True})
    with pytest.raises(ValueError):
        GraphTransport("token", sender_mailbox="owner@example.test", transport=transport).send_mail({})


@pytest.mark.parametrize("field", ["from", "sender", "From", "Sender"])
def test_payload_cannot_override_server_bound_sender(field):
    def handler(_request):
        pytest.fail("Sender override must fail before dispatch")

    graph = GraphTransport("token", sender_mailbox="owner@example.test",
                           transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="Sender must come"):
        graph.send_mail({"message": {field: {"emailAddress": {"address": "other@example.test"}}},
                         "saveToSentItems": True})


def test_application_send_requires_bound_mailbox():
    graph = GraphTransport("token", transport=httpx.MockTransport(
        lambda _: pytest.fail("No unbound sends")))
    with pytest.raises(ValueError, match="Server-bound sender required"):
        graph.send_mail({"message": {}, "saveToSentItems": True})

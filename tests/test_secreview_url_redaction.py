"""Security review: pre-authenticated upstream URLs never reach logs or RFP
error columns.

httpx quotes the full request URL, query string included, in
str(HTTPStatusError). Graph upload-session URLs carry a `tempauth` token in
that query string and share links carry access keys, so the upstream error
handlers in app.main and the RFP services' error columns pass exception text
through app.core.redact first.
"""

import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.redact import redact_text, redact_url
from app.main import app
from app.services import rfp_email_ingest, rfp_portal_ingest, rfp_split

SECRET = "SECRET123"
TOKEN_URL = f"https://tenant.sharepoint.com/_api/v2.0/drive/up?tempauth={SECRET}#frag"
_PATH_STATUS = "/_test/secreview-upstream-status"
_PATH_TRANSPORT = "/_test/secreview-upstream-transport"


def _status_error(url: str = TOKEN_URL) -> httpx.HTTPStatusError:
    req = httpx.Request("PUT", url)
    resp = httpx.Response(429, request=req)
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("raise_for_status did not raise")


def test_redact_url_drops_query_fragment_and_userinfo():
    assert redact_url(TOKEN_URL) == "https://tenant.sharepoint.com/_api/v2.0/drive/up"
    assert redact_url("https://bob:pw@host.example:8443/a/b?k=v") == "https://host.example:8443/a/b"
    assert redact_url("/relative/path?sig=abc") == "/relative/path"


def test_redact_text_rewrites_every_url_in_an_exception_message():
    exc = _status_error()
    assert SECRET in str(exc)  # httpx really does quote the query string
    text = redact_text(exc)
    assert SECRET not in text
    assert "tenant.sharepoint.com/_api/v2.0/drive/up" in text
    assert "429" in text
    assert redact_text("no url here") == "no url here"


@pytest.fixture()
def raising_routes():
    @app.get(_PATH_STATUS)
    def _boom_status():
        raise _status_error()

    @app.get(_PATH_TRANSPORT)
    def _boom_transport():
        raise httpx.ConnectError(f"connect failed for {TOKEN_URL}", request=httpx.Request("PUT", TOKEN_URL))

    yield
    app.router.routes[:] = [
        r for r in app.router.routes if getattr(r, "path", None) not in (_PATH_STATUS, _PATH_TRANSPORT)
    ]


@pytest.mark.parametrize("path", [_PATH_STATUS, _PATH_TRANSPORT])
def test_upstream_handlers_never_log_the_token(raising_routes, caplog, path):
    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="app.upstream"):
        r = client.get(path)
    assert r.status_code == 502
    records = [rec for rec in caplog.records if rec.name == "app.upstream"]
    assert records, "the upstream failure must still be logged"
    logged = "\n".join(
        rec.getMessage() + (logging.Formatter().formatException(rec.exc_info) if rec.exc_info else "")
        for rec in records
    )
    assert SECRET not in logged
    assert "tenant.sharepoint.com" in logged
    # The stack is still there for debugging.
    assert "Traceback" in logged


def test_email_retry_ladder_stores_redacted_last_error(monkeypatch):
    saved: dict = {}
    monkeypatch.setattr(rfp_email_ingest, "gate_busy", lambda exc: False)
    monkeypatch.setattr(rfp_email_ingest, "_cas", lambda sb, rid, status, fields, **kw: saved.update(fields))
    monkeypatch.setattr(rfp_email_ingest, "_record_retry", lambda *a, **kw: None)
    rfp_email_ingest._retry_or_fail(None, {"id": "e1", "attempts": 0}, "create", _status_error(), step="create")
    assert "last_error" in saved
    assert SECRET not in saved["last_error"]
    assert "tenant.sharepoint.com" in saved["last_error"]


def test_portal_error_texts_are_redacted():
    exc = rfp_portal_ingest.RfpPortalTransient(f"download failed: {TOKEN_URL}")
    assert SECRET not in rfp_portal_ingest._error(exc)
    assert SECRET not in rfp_portal_ingest.error_message(exc, "harvest")
    assert SECRET not in rfp_portal_ingest._error(_status_error())


def test_split_give_up_reason_is_redacted(monkeypatch):
    stored: list[dict] = []
    recorded: list[dict] = []

    def fake_cas(sb, harvest_id, expected, fields):
        stored.append(fields)
        return True

    monkeypatch.setattr(rfp_split, "_cas_split", fake_cas)
    monkeypatch.setattr(rfp_split, "_record", lambda *a, **kw: recorded.append(kw.get("detail") or {}))
    reason = rfp_split.give_up(None, "h1", str(_status_error()), attempts=4)
    assert SECRET not in reason
    assert stored and SECRET not in stored[0]["split_error"]
    assert recorded and SECRET not in str(recorded[0])

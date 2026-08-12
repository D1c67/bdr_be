"""Upstream-failure exception handlers in app.main.

An unhandled exception is turned into a 500 by Starlette's OUTERMOST
ServerErrorMiddleware, which sits above the CORS middleware, so that response
never carries Access-Control-Allow-Origin and the browser reports a bare
"Failed to fetch" instead of the real cause (seen in prod on the RFQ bulk-send
OneDrive path when Graph answered an upload with an error). These tests pin the
fix: a registered handler converts the exception INSIDE the middleware stack,
so the response is a 502 that still flows out through CORS.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app

_PATH = "/_test/upstream-status-error"


@pytest.fixture()
def raising_route():
    @app.get(_PATH)
    def _boom():
        req = httpx.Request("PUT", "https://graph.microsoft.com/v1.0/me/drive/items")
        raise httpx.HTTPStatusError(
            "throttled", request=req, response=httpx.Response(429, request=req)
        )

    yield
    app.router.routes[:] = [
        r for r in app.router.routes if getattr(r, "path", None) != _PATH
    ]


def test_httpx_status_error_becomes_cors_safe_502(raising_route):
    origins = get_settings().cors_origin_list
    headers = {"Origin": origins[0]} if origins else {}
    client = TestClient(app, raise_server_exceptions=False)

    r = client.get(_PATH, headers=headers)

    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "HTTP 429" in detail
    # The Graph URL must never reach the client: upload-session URLs embed
    # pre-authenticated tokens.
    assert "graph.microsoft.com" not in detail
    if origins:
        assert r.headers.get("access-control-allow-origin")

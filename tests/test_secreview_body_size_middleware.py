"""Security review regression tests: request-body cap (group body-size-middleware).

Finding: MaxBodySizeMiddleware only compared the declared Content-Length, so a
chunked request (no Content-Length) or an under-declaring client was never
counted, and the single 460 MB multipart allowance applied to plain JSON routes
too. FastAPI buffers the whole body before auth resolves, so an anonymous
caller could pin hundreds of MB per request on any JSON route.

The fix counts the bytes actually received and picks the cap by ROUTE: only a
multipart/form-data request to a route whose handler declares Form/File params
keeps max_request_body_bytes; everything else (a spoofed multipart Content-Type
on a JSON route included, the retester's bypass) gets max_json_body_bytes.
"""

import httpx
import pytest
from fastapi import Depends, FastAPI, File, UploadFile
from pydantic import BaseModel

from app.core.config import Settings
from app.core.middleware import MaxBodySizeMiddleware, form_routes_matcher


async def _drive(mw, scope, body_events):
    """Run an ASGI middleware once and return the list of sent messages."""
    events = iter(body_events)

    async def receive():
        return next(events)

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await mw(scope, receive, send)
    return sent


def _reading_app(state: dict):
    """A bare ASGI app that drains the body (like FastAPI) then answers 200."""

    async def app(scope, receive, send):
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        state["body"] = body
        state["reached"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app


def _chunks(n: int, size: int) -> list[dict]:
    events = [{"type": "http.request", "body": b"x" * size, "more_body": True} for _ in range(n)]
    events[-1] = {"type": "http.request", "body": b"x" * size, "more_body": False}
    return events


# ── Bare ASGI: received bytes are counted, not just Content-Length ────────────


async def test_chunked_body_over_cap_is_refused_and_app_never_completes():
    state: dict = {}
    mw = MaxBodySizeMiddleware(_reading_app(state), max_bytes=100)
    # No Content-Length at all (Transfer-Encoding: chunked): the old code let
    # every byte through.
    scope = {"type": "http", "headers": [(b"content-type", b"application/json")]}
    sent = await _drive(mw, scope, _chunks(n=6, size=50))
    assert sent[0]["status"] == 413
    assert b"request_body_too_large" in sent[1]["body"]
    assert "reached" not in state


async def test_under_declared_content_length_is_still_refused():
    state: dict = {}
    mw = MaxBodySizeMiddleware(_reading_app(state), max_bytes=100)
    scope = {
        "type": "http",
        "headers": [(b"content-length", b"10"), (b"content-type", b"application/json")],
    }
    sent = await _drive(mw, scope, _chunks(n=3, size=60))
    assert sent[0]["status"] == 413
    assert "reached" not in state


async def test_chunked_body_under_cap_reaches_app_intact():
    state: dict = {}
    mw = MaxBodySizeMiddleware(_reading_app(state), max_bytes=1000)
    scope = {"type": "http", "headers": []}
    sent = await _drive(mw, scope, _chunks(n=4, size=50))
    assert state["reached"] and state["body"] == b"x" * 200
    assert sent[0]["status"] == 200


# ── Cap chosen by route AND Content-Type ─────────────────────────────────────


def _upload_only_files(method: str, path: str) -> bool:
    return method == "POST" and path == "/files"


@pytest.mark.parametrize(
    ("path", "content_type", "declared", "expect"),
    [
        ("/files", b"application/json", b"150", 413),  # over the json cap (100)
        ("/files", b"application/json", b"90", 200),
        ("/files", None, b"150", 413),  # no content type: treated as non-multipart
        ("/files", b"multipart/form-data; boundary=abc", b"150", 200),  # under multipart cap
        ("/files", b"Multipart/Form-Data; boundary=abc", b"150", 200),  # case-insensitive
        ("/files", b"multipart/form-data; boundary=abc", b"501", 413),  # over multipart cap
        # The retester's bypass: a multipart Content-Type on a route that is
        # NOT an upload route must get the small cap, in every spelling.
        ("/projects", b"multipart/form-data; boundary=abc", b"150", 413),
        ("/projects", b"multipart/form-data", b"150", 413),
        ("/projects", b" Multipart/Form-Data", b"150", 413),
        ("/projects", b"multipart/form-dataXX", b"150", 413),
        ("/files/other", b"multipart/form-data", b"150", 413),
    ],
)
async def test_route_and_content_type_select_the_cap(path, content_type, declared, expect):
    state: dict = {}
    mw = MaxBodySizeMiddleware(
        _reading_app(state), max_bytes=500, max_json_bytes=100, upload_routes=_upload_only_files
    )
    headers = [(b"content-length", declared)]
    if content_type is not None:
        headers.append((b"content-type", content_type))
    scope = {"type": "http", "method": "POST", "path": path, "headers": headers}
    sent = await _drive(mw, scope, [{"type": "http.request", "body": b"x" * 10}])
    assert sent[0]["status"] == expect


async def test_upload_route_wrong_method_gets_the_small_cap():
    mw = MaxBodySizeMiddleware(_reading_app({}), max_bytes=500, max_json_bytes=100, upload_routes=_upload_only_files)
    scope = {
        "type": "http",
        "method": "PUT",
        "path": "/files",
        "headers": [(b"content-length", b"150"), (b"content-type", b"multipart/form-data")],
    }
    sent = await _drive(mw, scope, [{"type": "http.request", "body": b"x" * 10}])
    assert sent[0]["status"] == 413


async def test_no_upload_routes_means_no_route_gets_the_large_cap():
    mw = MaxBodySizeMiddleware(_reading_app({}), max_bytes=500, max_json_bytes=100)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/files",
        "headers": [(b"content-length", b"150"), (b"content-type", b"multipart/form-data")],
    }
    sent = await _drive(mw, scope, [{"type": "http.request", "body": b"x" * 10}])
    assert sent[0]["status"] == 413


async def test_spoofed_multipart_chunked_body_on_json_route_is_cut_at_small_cap():
    state: dict = {}
    mw = MaxBodySizeMiddleware(_reading_app(state), max_bytes=10_000, max_json_bytes=100, upload_routes=_upload_only_files)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/projects",
        "headers": [(b"content-type", b"multipart/form-data; boundary=x")],
    }
    sent = await _drive(mw, scope, _chunks(n=6, size=50))
    assert sent[0]["status"] == 413
    assert "reached" not in state


def test_json_cap_defaults_to_the_multipart_cap_when_not_given():
    mw = MaxBodySizeMiddleware(_reading_app({}), max_bytes=77)
    assert mw.max_json_bytes == 77
    assert mw.upload_routes is None


# ── form_routes_matcher: only Form/File routes, method + path aware ───────────


def test_form_routes_matcher_finds_only_form_routes_and_reads_lazily():
    app = FastAPI()
    matcher = form_routes_matcher(app)

    class Body(BaseModel):
        name: str

    # Routes registered AFTER the matcher exists (main.py adds the middleware
    # before include_router) must still be seen.
    @app.post("/things")
    def create(body: Body):
        return {}

    @app.post("/things/{thing_id}/files")
    def upload(thing_id: str, file: UploadFile = File(...)):
        return {}

    @app.get("/things/{thing_id}/files")
    def list_files(thing_id: str):
        return {}

    assert matcher("POST", "/things/abc/files") is True
    assert matcher("post", "/things/abc/files") is True
    assert matcher("GET", "/things/abc/files") is False
    assert matcher("POST", "/things") is False
    assert matcher("POST", "/things/abc/files/extra") is False
    assert matcher("POST", "/nope") is False


# ── Real FastAPI app: 413 rendered by the app, before any dependency runs ─────


def _fastapi_app(max_bytes: int, max_json_bytes: int) -> tuple[FastAPI, dict]:
    state = {"auth_ran": False, "handler_ran": False}

    class Body(BaseModel):
        name: str

    def fake_auth():
        state["auth_ran"] = True
        return "user"

    app = FastAPI()
    app.add_middleware(
        MaxBodySizeMiddleware,
        max_bytes=max_bytes,
        max_json_bytes=max_json_bytes,
        upload_routes=form_routes_matcher(app),
    )

    @app.post("/things")
    def create(body: Body, user: str = Depends(fake_auth)):
        state["handler_ran"] = True
        return {"ok": body.name, "user": user}

    @app.post("/things/{thing_id}/files")
    def upload(thing_id: str, file: UploadFile = File(...), user: str = Depends(fake_auth)):
        state["handler_ran"] = True
        return {"ok": file.filename, "user": user}

    return app, state


async def _chunked_json(total: int, chunk: int):
    # A JSON object padded past the cap; httpx sends an async iterator as
    # Transfer-Encoding: chunked with no Content-Length.
    prefix = b'{"name": "'
    sent = 0
    yield prefix
    sent += len(prefix)
    while sent < total:
        piece = b"a" * min(chunk, total - sent)
        yield piece
        sent += len(piece)
    yield b'"}'


async def test_fastapi_chunked_json_over_cap_gets_413_before_auth():
    app, state = _fastapi_app(max_bytes=10_000, max_json_bytes=1_000)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post(
            "/things",
            content=_chunked_json(total=5_000, chunk=500),
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 413
    assert resp.json() == {"detail": "request_body_too_large"}
    assert state["auth_ran"] is False
    assert state["handler_ran"] is False


async def test_fastapi_json_over_json_cap_but_under_multipart_cap_gets_413():
    app, state = _fastapi_app(max_bytes=10_000, max_json_bytes=1_000)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post("/things", json={"name": "a" * 2_000})
    assert resp.status_code == 413
    assert resp.json() == {"detail": "request_body_too_large"}
    assert state["auth_ran"] is False


async def test_fastapi_small_json_passes_and_multipart_gets_the_large_cap():
    app, state = _fastapi_app(max_bytes=10_000, max_json_bytes=1_000)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post("/things", json={"name": "fine"})
        assert resp.status_code == 200 and state["handler_ran"]

        # A multipart body between the two caps on the REAL upload route passes
        # the middleware and reaches the handler with the large cap.
        state["handler_ran"] = False
        resp = await client.post(
            "/things/abc/files",
            files={"file": ("big.bin", b"z" * 3_000, "application/octet-stream")},
        )
        assert resp.status_code == 200 and state["handler_ran"]
        assert resp.json()["ok"] == "big.bin"


@pytest.mark.parametrize(
    "content_type",
    [
        "multipart/form-data",
        "multipart/form-data; boundary=x",
        " Multipart/Form-Data",
        "multipart/form-dataXX",
    ],
)
async def test_fastapi_spoofed_multipart_on_json_route_gets_413_before_auth(content_type):
    # The retester's bypass: the same body that the JSON cap refuses, sent to a
    # JSON route with a multipart Content-Type, used to get the 460 MB cap and
    # sit in memory until auth answered 401. The cap is now chosen by route.
    app, state = _fastapi_app(max_bytes=10_000, max_json_bytes=1_000)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post(
            "/things",
            content=_chunked_json(total=5_000, chunk=500),
            headers={"content-type": content_type},
        )
        assert resp.status_code == 413
        assert resp.json() == {"detail": "request_body_too_large"}
        assert state["auth_ran"] is False and state["handler_ran"] is False

        # Honest Content-Length, same spoof: refused before a byte is read.
        resp = await client.post(
            "/things",
            content=b"y" * 5_000,
            headers={"content-type": content_type},
        )
        assert resp.status_code == 413
        assert state["auth_ran"] is False and state["handler_ran"] is False

        # And a non-multipart body on the upload route still gets the small cap.
        resp = await client.post("/things/abc/files", content=b"y" * 5_000, headers={"content-type": "text/plain"})
        assert resp.status_code == 413
        assert state["auth_ran"] is False


# ── Settings + wiring ─────────────────────────────────────────────────────────


def test_settings_default_json_cap_and_bounds():
    s = Settings(_env_file=None)
    assert s.max_json_body_bytes == 16 * 1024 * 1024
    assert s.max_json_body_bytes < s.max_request_body_bytes
    # The largest legitimate JSON bodies (400k-char text fields) fit comfortably.
    assert s.max_json_body_bytes > 4 * max(s.boq_max_text_chars, s.openai_proposal_max_input_chars)
    with pytest.raises(ValueError):
        Settings(_env_file=None, max_json_body_bytes=s.max_request_body_bytes + 1)
    with pytest.raises(ValueError):
        Settings(_env_file=None, max_json_body_bytes=0)


def test_main_app_wires_both_caps():
    from app.main import app

    entries = [m for m in app.user_middleware if m.cls is MaxBodySizeMiddleware]
    assert len(entries) == 1
    kwargs = entries[0].kwargs
    assert kwargs["max_bytes"] == 460 * 1024 * 1024
    assert kwargs["max_json_bytes"] == 16 * 1024 * 1024

    # The large cap is bound to the real upload routes, and to nothing else.
    matcher = kwargs["upload_routes"]
    assert matcher("POST", "/projects/00000000-0000-0000-0000-000000000000/files") is True
    assert matcher("POST", "/rfp-ingest/runs/abc/files") is True
    assert matcher("POST", "/bid-splitter/jobs/abc/files") is True
    assert matcher("POST", "/submittals/files") is True
    assert matcher("POST", "/pm/projects/abc/documents") is True
    assert matcher("POST", "/payroll/employees/abc/documents") is True
    assert matcher("POST", "/projects") is False  # the finding's own target
    assert matcher("GET", "/projects/abc/files") is False
    assert matcher("POST", "/projects/abc/files/extra") is False
    assert matcher("POST", "/users") is False


async def test_main_app_refuses_spoofed_multipart_on_projects_before_auth():
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    body = b'{"name": "' + b"a" * (16 * 1024 * 1024 + 1) + b'"}'  # 1 byte over 16 MB
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post(
            "/projects",
            content=body,
            headers={"content-type": "multipart/form-data; boundary=x"},
        )
        assert resp.status_code == 413
        assert resp.json() == {"detail": "request_body_too_large"}

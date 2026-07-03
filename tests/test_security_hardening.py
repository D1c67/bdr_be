"""Verification tests for the security-hardening changes.

These exercise the NEW controls directly (rate limiting, body-size + header
middleware, JWT algorithm selection, extracted-quote validation, workbook-render
caps, spooled export, upload cap), which the rest of the suite doesn't touch.
"""

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from app.core import ratelimit, security
from app.core.deps import CurrentUser
from app.core.middleware import MaxBodySizeMiddleware, SecurityHeadersMiddleware
from app.core.roles import Role
from app.services import file_export, rfq_inbox

EXCEL_DIR = Path(__file__).resolve().parents[1] / "excel_format"
BOQ = EXCEL_DIR / "BOQ - COUNTS.xlsx"


def _user(role=Role.ESTIMATING_ADMIN, uid="u1"):
    return CurrentUser(id=uid, email="e@g3.com", role=role, is_active=True)


@pytest.fixture(autouse=True)
def _clear_buckets():
    ratelimit._buckets.clear()
    yield
    ratelimit._buckets.clear()


# ── Rate limiting ─────────────────────────────────────────────────────────────


async def test_rate_limit_raises_429_with_code_and_headers():
    dep = ratelimit.rate_limit("t_scope", lambda: 2)
    u = _user()
    await dep(user=u)
    await dep(user=u)  # 2 allowed
    with pytest.raises(HTTPException) as ei:
        await dep(user=u)  # 3rd trips
    assert ei.value.status_code == 429
    assert ei.value.detail == "rate_limited"
    assert "Retry-After" in ei.value.headers
    assert ei.value.headers["X-RateLimit-Scope"] == "t_scope"


async def test_rate_limit_is_per_account():
    dep = ratelimit.rate_limit("t_scope2", lambda: 1)
    await dep(user=_user(uid="a"))
    # A different account has its own bucket — not throttled by the first.
    await dep(user=_user(uid="b"))
    with pytest.raises(HTTPException):
        await dep(user=_user(uid="a"))


async def test_rate_limit_role_filter_skips_other_roles():
    dep = ratelimit.rate_limit("t_scope3", lambda: 1, roles=frozenset({Role.ESTIMATOR}))
    # Non-estimator is exempt no matter how many times it's called.
    for _ in range(5):
        await dep(user=_user(role=Role.EXECUTIVE))
    # The estimator is limited.
    await dep(user=_user(role=Role.ESTIMATOR, uid="est"))
    with pytest.raises(HTTPException):
        await dep(user=_user(role=Role.ESTIMATOR, uid="est"))


async def test_rate_limit_master_switch_disables(monkeypatch):
    monkeypatch.setattr(
        ratelimit, "get_settings", lambda: SimpleNamespace(rate_limit_enabled=False)
    )
    dep = ratelimit.rate_limit("t_scope4", lambda: 1)
    u = _user()
    for _ in range(10):  # never raises while disabled
        await dep(user=u)


# ── Middleware ────────────────────────────────────────────────────────────────


async def _drive(mw, scope, body_events=None):
    """Run an ASGI middleware once and return the list of sent messages."""
    events = iter(body_events or [{"type": "http.request", "body": b""}])

    async def receive():
        return next(events)

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await mw(scope, receive, send)
    return sent


async def test_body_size_middleware_rejects_oversized():
    async def app(scope, receive, send):  # should not run
        raise AssertionError("app should not be reached for oversized body")

    mw = MaxBodySizeMiddleware(app, max_bytes=100)
    scope = {"type": "http", "headers": [(b"content-length", b"500")]}
    sent = await _drive(mw, scope)
    assert sent[0]["status"] == 413
    assert b"request_body_too_large" in sent[1]["body"]


async def test_body_size_middleware_allows_within_cap():
    reached = {"ok": False}

    async def app(scope, receive, send):
        reached["ok"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})

    mw = MaxBodySizeMiddleware(app, max_bytes=1000)
    scope = {"type": "http", "headers": [(b"content-length", b"50")]}
    await _drive(mw, scope)
    assert reached["ok"]


async def test_security_headers_injected_and_not_clobbered():
    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                # Pretend the handler already set Referrer-Policy — must be kept.
                "headers": [(b"referrer-policy", b"unsafe-url")],
            }
        )

    mw = SecurityHeadersMiddleware(
        app,
        {
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )
    sent = await _drive(mw, {"type": "http"})
    headers = {k.decode(): v.decode() for k, v in sent[0]["headers"]}
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "unsafe-url"  # existing value preserved


# ── JWT algorithm selection ───────────────────────────────────────────────────


def test_decode_token_accepts_hs256_when_enabled(monkeypatch):
    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: SimpleNamespace(legacy_hs256_enabled=True, supabase_jwt_secret="s3cret"),
    )
    token = jwt.encode({"sub": "u1", "aal": "aal2"}, "s3cret", algorithm="HS256")
    claims = security.decode_token(token)
    assert claims["sub"] == "u1"


def test_decode_token_rejects_hs256_when_disabled(monkeypatch):
    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: SimpleNamespace(legacy_hs256_enabled=False, supabase_jwt_secret="s3cret"),
    )
    token = jwt.encode({"sub": "u1"}, "s3cret", algorithm="HS256")
    with pytest.raises(jwt.PyJWTError):
        security.decode_token(token)


def test_decode_token_rejects_unexpected_algorithm(monkeypatch):
    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: SimpleNamespace(legacy_hs256_enabled=True, supabase_jwt_secret="s3cret"),
    )
    # An algorithm outside the accepted {RS256, ES256, HS256} set is refused
    # without ever consulting the shared secret or JWKS.
    token = jwt.encode({"sub": "u1"}, "s3cret", algorithm="HS384")
    with pytest.raises(jwt.InvalidAlgorithmError):
        security.decode_token(token)


# ── Extracted-quote validation (prompt-injection / garbage guard) ─────────────


@pytest.mark.parametrize(
    "result,ok",
    [
        ({"total_amount": "1500.00", "confidence": 0.9}, True),
        ({"total_amount": 250000}, True),  # confidence absent is allowed
        ({"total_amount": "-5"}, False),  # negative would win lowest-quote
        ({"total_amount": "0"}, False),
        ({"total_amount": "999999999999"}, False),  # over the sanity ceiling
        ({"total_amount": "abc"}, False),
        ({"total_amount": None}, False),
        ({"total_amount": "1000", "confidence": 0.1}, False),  # low confidence
    ],
)
def test_valid_extracted_amount(result, ok):
    assert rfq_inbox._valid_extracted_amount(result) is ok


# ── Workbook render cap ───────────────────────────────────────────────────────


@pytest.mark.skipif(not BOQ.exists(), reason="example BOQ not present")
def test_worksheets_to_text_respects_char_cap():
    from app.services import boq_extraction as bx

    full = bx.worksheets_to_text(BOQ.read_bytes())
    capped = bx.worksheets_to_text(BOQ.read_bytes(), max_chars=200)
    assert len(capped) < len(full)
    assert "[TRUNCATED" in capped
    # A generous cap yields the whole document, no truncation marker.
    assert "[TRUNCATED" not in bx.worksheets_to_text(BOQ.read_bytes(), max_chars=10_000_000)


# ── Spooled export ────────────────────────────────────────────────────────────


def test_build_export_spooled_streams_valid_zip(monkeypatch):
    monkeypatch.setattr(file_export.storage, "download_file", lambda p: f"body:{p}".encode())
    rows = [
        {"category": "drawing", "storage_path": "a", "filename": "E-1.pdf", "size_bytes": 6},
        {"category": "estimate", "storage_path": "b", "filename": "Est.xlsx", "size_bytes": 6},
    ]
    spool, manifest, size = file_export.build_export_spooled(rows)
    try:
        data = spool.read()
    finally:
        spool.close()
    assert size == len(data)  # reported size matches the streamable bytes
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
    assert "drawing/E-1.pdf" in names
    assert "estimate/Est.xlsx" in names
    assert "MANIFEST.txt" in names
    assert all(m["status"] == "ok" for m in manifest)


# ── Upload size cap ───────────────────────────────────────────────────────────


async def test_read_capped_allows_under_limit():
    from app.routers.files import _read_capped

    up = UploadFile(filename="x.bin", file=io.BytesIO(b"a" * 50))
    assert await _read_capped(up, 100) == b"a" * 50


async def test_read_capped_rejects_over_limit():
    from app.routers.files import _read_capped

    up = UploadFile(filename="x.bin", file=io.BytesIO(b"a" * 500))
    with pytest.raises(HTTPException) as ei:
        await _read_capped(up, 100)
    assert ei.value.status_code == 413

"""Security review, group files-upload-delete.

Finding 7: an assigned external estimator could OOM a shared worker with a few
concurrent near-cap uploads. Fixes under test:
  (a) _read_capped lands the body in memory ONCE (no bytearray + bytes copy);
  (b) uploads at or above `large_upload_bytes` hold an in-flight slot, 1 per
      account and 3 per process, and a saturated slot is a code-tagged 429;
  (c) the estimator role gets its own, lower per-file cap
      (`estimator_upload_max_bytes`); internal callers keep upload_max_bytes.

Finding 38: DELETE /projects/{id}/files/{file_id} fans out drawing_changed
bells and mirror emails with no limiter; it now carries the DEFAULT budget.
"""

import io
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile

from app.core import ratelimit
from app.core.deps import CurrentUser
from app.core.error_codes import ErrorCode, RateLimitScope
from app.core.roles import Role
from app.routers import files as files_mod
from app.routers.files import _read_capped, delete_file, upload_file

MB = 1024 * 1024


def _user(role=Role.ESTIMATING_ADMIN, uid="u1"):
    return CurrentUser(id=uid, email="e@g3.com", role=role, is_active=True)


def _settings(**over):
    base = dict(
        rate_limit_enabled=True,
        large_upload_bytes=20 * MB,
        large_upload_max_concurrent=3,
        large_upload_max_concurrent_per_user=1,
        upload_max_bytes=450 * MB,
        estimator_upload_max_bytes=200 * MB,
        default_rate_limit_per_min=240,
        upload_rate_limit_per_min=20,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    ratelimit._buckets.clear()
    with ratelimit._inflight_lock:
        ratelimit._inflight_total = 0
        ratelimit._inflight_by_user.clear()
    monkeypatch.setattr(ratelimit, "get_settings", _settings)
    yield
    ratelimit._buckets.clear()
    with ratelimit._inflight_lock:
        ratelimit._inflight_total = 0
        ratelimit._inflight_by_user.clear()


# ── (a) one copy in _read_capped ──────────────────────────────────────────────


class _OneShotUpload:
    """A source whose read() hands back one fixed bytes object, so the test can
    tell whether _read_capped returned it as-is or copied it."""

    filename = "x.bin"

    def __init__(self, payload: bytes, size=None):
        self.payload = payload
        self.size = size
        self.reads = 0

    async def read(self, n=-1):
        self.reads += 1
        if self.reads == 1:
            return self.payload
        return b""


async def test_read_capped_returns_the_read_bytes_without_copying():
    payload = b"a" * 4096
    up = _OneShotUpload(payload)
    out = await _read_capped(up, 8192)
    assert out == payload
    # The old code accumulated into a bytearray and returned bytes(buf): a
    # second full-size copy. The body must now be handed back as read.
    assert out is payload


async def test_read_capped_single_bounded_read_for_a_real_upload():
    up = UploadFile(filename="x.bin", file=io.BytesIO(b"b" * 300))
    assert await _read_capped(up, 1000) == b"b" * 300


async def test_read_capped_still_413s_over_cap_for_short_readers():
    class _Chunky:
        filename = "x.bin"
        size = None

        def __init__(self):
            self.chunks = [b"a" * 60, b"a" * 60]

        async def read(self, n=-1):
            return self.chunks.pop(0) if self.chunks else b""

    with pytest.raises(HTTPException) as ei:
        await _read_capped(_Chunky(), 100)
    assert ei.value.status_code == 413


async def test_read_capped_413s_exactly_one_byte_over_cap():
    up = UploadFile(filename="x.bin", file=io.BytesIO(b"a" * 101))
    with pytest.raises(HTTPException) as ei:
        await _read_capped(up, 100)
    assert ei.value.status_code == 413
    # Exactly at the cap is fine.
    up = UploadFile(filename="x.bin", file=io.BytesIO(b"a" * 100))
    assert len(await _read_capped(up, 100)) == 100


# ── (b) in-flight large-upload slots ──────────────────────────────────────────


def test_small_uploads_never_take_a_slot():
    with ratelimit.large_upload_slot("u1", 5 * MB):
        with ratelimit.large_upload_slot("u1", 5 * MB):
            assert ratelimit.inflight_large_uploads() == (0, {})


def test_second_large_upload_by_same_user_is_429_with_headers():
    with ratelimit.large_upload_slot("u1", 100 * MB):
        assert ratelimit.inflight_large_uploads() == (1, {"u1": 1})
        with pytest.raises(HTTPException) as ei:
            with ratelimit.large_upload_slot("u1", 100 * MB):
                pass
        assert ei.value.status_code == 429
        assert ei.value.detail == ErrorCode.RATE_LIMITED
        assert ei.value.headers["X-RateLimit-Scope"] == RateLimitScope.FILE_UPLOAD
        assert int(ei.value.headers["Retry-After"]) > 0
    # Released on exit.
    assert ratelimit.inflight_large_uploads() == (0, {})


def test_process_wide_cap_of_three_large_uploads():
    with ratelimit.large_upload_slot("a", 100 * MB), ratelimit.large_upload_slot(
        "b", 100 * MB
    ), ratelimit.large_upload_slot("c", 100 * MB):
        assert ratelimit.inflight_large_uploads()[0] == 3
        with pytest.raises(HTTPException) as ei:
            with ratelimit.large_upload_slot("d", 100 * MB):
                pass
        assert ei.value.status_code == 429
    # All three released; a fourth account now gets in.
    with ratelimit.large_upload_slot("d", 100 * MB):
        assert ratelimit.inflight_large_uploads() == (1, {"d": 1})


def test_unknown_size_counts_as_large():
    with ratelimit.large_upload_slot("u1", None):
        assert ratelimit.inflight_large_uploads() == (1, {"u1": 1})


def test_slot_released_when_the_body_raises():
    with pytest.raises(RuntimeError):
        with ratelimit.large_upload_slot("u1", 100 * MB):
            raise RuntimeError("storage down")
    assert ratelimit.inflight_large_uploads() == (0, {})


def test_master_switch_disables_the_slot(monkeypatch):
    monkeypatch.setattr(ratelimit, "get_settings", lambda: _settings(rate_limit_enabled=False))
    with ratelimit.large_upload_slot("u1", 100 * MB):
        with ratelimit.large_upload_slot("u1", 100 * MB):
            assert ratelimit.inflight_large_uploads() == (0, {})


# ── upload_file wiring: cap per role + slot around the buffering ─────────────


class _FakeUpload:
    def __init__(self, content=b"pdfbytes", filename="estimate.xlsx", size=None):
        self.filename = filename
        self.size = len(content) if size is None else size
        self._chunks = [content]

    async def read(self, n=-1):
        return self._chunks.pop(0) if self._chunks else b""


class _InsertSB:
    def __init__(self):
        self.inserted = None

    def table(self, name):
        return self

    def insert(self, payload):
        self.inserted = payload
        return self

    def execute(self):
        return SimpleNamespace(data=[{**(self.inserted or {}), "id": "new-file"}])


def _upload_env(monkeypatch):
    sb = _InsertSB()
    monkeypatch.setattr(files_mod, "get_supabase", lambda: sb)
    monkeypatch.setattr(files_mod, "get_settings", _settings)
    monkeypatch.setattr(files_mod.storage, "build_object_path", lambda *a, **k: "p1/estimate/x")
    monkeypatch.setattr(files_mod.storage, "upload_file", lambda *a, **k: None)
    monkeypatch.setattr(files_mod.office_preview, "is_convertible", lambda *a, **k: False)
    monkeypatch.setattr(files_mod, "audit", lambda *a, **k: None)
    monkeypatch.setattr(files_mod, "handoff_locked", lambda _pid: False)
    monkeypatch.setattr(files_mod.files_needed, "clear_if_satisfied", lambda *a, **k: None)
    monkeypatch.setattr(files_mod, "_notify_drawing_changed", lambda *a, **k: None)
    return sb


async def _do_upload(**kw):
    kw.setdefault("background", BackgroundTasks())
    kw.setdefault("material_category_id", None)
    kw.setdefault("note", None)
    kw.setdefault("doc_type", None)
    kw.setdefault("addendum_number", None)
    kw.setdefault("addendum_issued_on", None)
    kw.setdefault("file", _FakeUpload())
    kw.setdefault("project_id", "p1")
    return await upload_file(**kw)


async def test_estimator_upload_uses_the_lower_cap(monkeypatch):
    _upload_env(monkeypatch)
    seen = {}

    async def _spy(upload, max_bytes):
        seen["max_bytes"] = max_bytes
        return b"pdfbytes"

    monkeypatch.setattr(files_mod, "_read_capped", _spy)
    await _do_upload(category="estimate", user=_user(Role.ESTIMATOR, "est"))
    assert seen["max_bytes"] == 200 * MB


async def test_internal_upload_keeps_the_full_cap(monkeypatch):
    _upload_env(monkeypatch)
    seen = {}

    async def _spy(upload, max_bytes):
        seen["max_bytes"] = max_bytes
        return b"pdfbytes"

    monkeypatch.setattr(files_mod, "_read_capped", _spy)
    await _do_upload(category="drawing", user=_user())
    assert seen["max_bytes"] == 450 * MB


async def test_estimator_over_its_cap_is_413_even_under_upload_max_bytes(monkeypatch):
    _upload_env(monkeypatch)
    # Advertised 300 MB: under the internal 450 MB cap, over the estimator's 200 MB.
    big = _FakeUpload(size=300 * MB)
    with pytest.raises(HTTPException) as ei:
        await _do_upload(category="estimate", user=_user(Role.ESTIMATOR, "est"), file=big)
    assert ei.value.status_code == 413


async def test_large_upload_429s_while_the_same_account_has_one_in_flight(monkeypatch):
    sb = _upload_env(monkeypatch)
    est = _user(Role.ESTIMATOR, "est")
    # Simulate the first large upload still being buffered by this account.
    with ratelimit.large_upload_slot(est.id, 100 * MB):
        with pytest.raises(HTTPException) as ei:
            await _do_upload(category="estimate", user=est, file=_FakeUpload(size=100 * MB))
        assert ei.value.status_code == 429
        assert ei.value.detail == ErrorCode.RATE_LIMITED
        assert "Retry-After" in ei.value.headers
        assert sb.inserted is None  # nothing was buffered, pushed or recorded
    # Slot free again: the upload goes through and its size is recorded.
    row = await _do_upload(category="estimate", user=est)
    assert row["id"] == "new-file"
    assert sb.inserted["size_bytes"] == len(b"pdfbytes")
    assert ratelimit.inflight_large_uploads() == (0, {})


async def test_large_upload_slot_is_released_after_a_successful_upload(monkeypatch):
    _upload_env(monkeypatch)
    seen = {}

    async def _spy(upload, max_bytes):
        seen["inflight"] = ratelimit.inflight_large_uploads()
        return b"pdfbytes"

    monkeypatch.setattr(files_mod, "_read_capped", _spy)
    await _do_upload(category="drawing", user=_user(), file=_FakeUpload(size=100 * MB))
    # Held while buffering, released once the body was pushed.
    assert seen["inflight"] == (1, {"u1": 1})
    assert ratelimit.inflight_large_uploads() == (0, {})


# ── Finding 38: the delete route carries a limiter ───────────────────────────


def _route(path_suffix: str, method: str):
    for r in files_mod.router.routes:
        if r.path.endswith(path_suffix) and method in r.methods:
            return r
    raise AssertionError(f"no {method} route ending in {path_suffix}")


def test_delete_route_has_the_default_budget_limiter():
    route = _route("/{file_id}", "DELETE")
    assert route.endpoint is delete_file
    assert any(
        d.call is files_mod.file_delete_rate_limit for d in route.dependant.dependencies
    )


async def test_delete_limiter_is_the_default_scope_and_429s_past_the_budget(monkeypatch):
    monkeypatch.setattr(ratelimit, "get_settings", lambda: _settings())
    monkeypatch.setattr(files_mod, "get_settings", lambda: _settings(default_rate_limit_per_min=2))
    u = _user()
    await files_mod.file_delete_rate_limit(user=u)
    await files_mod.file_delete_rate_limit(user=u)
    with pytest.raises(HTTPException) as ei:
        await files_mod.file_delete_rate_limit(user=u)
    assert ei.value.status_code == 429
    assert ei.value.detail == ErrorCode.RATE_LIMITED
    assert ei.value.headers["X-RateLimit-Scope"] == RateLimitScope.DEFAULT

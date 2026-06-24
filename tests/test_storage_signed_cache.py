"""Unit tests for signed-URL memoization (no network)."""

import pytest

from app.services import storage


class _CountingStorage:
    def __init__(self):
        self.mints = 0

    def from_(self, bucket):
        return self

    def create_signed_url(self, path, ttl):
        self.mints += 1
        return {"signedURL": f"https://x/{path}?token=t{self.mints}"}


@pytest.fixture
def counting(monkeypatch):
    stub = _CountingStorage()

    class _SB:
        storage = stub

    monkeypatch.setattr(storage, "get_supabase", lambda: _SB())
    storage._signed_url_cache.clear()
    yield stub
    storage._signed_url_cache.clear()


def _freeze_time(monkeypatch, t):
    monkeypatch.setattr(storage.time, "time", lambda: t)


def test_same_path_reuses_url(monkeypatch, counting):
    _freeze_time(monkeypatch, 1000.0)
    u1 = storage.signed_url("p/a.pdf")
    u2 = storage.signed_url("p/a.pdf")
    assert u1 == u2
    assert counting.mints == 1


def test_remints_near_expiry(monkeypatch, counting):
    _freeze_time(monkeypatch, 1000.0)
    u1 = storage.signed_url("p/a.pdf")  # ttl 900 → expires 1900
    _freeze_time(monkeypatch, 1900.0 - 30)  # inside the 60s refresh margin
    u2 = storage.signed_url("p/a.pdf")
    assert u1 != u2
    assert counting.mints == 2


def test_use_cache_false_always_mints(monkeypatch, counting):
    _freeze_time(monkeypatch, 1000.0)
    storage.signed_url("p/a.pdf")
    storage.signed_url("p/a.pdf", use_cache=False)
    assert counting.mints == 2


def test_explicit_ttl_bypasses_cache(monkeypatch, counting):
    _freeze_time(monkeypatch, 1000.0)
    storage.signed_url("p/a.pdf", ttl_seconds=60)
    storage.signed_url("p/a.pdf", ttl_seconds=60)
    assert counting.mints == 2


def test_different_paths_mint_separately(monkeypatch, counting):
    _freeze_time(monkeypatch, 1000.0)
    storage.signed_url("p/a.pdf")
    storage.signed_url("p/b.pdf")
    assert counting.mints == 2

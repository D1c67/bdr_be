"""Harness checks do not substitute for running the PostgreSQL race probe."""
from contextlib import contextmanager
import itertools
import threading

import pytest

from scripts.check_assistant_browser_concurrency import race, validate_local_uri


@pytest.mark.parametrize("uri", [
    "postgresql://postgres:secret@127.0.0.1:54322/phase6_probe_test",
    "postgres://postgres:secret@[::1]/phase6_probe_test",
])
def test_disposable_loopback_uri(uri):
    assert validate_local_uri(uri) == uri


@pytest.mark.parametrize("uri", [
    "", "host=127.0.0.1 dbname=phase6_probe_test",
    "postgresql://localhost/phase6_probe_test", "postgresql://db.example/phase6_probe_test",
    "postgresql://127.0.0.1/postgres", "postgresql://127.0.0.1/phase6_probe_",
    "postgresql://127.0.0.1/phase6_probe_test?host=db.example",
    "postgresql://127.0.0.1/phase6_probe_test#fragment",
    "postgresql://127.0.0.1/phase6_probe_%2fother",
])
def test_rejects_external_or_non_disposable_targets(uri):
    with pytest.raises(ValueError):
        validate_local_uri(uri)


class FakeConnections:
    """Small lock simulation to verify orchestration, release and false-pass refusal."""
    def __init__(self):
        self.ids = itertools.count(1)
        self.lock = threading.Lock()
        self.owner = None
        self.waiter = None
        self.closed = []

    @contextmanager
    def connect(self):
        source = self

        class Connection:
            pid = next(source.ids)

            def execute(self, sql, args=None):
                value = self.pid if "pg_backend_pid" in sql else (
                    [source.owner] if source.waiter == args[0] else [])
                return type("Cursor", (), {"fetchone": lambda _: (value,)})()

        conn = Connection()
        try:
            yield conn
        finally:
            if self.owner == conn.pid:
                self.owner = None
                self.lock.release()
            self.closed.append(conn.pid)

    def first(self, conn):
        self.lock.acquire()
        self.owner = conn.pid
        return "first"

    def second(self, conn):
        self.waiter = conn.pid
        with self.lock:
            return "second"


def test_race_observes_distinct_blocked_sessions_then_releases_holder():
    db = FakeConnections()
    assert race(db.connect, db.first, db.second, timeout=1) == ("first", "second")
    assert len(db.closed) == 3
    assert not db.lock.locked()


def test_race_rejects_nonoverlap_and_releases_locks_on_failure():
    db = FakeConnections()
    with pytest.raises(RuntimeError, match="without observed lock contention"):
        race(db.connect, db.first, lambda _: "unblocked", timeout=1)
    assert not db.lock.locked()

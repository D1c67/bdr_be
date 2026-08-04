"""Dev AI monitor (routers/llm_monitor): guards, aggregation, job actions.

Invariants pinned here:

  guards      every route requires require_dev AND llm_monitor_rate_limit;
              a non-dev token can never read the ledger or touch jobs.
  summary     aggregates llm_call_log on the Los Angeles calendar into
              totals / by_day / by_feature / by_error_kind / by_tier plus
              live queue counts, without mutating anything.
  failures    recent failed calls and terminally failed/canceled jobs,
              with project labels joined onto the job views.
  queue       claim-order listing where only queued rows get a position.
  retry       failed/canceled jobs get a FRESH queued job (attempt cycle
              reset, old row kept as history); anything else is a 409.
  cancel      queued jobs only; running or terminal rows are a 409.

Supabase is faked in-memory per house convention (own copy, llm_jobs insert
defaults included so llm_queue.requeue_terminal can run for real).
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.config import Settings
from app.core.deps import CurrentUser, require_dev
from app.core.ratelimit import llm_monitor_rate_limit
from app.core.roles import Role
from app.routers import llm_monitor
from app.services import llm_queue as lq


# ── Fake Supabase ────────────────────────────────────────────────────────


def _ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return value


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._op = None
        self._payload = None
        self._filters = []
        self._single = False
        self._orders = []
        self._limit = None
        self._range = None

    def select(self, sel="*", count=None, **k):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, list(vals)))
        return self

    def lt(self, col, val):
        self._filters.append(("lt", col, val))
        return self

    def gte(self, col, val):
        self._filters.append(("gte", col, val))
        return self

    def single(self):
        self._single = True
        return self

    def order(self, col, desc=False, **k):
        self._orders.append((col, desc))
        return self

    def limit(self, n, **k):
        self._limit = n
        return self

    def range(self, start, end, **k):
        self._range = (start, end)
        return self

    def _matches(self, row):
        for op, col, val in self._filters:
            have = row.get(col)
            if op == "eq" and have != val:
                return False
            if op == "in" and have not in val:
                return False
            if op == "lt" and (have is None or not _ts(have) < _ts(val)):
                return False
            if op == "gte" and (have is None or not _ts(have) >= _ts(val)):
                return False
        return True

    def _insert_one(self, payload, rows):
        row = dict(payload)
        if self.table == "llm_jobs":
            for r in rows:
                if (
                    r.get("job_type") == row.get("job_type")
                    and r.get("target_id") == row.get("target_id")
                    and r.get("status") in ("queued", "running")
                ):
                    raise Exception(
                        "duplicate key value violates unique constraint "
                        '"llm_jobs_active_target_uq" (code 23505)'
                    )
            row.setdefault("status", "queued")
            row.setdefault("attempts", 0)
            row.setdefault("priority", 100)
            row.setdefault("next_attempt_at", self.db.now.isoformat())
            for col in (
                "claimed_by", "lease_expires_at", "error_kind",
                "last_error", "started_at", "finished_at",
            ):
                row.setdefault(col, None)
        row.setdefault("id", uuid.uuid4().hex)
        row.setdefault("created_at", self.db.next_created_at())
        rows.append(row)
        return dict(row)

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "select":
            hits = [r for r in rows if self._matches(r)]
            for col, desc in reversed(self._orders):
                hits.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
            total = len(hits)
            if self._range is not None:
                hits = hits[self._range[0] : self._range[1] + 1]
            if self._limit is not None:
                hits = hits[: self._limit]
            hits = [dict(r) for r in hits]
            if self._single:
                return SimpleNamespace(data=(hits[0] if hits else None), count=total)
            return SimpleNamespace(data=hits, count=total)
        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            return SimpleNamespace(data=[self._insert_one(p, rows) for p in payloads])
        if self._op == "update":
            out = []
            for r in rows:
                if self._matches(r):
                    r.update(self._payload)
                    out.append(dict(r))
            return SimpleNamespace(data=out)
        if self._op == "delete":
            self.db.tables[self.table] = [r for r in rows if not self._matches(r)]
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=[])


class FakeDB:
    def __init__(self, tables=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self.now = datetime.now(timezone.utc)
        self._seq = 0

    def table(self, name):
        return _Query(self, name)

    def next_created_at(self):
        self._seq += 1
        return (self.now + timedelta(seconds=self._seq)).isoformat()


def _dev(uid="dev1"):
    return CurrentUser(
        id=uid, email="dev@g3.com", role=Role.IT_ADMIN, is_active=True, is_dev=True
    )


def _install(monkeypatch, db):
    audits = []
    monkeypatch.setattr(llm_monitor, "get_supabase", lambda: db)
    monkeypatch.setattr(llm_monitor, "audit", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(lq, "get_supabase", lambda: db)
    monkeypatch.setattr(lq, "get_settings", lambda: Settings(_env_file=None))
    return audits


def _job_row(**over):
    row = {
        "id": uuid.uuid4().hex,
        "job_type": "boq_extraction",
        "feature": "boq",
        "target_id": "a1",
        "project_id": None,
        "payload": {"analysis_id": "a1"},
        "status": "queued",
        "priority": 100,
        "attempts": 0,
        "max_attempts": 6,
        "next_attempt_at": None,
        "claimed_by": None,
        "lease_expires_at": None,
        "error_kind": None,
        "last_error": None,
        "started_at": None,
        "finished_at": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    row.update(over)
    return row


# 20:00 UTC is midday in Los Angeles (UTC-7/-8), so the LA calendar day
# always equals the UTC date at that hour: no DST ambiguity in the expected
# day labels.
def _stamp(days_ago: int) -> str:
    return (
        (datetime.now(timezone.utc) - timedelta(days=days_ago))
        .replace(hour=20, minute=0, second=0, microsecond=0)
        .isoformat()
    )


def _la_day(stamp_iso: str) -> str:
    return stamp_iso[:10]


# ── Guard wiring ─────────────────────────────────────────────────────────


def test_every_monitor_route_requires_dev_and_rate_limit():
    routes = list(llm_monitor.router.routes)
    assert len(routes) == 5
    for route in routes:
        deps = [d.call for d in route.dependant.dependencies]
        assert require_dev in deps, f"{route.path} missing require_dev"
        assert llm_monitor_rate_limit in deps, f"{route.path} missing llm_monitor_rate_limit"


# ── summary ──────────────────────────────────────────────────────────────


def test_summary_aggregates_calls_and_queue(monkeypatch):
    day_a, day_b = _stamp(3), _stamp(2)
    db = FakeDB({
        "llm_call_log": [
            {"id": "c1", "feature": "boq", "provider": "anthropic", "tier": "job",
             "ok": True, "error_kind": None, "duration_ms": 100, "created_at": day_a},
            {"id": "c2", "feature": "boq", "provider": "anthropic", "tier": "job",
             "ok": True, "error_kind": None, "duration_ms": 300, "created_at": day_a},
            {"id": "c3", "feature": "boq", "provider": "anthropic", "tier": "job",
             "ok": False, "error_kind": "timeout", "duration_ms": 200, "created_at": day_a},
            {"id": "c4", "feature": "proposal", "provider": "openai", "tier": "interactive",
             "ok": True, "error_kind": None, "duration_ms": None, "created_at": day_b},
            {"id": "c5", "feature": "proposal", "provider": "openai", "tier": "interactive",
             "ok": False, "error_kind": "timeout", "duration_ms": 50, "created_at": day_b},
        ],
        "llm_jobs": [
            _job_row(target_id="t1"),
            _job_row(target_id="t2"),
            _job_row(target_id="t3", status="running"),
            _job_row(target_id="t4", status="failed"),
        ],
    })
    _install(monkeypatch, db)
    out = llm_monitor.summary(days=30, _=_dev())
    assert out["truncated"] is False
    assert out["totals"] == {"calls": 5, "failures": 2, "success_rate": 0.6}
    assert out["by_day"] == [
        {"date": _la_day(day_a), "calls": 3, "failures": 1},
        {"date": _la_day(day_b), "calls": 2, "failures": 1},
    ]
    assert out["by_feature"] == [
        {"feature": "boq", "calls": 3, "failures": 1, "avg_duration_ms": 200},
        {"feature": "proposal", "calls": 2, "failures": 1, "avg_duration_ms": 50},
    ]
    assert out["by_error_kind"] == [{"kind": "timeout", "count": 2}]
    assert out["by_tier"] == [
        {"tier": "interactive", "calls": 2, "failures": 1},
        {"tier": "job", "calls": 3, "failures": 1},
    ]
    assert out["queue"] == {
        "queued": 2,
        "running": 1,
        "jobs_by_status": {"queued": 2, "running": 1, "failed": 1},
    }


# ── failures ─────────────────────────────────────────────────────────────


def test_failures_returns_failed_calls_and_terminal_jobs_with_labels(monkeypatch):
    recent = _stamp(1)
    db = FakeDB({
        "llm_call_log": [
            {"id": "c-ok", "feature": "boq", "provider": "anthropic", "model": "m",
             "tier": "job", "job_id": None, "ok": True, "error_kind": None,
             "error": None, "duration_ms": 10, "created_at": recent},
            {"id": "c-bad", "feature": "boq", "provider": "anthropic", "model": "m",
             "tier": "job", "job_id": "j1", "ok": False, "error_kind": "timeout",
             "error": "timed out", "duration_ms": 10, "created_at": recent},
        ],
        "llm_jobs": [
            _job_row(id="j1", status="failed", project_id="p1", error_kind="timeout",
                     last_error="timed out", created_at=recent),
            _job_row(id="j2", target_id="t2", status="succeeded", created_at=recent),
        ],
        "projects": [{"id": "p1", "number": "26-104", "name": "Riverside Plaza"}],
    })
    _install(monkeypatch, db)
    out = llm_monitor.failures(days=7, limit=100, _=_dev())
    assert [c["id"] for c in out["calls"]] == ["c-bad"]
    assert len(out["jobs"]) == 1
    job = out["jobs"][0]
    assert job["id"] == "j1" and job["status"] == "failed"
    assert job["project_number"] == "26-104"
    assert job["project_name"] == "Riverside Plaza"
    assert job["error_kind"] == "timeout"


# ── queue state ──────────────────────────────────────────────────────────


def test_queue_state_positions_only_queued_rows_in_claim_order(monkeypatch):
    base = datetime.now(timezone.utc)
    db = FakeDB({
        "llm_jobs": [
            _job_row(id="qa", target_id="ta", priority=100,
                     created_at=(base - timedelta(minutes=2)).isoformat()),
            _job_row(id="qb", target_id="tb", priority=50,
                     created_at=(base - timedelta(minutes=1)).isoformat()),
            _job_row(id="rc", target_id="tc", priority=10, status="running",
                     created_at=base.isoformat()),
        ],
    })
    _install(monkeypatch, db)
    out = llm_monitor.queue_state(_=_dev())
    got = [(j["id"], j["position"]) for j in out["jobs"]]
    # Claim order is (priority, created_at); the running row sorts first but
    # never takes a queue position.
    assert got == [("rc", None), ("qb", 1), ("qa", 2)]


# ── retry ────────────────────────────────────────────────────────────────


def _spec_recorder(monkeypatch, current_status=None):
    marks = []
    spec = lq._JobSpec(
        "boq",
        lambda p: None,
        lambda t, f: marks.append((t, f)),
        current_status or (lambda t: None),
    )
    monkeypatch.setattr(lq, "_spec", lambda jt: spec)
    return marks


# Real uuid shapes: _job_or_404 rejects malformed ids up front (clean 404
# instead of a PostgREST cast 500), so action tests must use valid uuids.
FAILED_ID = "11111111-1111-4111-8111-111111111111"
QUEUED_ID = "22222222-2222-4222-8222-222222222222"
RUNNING_ID = "33333333-3333-4333-8333-333333333333"


def test_retry_failed_job_returns_a_fresh_queued_job(monkeypatch):
    db = FakeDB({
        "llm_jobs": [
            _job_row(id=FAILED_ID, status="failed", attempts=6, error_kind="timeout"),
        ],
    })
    audits = _install(monkeypatch, db)
    marks = _spec_recorder(monkeypatch)
    out = llm_monitor.retry_job(FAILED_ID, user=_dev())
    assert out["id"] != FAILED_ID
    assert out["status"] == "queued" and out["attempts"] == 0
    assert len(db.tables["llm_jobs"]) == 2  # the failed row stays as history
    assert marks == [("a1", {"status": "pending", "error": None})]
    assert audits and audits[0][1] == "llm_monitor.retry"


def test_retry_rejects_a_queued_job_with_409(monkeypatch):
    db = FakeDB({"llm_jobs": [_job_row(id=QUEUED_ID, status="queued")]})
    _install(monkeypatch, db)
    with pytest.raises(HTTPException) as exc:
        llm_monitor.retry_job(QUEUED_ID, user=_dev())
    assert exc.value.status_code == 409
    assert len(db.tables["llm_jobs"]) == 1


def test_retry_refuses_when_domain_row_has_since_completed(monkeypatch):
    # A stale Retry must not clobber a target that later succeeded (a fresh
    # run or a manual override): 409, nothing enqueued, domain untouched.
    db = FakeDB({
        "llm_jobs": [
            _job_row(id=FAILED_ID, status="failed", attempts=6, error_kind="timeout"),
        ],
    })
    _install(monkeypatch, db)
    marks = _spec_recorder(monkeypatch, current_status=lambda t: "done")
    with pytest.raises(HTTPException) as exc:
        llm_monitor.retry_job(FAILED_ID, user=_dev())
    assert exc.value.status_code == 409
    assert len(db.tables["llm_jobs"]) == 1
    assert marks == []


def test_retry_refuses_when_a_newer_job_supersedes(monkeypatch):
    newer = _job_row(id=QUEUED_ID, status="failed", attempts=6)
    newer["created_at"] = "2026-08-03T13:00:00+00:00"
    older = _job_row(id=FAILED_ID, status="failed", attempts=6)
    older["created_at"] = "2026-08-03T12:00:00+00:00"
    db = FakeDB({"llm_jobs": [older, newer]})
    _install(monkeypatch, db)
    marks = _spec_recorder(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        llm_monitor.retry_job(FAILED_ID, user=_dev())
    assert exc.value.status_code == 409
    assert marks == []


def test_retry_unknown_job_is_404(monkeypatch):
    _install(monkeypatch, FakeDB())
    with pytest.raises(HTTPException) as exc:
        llm_monitor.retry_job("44444444-4444-4444-8444-444444444444", user=_dev())
    assert exc.value.status_code == 404


def test_retry_malformed_job_id_is_404_not_500(monkeypatch):
    _install(monkeypatch, FakeDB())
    with pytest.raises(HTTPException) as exc:
        llm_monitor.retry_job("not-a-uuid", user=_dev())
    assert exc.value.status_code == 404


# ── cancel ───────────────────────────────────────────────────────────────


def test_cancel_queued_job_cancels_and_marks_domain(monkeypatch):
    db = FakeDB({"llm_jobs": [_job_row(id=QUEUED_ID, status="queued")]})
    _install(monkeypatch, db)
    marks = _spec_recorder(monkeypatch)
    out = llm_monitor.cancel_job(QUEUED_ID, user=_dev())
    assert out["status"] == "canceled"
    row = db.tables["llm_jobs"][0]
    assert row["status"] == "canceled"
    assert row["last_error"] == lq._CANCELED_MESSAGE
    assert marks == [("a1", {"status": "failed", "error": lq._CANCELED_MESSAGE})]


def test_cancel_running_job_is_409_and_untouched(monkeypatch):
    row = _job_row(id=RUNNING_ID, status="running")
    row["lease_expires_at"] = "2999-01-01T00:00:00+00:00"  # live lease, no zombie
    db = FakeDB({"llm_jobs": [row]})
    _install(monkeypatch, db)
    marks = _spec_recorder(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        llm_monitor.cancel_job(RUNNING_ID, user=_dev())
    assert exc.value.status_code == 409
    assert db.tables["llm_jobs"][0]["status"] == "running"
    assert marks == []

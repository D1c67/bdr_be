"""Durable LLM job queue (migration 0094) and its supporting layers.

Invariants pinned here:

  llm_errors      classify() buckets every failure shape into a stable kind
                  (gate busy, bad output, connection vs timeout causes, HTTP
                  status codes, quota exhaustion); only the documented kinds
                  are transient; user_message() names "the local AI server"
                  vs "the AI provider" from the model label and passes the
                  raiser's text through for invalid_output/bad_input.
  llm_gate        slot() caps in-flight calls per provider and reserves
                  interactive headroom background tiers can never take;
                  admission timeout raises LlmBusy; tier()/job_id ride a
                  contextvar with clean nesting.
  llm._guarded    every complete_* call logs exactly one llm_call_log row
                  (ok or failure, with the active tier) and re-raises;
                  log_call stores the sanitized message plus the job id and
                  never raises itself.
  llm_queue       enqueue sets max_attempts = 1 + len(retry delays) and
                  collapses duplicate-active inserts (23505) onto the
                  existing job; the claim RPC only takes due queued jobs in
                  (priority, created_at) order and spends an attempt; the
                  worker requeues transient failures on the exact backoff
                  ladder (10/20/45/90/180s), fails permanent errors
                  immediately, and the sweep requeues or terminally fails
                  expired leases; poll_info/cancel/requeue_terminal behave
                  as the monitor and pollers expect.
  routers         start endpoints dispatch to the queue when enabled (and
                  BackgroundTasks when not), a live queue job blocks a
                  second start and keeps a stale row from being failed, and
                  the poll endpoints attach queue detail to pending rows.
  service split   execute() raises so the queue owns the terminal mark
                  (row stays "running"); run_* wrappers are the only place
                  failures are terminal-marked inline.
  rfp_ingest      the sandbox job type rides the same table but never an LLM
                  slot: the claim RPC's job_types filter keeps the two
                  passes disjoint, each with its own capacity (a second
                  sandbox run waits while one runs; the LLM pass never
                  takes a sandbox row; the pass is skipped while the flag is
                  off); renew_lease / requeue_self CAS through the same
                  (claimed_by, attempts) fence and are no-ops with no
                  current job; the spec's "sandbox" label stands in for an
                  LLM model and its own error_message keeps every sentence
                  the queue stores sandbox-authored (an exception the sandbox
                  never declared is recorded as interrupted, not as a problem
                  with the AI provider); self-classifying exceptions requeue
                  (infrastructure) or fail (bad_input) with their own text;
                  LeaseLost writes nothing; an error inside the failure
                  handler still forces a terminal state; the hourly prune
                  slot runs the sandbox retention behind the flag.

Supabase is faked in-memory per house convention; the fake enforces the
partial unique index on active (job_type, target_id) pairs and implements
claim_llm_jobs in Python mirroring the SQL semantics in 0094 (plus the
job_types filter 0119 adds). app.services.rfp_ingest is written by a later
package, so a stand-in module with exactly the surface llm_queue wires is
installed in sys.modules for the sandbox tests.
"""

import contextlib
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException

from app.core.config import Settings
from app.core.deps import CurrentUser
from app.core.ratelimit import ai_rate_limit
from app.core.roles import Role
from app.models.schemas import BoqAnalysisStart, ProposalGenerateIn
from app.routers import boq_analysis as boq_router
from app.routers import general_material as gm_router
from app.routers import proposals as proposals_router
from app.sandbox import protocol
from app.services import boq_extraction as bx
from app.services import llm, llm_errors, llm_gate
from app.services import llm_queue as lq
from app.services import proposal_scope as ps

NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)


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
        self._on_conflict = None
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

    def upsert(self, payload, on_conflict=None, **k):
        self._op, self._payload, self._on_conflict = "upsert", payload, on_conflict
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
        if self._op == "upsert":
            row = dict(self._payload)
            key = self._on_conflict
            existing = next((r for r in rows if key and r.get(key) == row.get(key)), None)
            if existing:
                existing.update(row)
                return SimpleNamespace(data=[dict(existing)])
            row.setdefault("id", uuid.uuid4().hex)
            rows.append(row)
            return SimpleNamespace(data=[dict(row)])
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


class _Rpc:
    """Python mirror of the claim_llm_jobs SQL in 0094 (+ the job_types filter
    0119 adds, `job_types is null or job_type = any(job_types)`): claim up to
    max_jobs queued jobs whose next_attempt_at <= now, ordered (priority,
    created_at); set running/claimed_by/lease, spend an attempt, coalesce
    started_at."""

    def __init__(self, db, params):
        self.db = db
        self.params = params

    def execute(self):
        now = self.db.now
        rows = self.db.tables.setdefault("llm_jobs", [])
        job_types = self.params.get("job_types")
        due = [
            r for r in rows
            if r.get("status") == "queued"
            and _ts(r.get("next_attempt_at")) <= now
            and (job_types is None or r.get("job_type") in job_types)
        ]
        due.sort(key=lambda r: (r.get("priority", 100), r.get("created_at") or ""))
        claimed = []
        for r in due[: self.params["max_jobs"]]:
            r["status"] = "running"
            r["claimed_by"] = self.params["worker_id"]
            r["lease_expires_at"] = (
                now + timedelta(seconds=self.params["lease_seconds"])
            ).isoformat()
            r["attempts"] = (r.get("attempts") or 0) + 1
            r["started_at"] = r.get("started_at") or now.isoformat()
            claimed.append(dict(r))
        return SimpleNamespace(data=claimed)


class FakeDB:
    def __init__(self, tables=None, now=NOW):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self.now = now
        self._seq = 0
        self.rpc_calls = []  # every claim's params, so tests can see each pass

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params):
        assert name == "claim_llm_jobs"
        self.rpc_calls.append(dict(params))
        return _Rpc(self, params)

    def next_created_at(self):
        self._seq += 1
        return (self.now + timedelta(seconds=self._seq)).isoformat()


def _user(role=Role.ESTIMATING_ADMIN, uid="u1", is_dev=False):
    return CurrentUser(id=uid, email="e@g3.com", role=role, is_active=True, is_dev=is_dev)


def _settings(**over):
    return Settings(_env_file=None, **over)


# ── llm_errors: classify ─────────────────────────────────────────────────


class _StatusError(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


class _QuotaError(Exception):
    code = "insufficient_quota"


def test_classify_buckets_known_exception_types():
    assert llm_errors.classify(llm_gate.LlmBusy("busy")) == "overloaded"
    assert llm_errors.classify(llm.LlmBadOutput("cut off")) == "invalid_output"
    assert llm_errors.classify(ValueError("scanned PDF")) == "bad_input"
    assert llm_errors.classify(llm.SelfHostedUnreachable("down")) == "unreachable"
    assert llm_errors.classify(RuntimeError("???")) == "unknown"


def test_classify_self_hosted_timeout_cause_wins_over_unreachable():
    try:
        raise llm.SelfHostedUnreachable("did not respond") from httpx.ReadTimeout("slow")
    except llm.SelfHostedUnreachable as exc:
        assert llm_errors.classify(exc) == "timeout"


def test_classify_by_provider_status_code():
    assert llm_errors.classify(_StatusError("slow down", 429)) == "rate_limited"
    assert llm_errors.classify(_StatusError("unavailable", 503)) == "overloaded"
    assert llm_errors.classify(_StatusError("anthropic overloaded", 529)) == "overloaded"
    assert llm_errors.classify(_StatusError("boom", 500)) == "server_error"
    assert llm_errors.classify(_StatusError("bad key", 401)) == "unauthorized"
    assert llm_errors.classify(_StatusError("bad request", 400)) == "bad_input"


def test_classify_insufficient_quota_code_is_out_of_tokens():
    assert llm_errors.classify(_QuotaError("Error code: 429")) == "out_of_tokens"


def test_transient_kinds_are_exactly_the_retryable_ones():
    for kind in (
        "unreachable", "timeout", "overloaded", "rate_limited",
        "server_error", "invalid_output", "unknown",
    ):
        assert llm_errors.is_transient_kind(kind), kind
    for kind in ("out_of_tokens", "not_configured", "unauthorized", "bad_input"):
        assert not llm_errors.is_transient_kind(kind), kind


def test_user_message_names_the_service_from_the_model_label():
    exc = llm.SelfHostedUnreachable("down")
    assert "local AI server" in llm_errors.user_message(exc, "self-hosted:qwen")
    assert "AI provider" in llm_errors.user_message(exc, "gpt-5.4")


def test_user_message_out_of_tokens_names_model_and_it_director():
    msg = llm_errors.user_message(_QuotaError("Error code: 429"), "claude-opus-4-8")
    assert "claude-opus-4-8" in msg
    assert "IT Director" in msg


def test_user_message_passes_raiser_text_through_for_output_and_input_kinds():
    assert llm_errors.user_message(llm.LlmBadOutput("custom words"), "m") == "custom words"
    assert (
        llm_errors.user_message(ValueError("BOQ file is too large."), "m")
        == "BOQ file is too large."
    )


# ── llm_gate: slot + tier ────────────────────────────────────────────────


@pytest.fixture
def clean_gates():
    llm_gate.reset_gates()
    yield
    llm_gate.reset_gates()


def _gate_settings(**over):
    base = dict(
        llm_interactive_wait_seconds=0.05,
        llm_background_wait_seconds=0.05,
        llm_max_concurrent_self_hosted=2,
        llm_interactive_reserved_slots=1,
    )
    base.update(over)
    return _settings(**base)


def test_slot_reserves_interactive_headroom(clean_gates):
    s = _gate_settings()  # total 2, background pool 1
    with contextlib.ExitStack() as stack:
        stack.enter_context(llm_gate.slot("self_hosted", llm_gate.TIER_JOB, s))
        # The single background permit is taken: a second background caller
        # is refused while an interactive caller still gets the reserved slot.
        with pytest.raises(llm_gate.LlmBusy):
            with llm_gate.slot("self_hosted", llm_gate.TIER_PIPELINE, s):
                pass
        with llm_gate.slot("self_hosted", llm_gate.TIER_INTERACTIVE, s):
            pass


def test_slot_interactive_busy_when_total_exhausted_and_released_on_exit(clean_gates):
    s = _gate_settings()
    with contextlib.ExitStack() as stack:
        stack.enter_context(llm_gate.slot("self_hosted", llm_gate.TIER_JOB, s))
        stack.enter_context(llm_gate.slot("self_hosted", llm_gate.TIER_INTERACTIVE, s))
        with pytest.raises(llm_gate.LlmBusy):
            with llm_gate.slot("self_hosted", llm_gate.TIER_INTERACTIVE, s):
                pass
    # Both permits were released on exit; the gate admits again.
    with llm_gate.slot("self_hosted", llm_gate.TIER_INTERACTIVE, s):
        pass
    with llm_gate.slot("self_hosted", llm_gate.TIER_PIPELINE, s):
        pass


def test_tier_defaults_interactive_and_nests_cleanly():
    assert llm_gate.current_tier() == llm_gate.TIER_INTERACTIVE
    assert llm_gate.current_job_id() is None
    with llm_gate.tier(llm_gate.TIER_PIPELINE):
        assert llm_gate.current_tier() == "pipeline"
        with llm_gate.tier(llm_gate.TIER_JOB, job_id="j1"):
            assert llm_gate.current_tier() == "job"
            assert llm_gate.current_job_id() == "j1"
        assert llm_gate.current_tier() == "pipeline"
        assert llm_gate.current_job_id() is None
    assert llm_gate.current_tier() == llm_gate.TIER_INTERACTIVE


# ── llm._guarded: gate + accounting around every call ────────────────────


def _sh_settings(**over):
    base = dict(
        full_self_hosted_llms_enabled=True,
        self_hosted_llm_local_base_url="http://localhost:11434/v1",
        self_hosted_email_match_model="local-m",
    )
    base.update(over)
    return _settings(**base)


def _fake_sh_client(content='{"ok": true}', raise_exc=None):
    """OpenAI-compatible stub exposing only chat.completions (the self-hosted
    wire); mirrors the test_llm_routing helper."""
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if raise_exc is not None:
            raise raise_exc
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content), finish_reason="stop"
                )
            ]
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client.with_options = lambda **kw: client
    return client, calls


def test_guarded_success_logs_ok_with_tier_and_route(monkeypatch, clean_gates):
    logged = []
    monkeypatch.setattr(llm_gate, "log_call", lambda **kw: logged.append(kw))
    client, _ = _fake_sh_client()
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    out = llm.complete_json(
        "email_match",
        system="s",
        messages=[{"role": "user", "content": "x"}],
        settings=_sh_settings(),
    )
    assert out == {"ok": True}
    assert len(logged) == 1
    row = logged[0]
    assert row["ok"] is True and row["error"] is None
    assert row["tier_name"] == "interactive"
    assert row["provider"] == "self_hosted" and row["model"] == "local-m"
    assert row["feature"] == "email_match"


def test_guarded_failure_logs_and_reraises(monkeypatch, clean_gates):
    logged = []
    monkeypatch.setattr(llm_gate, "log_call", lambda **kw: logged.append(kw))
    boom = RuntimeError("wire exploded")
    client, _ = _fake_sh_client(raise_exc=boom)
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    with pytest.raises(RuntimeError, match="wire exploded"):
        llm.complete_json(
            "email_match",
            system="s",
            messages=[{"role": "user", "content": "x"}],
            settings=_sh_settings(),
        )
    assert len(logged) == 1
    assert logged[0]["ok"] is False and logged[0]["error"] is boom


def test_job_tier_flows_into_the_call_log(monkeypatch, clean_gates):
    logged = []
    monkeypatch.setattr(llm_gate, "log_call", lambda **kw: logged.append(kw))
    client, _ = _fake_sh_client()
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    with llm_gate.tier(llm_gate.TIER_JOB, job_id="j1"):
        assert llm_gate.current_job_id() == "j1"
        llm.complete_json(
            "email_match",
            system="s",
            messages=[{"role": "user", "content": "x"}],
            settings=_sh_settings(),
        )
    assert logged[0]["tier_name"] == "job"


def test_log_call_writes_sanitized_row_with_job_id(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr("app.core.supabase_client.get_supabase", lambda: db)
    with llm_gate.tier(llm_gate.TIER_JOB, job_id="j-77"):
        llm_gate.log_call(
            feature="boq",
            provider="self_hosted",
            model="qwen",
            tier_name="job",
            ok=False,
            error=llm.SelfHostedUnreachable("raw SDK text with http://10.0.0.5:8000"),
            duration_ms=5,
        )
    row = db.tables["llm_call_log"][0]
    assert row["job_id"] == "j-77" and row["tier"] == "job"
    assert row["error_kind"] == "unreachable"
    # Sanitized user message, never str(exc): the endpoint must not leak.
    assert "local AI server" in row["error"]
    assert "10.0.0.5" not in row["error"]


def test_log_call_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("app.core.supabase_client.get_supabase", boom)
    llm_gate.log_call(
        feature="boq", provider="openai", model="m", tier_name="interactive",
        ok=True, error=None, duration_ms=1,
    )


# ── llm_queue: enqueue ───────────────────────────────────────────────────


def _queue_env(monkeypatch, tables=None, settings=None):
    db = FakeDB(tables)
    monkeypatch.setattr(lq, "get_supabase", lambda: db)
    monkeypatch.setattr(lq, "get_settings", lambda: settings or _settings())
    return db


def test_enqueue_sets_max_attempts_and_feature(monkeypatch):
    db = _queue_env(monkeypatch)
    job = lq.enqueue(
        lq.JOB_BOQ, target_id="a1", project_id="p1",
        payload={"analysis_id": "a1"}, created_by="u1",
    )
    row = db.tables["llm_jobs"][0]
    assert job["id"] == row["id"]
    assert row["max_attempts"] == 6  # 1 + len([10,20,45,90,180])
    assert row["feature"] == "boq"
    assert row["status"] == "queued" and row["attempts"] == 0
    assert lq.enqueue(lq.JOB_GENERAL_MATERIAL, target_id="p2")["feature"] == "estimate"
    assert lq.enqueue(lq.JOB_PROPOSAL, target_id="d1")["feature"] == "proposal"


def test_enqueue_duplicate_active_returns_the_existing_job(monkeypatch):
    db = _queue_env(monkeypatch)
    first = lq.enqueue(lq.JOB_BOQ, target_id="a1")
    second = lq.enqueue(lq.JOB_BOQ, target_id="a1")
    assert second["id"] == first["id"]
    assert len(db.tables["llm_jobs"]) == 1


# ── llm_queue: claim semantics ───────────────────────────────────────────


def test_claim_takes_only_due_jobs_in_priority_then_created_order(monkeypatch):
    db = _queue_env(monkeypatch)
    a = lq.enqueue(lq.JOB_BOQ, target_id="a")               # priority 100, created first
    b = lq.enqueue(lq.JOB_BOQ, target_id="b", priority=50)  # lower = runs first
    c = lq.enqueue(lq.JOB_BOQ, target_id="c")
    next(r for r in db.tables["llm_jobs"] if r["id"] == c["id"])["next_attempt_at"] = (
        NOW + timedelta(seconds=60)
    ).isoformat()  # not due yet
    claimed = lq._claim(_settings(), 10)
    assert [j["id"] for j in claimed] == [b["id"], a["id"]]
    for j in claimed:
        assert j["status"] == "running"
        assert j["attempts"] == 1
        assert j["claimed_by"] == lq._WORKER_TOKEN
        assert j["lease_expires_at"] == (NOW + timedelta(seconds=900)).isoformat()
        assert j["started_at"] == NOW.isoformat()
    still = next(r for r in db.tables["llm_jobs"] if r["id"] == c["id"])
    assert still["status"] == "queued" and still["attempts"] == 0


def test_claim_respects_max_jobs(monkeypatch):
    db = _queue_env(monkeypatch)
    for t in ("a", "b", "c"):
        lq.enqueue(lq.JOB_BOQ, target_id=t)
    claimed = lq._claim(_settings(), 2)
    assert len(claimed) == 2
    assert sum(1 for r in db.tables["llm_jobs"] if r["status"] == "queued") == 1


def test_reclaim_spends_another_attempt_and_keeps_started_at(monkeypatch):
    db = _queue_env(monkeypatch)
    lq.enqueue(lq.JOB_BOQ, target_id="a")
    [first] = lq._claim(_settings(), 1)
    row = db.tables["llm_jobs"][0]
    row.update(status="queued", next_attempt_at=NOW.isoformat(), claimed_by=None)
    [second] = lq._claim(_settings(), 1)
    assert second["attempts"] == 2
    assert second["started_at"] == first["started_at"]  # coalesce keeps the first claim


# ── llm_queue: _execute ──────────────────────────────────────────────────


def _record_spec(run=None, current_status=None):
    runs, marks = [], []

    def default_run(payload):
        runs.append(payload)

    spec = lq._JobSpec(
        "boq",
        run or default_run,
        lambda t, f: marks.append((t, f)),
        current_status or (lambda t: None),
    )
    return spec, runs, marks


def _raiser(exc):
    def run(payload):
        raise exc

    return run


def _running_row(
    db, *, job_id, target, attempts, lease=None, max_attempts=6,
    job_type=lq.JOB_BOQ, payload=None,
):
    row = {
        "id": job_id, "job_type": job_type, "feature": lq._FEATURE_BY_TYPE[job_type],
        "target_id": target, "project_id": None,
        "payload": payload or {"analysis_id": target}, "status": "running",
        "priority": 100, "attempts": attempts, "max_attempts": max_attempts,
        "next_attempt_at": None, "claimed_by": "w1",
        "lease_expires_at": lease or (NOW + timedelta(seconds=900)).isoformat(),
        "error_kind": None, "last_error": None, "started_at": NOW.isoformat(),
        "finished_at": None, "created_at": db.next_created_at(),
    }
    db.tables.setdefault("llm_jobs", []).append(row)
    return dict(row)


def test_execute_success_marks_job_succeeded(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    lq.enqueue(lq.JOB_BOQ, target_id="a1", payload={"analysis_id": "a1"})
    [job] = db.rpc(
        "claim_llm_jobs", {"worker_id": "w1", "lease_seconds": 900, "max_jobs": 1}
    ).execute().data
    spec, runs, marks = _record_spec()
    monkeypatch.setattr(lq, "_spec", lambda jt: spec)
    lq._execute(job)
    assert runs == [{"analysis_id": "a1"}]
    row = db.tables["llm_jobs"][0]
    assert row["status"] == "succeeded"
    assert row["finished_at"] == NOW.isoformat()
    assert row["lease_expires_at"] is None and row["claimed_by"] is None
    assert marks == []  # the runner owns its own done mark on success


def test_transient_failure_requeues_with_first_delay_and_domain_pending(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    job = _running_row(db, job_id="j1", target="a1", attempts=1)
    spec, _, marks = _record_spec(run=_raiser(llm.LlmBadOutput("bad")))
    monkeypatch.setattr(lq, "_spec", lambda jt: spec)
    lq._execute(job)
    row = db.tables["llm_jobs"][0]
    assert row["status"] == "queued"
    assert row["next_attempt_at"] == (NOW + timedelta(seconds=10)).isoformat()
    assert row["error_kind"] == "invalid_output"
    assert row["last_error"] == "bad"
    assert row["claimed_by"] is None and row["lease_expires_at"] is None
    assert marks == [("a1", {"status": "pending", "error": None})]


def test_backoff_ladder_drives_attempts_one_through_five(monkeypatch):
    for attempt, delay in zip(range(1, 6), [10, 20, 45, 90, 180]):
        db = _queue_env(monkeypatch)
        monkeypatch.setattr(lq, "_now", lambda: NOW)
        job = _running_row(db, job_id=f"j{attempt}", target="a1", attempts=attempt)
        spec, _, _ = _record_spec(run=_raiser(llm.LlmBadOutput("bad")))
        monkeypatch.setattr(lq, "_spec", lambda jt: spec)
        lq._execute(job)
        row = db.tables["llm_jobs"][0]
        assert row["status"] == "queued", f"attempt {attempt}"
        assert row["next_attempt_at"] == (NOW + timedelta(seconds=delay)).isoformat(), (
            f"attempt {attempt} should wait {delay}s"
        )


def test_transient_failure_after_last_delay_fails_terminally(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    job = _running_row(db, job_id="j6", target="a1", attempts=6)
    spec, _, marks = _record_spec(run=_raiser(llm.LlmBadOutput("bad")))
    monkeypatch.setattr(lq, "_spec", lambda jt: spec)
    lq._execute(job)
    row = db.tables["llm_jobs"][0]
    assert row["status"] == "failed"
    assert row["finished_at"] == NOW.isoformat()
    assert row["error_kind"] == "invalid_output"
    assert row["last_error"] == "bad"
    assert marks == [("a1", {"status": "failed", "error": "bad (failed after 6 attempts)"})]


def test_permanent_failure_fails_immediately_without_attempt_suffix(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    message = "Estimate file is too large to analyze (60MB; limit 50MB)."
    job = _running_row(db, job_id="j1", target="a1", attempts=1)
    spec, _, marks = _record_spec(run=_raiser(ValueError(message)))
    monkeypatch.setattr(lq, "_spec", lambda jt: spec)
    lq._execute(job)
    row = db.tables["llm_jobs"][0]
    assert row["status"] == "failed"
    assert row["error_kind"] == "bad_input"
    assert row["last_error"] == message
    assert marks == [("a1", {"status": "failed", "error": message})]
    assert "(failed after" not in marks[0][1]["error"]


def test_out_of_tokens_fails_immediately_despite_retries_remaining(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    job = _running_row(db, job_id="j1", target="a1", attempts=1)
    spec, _, marks = _record_spec(run=_raiser(_QuotaError("Error code: 429")))
    monkeypatch.setattr(lq, "_spec", lambda jt: spec)
    lq._execute(job)
    row = db.tables["llm_jobs"][0]
    assert row["status"] == "failed"
    assert row["error_kind"] == "out_of_tokens"
    assert "IT Director" in row["last_error"]
    assert "claude-opus-4-8" in row["last_error"]  # active model for the boq feature
    assert marks[0][1]["status"] == "failed"


# ── llm_queue: sweep ─────────────────────────────────────────────────────


def test_sweep_requeues_or_fails_expired_leases_and_skips_live_ones(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    monkeypatch.setattr(lq, "_last_prune", time.monotonic())  # keep the prune out
    past = (NOW - timedelta(seconds=5)).isoformat()
    future = (NOW + timedelta(seconds=600)).isoformat()
    _running_row(db, job_id="j1", target="t1", attempts=1, lease=past)
    _running_row(db, job_id="j2", target="t2", attempts=6, lease=past)
    _running_row(db, job_id="j3", target="t3", attempts=1, lease=future)
    spec, _, marks = _record_spec()
    monkeypatch.setattr(lq, "_spec", lambda jt: spec)
    lq._sweep(_settings())
    rows = {r["id"]: r for r in db.tables["llm_jobs"]}
    j1 = rows["j1"]
    assert j1["status"] == "queued"
    assert j1["next_attempt_at"] == NOW.isoformat()  # immediately claimable again
    assert j1["error_kind"] == "interrupted"
    assert j1["last_error"] == lq._INTERRUPTED_MESSAGE
    assert j1["claimed_by"] is None and j1["lease_expires_at"] is None
    j2 = rows["j2"]
    assert j2["status"] == "failed"
    assert j2["error_kind"] == "interrupted"
    assert j2["last_error"] == lq._INTERRUPTED_FINAL_MESSAGE
    assert j2["finished_at"] == NOW.isoformat()
    j3 = rows["j3"]
    assert j3["status"] == "running" and j3["error_kind"] is None
    assert marks == [
        ("t1", {"status": "pending", "error": None}),
        ("t2", {"status": "failed", "error": lq._INTERRUPTED_FINAL_MESSAGE}),
    ]


def test_sweep_prunes_only_terminal_rows_past_retention(monkeypatch):
    old = "2026-01-01T00:00:00+00:00"  # past the 90-day default retention from NOW
    db = _queue_env(
        monkeypatch,
        tables={
            "llm_call_log": [
                {"id": "c-old", "created_at": old},
                {"id": "c-new", "created_at": NOW.isoformat()},
            ],
            "llm_jobs": [
                {"id": "j-old-done", "status": "succeeded", "created_at": old},
                {"id": "j-old-queued", "status": "queued", "created_at": old},
                {"id": "j-new-failed", "status": "failed", "created_at": NOW.isoformat()},
            ],
        },
    )
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    # A negative sentinel, not 0.0: time.monotonic() counts from boot, so on a
    # recently-woken host 0.0 can be within the prune interval and skip the prune.
    monkeypatch.setattr(lq, "_last_prune", -lq._PRUNE_EVERY_SECONDS)
    lq._sweep(_settings())
    assert [r["id"] for r in db.tables["llm_call_log"]] == ["c-new"]
    kept = {r["id"] for r in db.tables["llm_jobs"]}
    assert kept == {"j-old-queued", "j-new-failed"}  # active rows never pruned


# ── llm_queue: poll_info ─────────────────────────────────────────────────


def test_poll_info_none_when_target_never_queued(monkeypatch):
    _queue_env(monkeypatch)
    assert lq.poll_info(lq.JOB_BOQ, "a1") is None


def test_poll_info_reports_queue_position_and_retrying(monkeypatch):
    db = _queue_env(monkeypatch)
    lq.enqueue(lq.JOB_BOQ, target_id="a1")
    lq.enqueue(lq.JOB_BOQ, target_id="a2")
    info = lq.poll_info(lq.JOB_BOQ, "a2")
    assert info["state"] == "queued"
    assert info["position"] == 2  # behind the earlier-created job
    assert info["retrying"] is False
    assert lq.poll_info(lq.JOB_BOQ, "a1")["position"] == 1
    # A queued job that has already burned attempts is waiting out a retry.
    next(r for r in db.tables["llm_jobs"] if r["target_id"] == "a1")["attempts"] = 2
    assert lq.poll_info(lq.JOB_BOQ, "a1")["retrying"] is True


def test_poll_info_running_job_has_no_position(monkeypatch):
    _queue_env(monkeypatch)
    lq.enqueue(lq.JOB_BOQ, target_id="a1")
    lq._claim(_settings(), 1)
    info = lq.poll_info(lq.JOB_BOQ, "a1")
    assert info["state"] == "running"
    assert info["position"] is None and info["next_attempt_at"] is None


# ── llm_queue: cancel + requeue_terminal ─────────────────────────────────


def test_cancel_queued_job_marks_domain_failed(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    job = lq.enqueue(lq.JOB_BOQ, target_id="a1")
    spec, _, marks = _record_spec()
    monkeypatch.setattr(lq, "_spec", lambda jt: spec)
    out = lq.cancel(job["id"])
    assert out["status"] == "canceled"
    row = db.tables["llm_jobs"][0]
    assert row["status"] == "canceled"
    assert row["last_error"] == lq._CANCELED_MESSAGE
    assert row["finished_at"] == NOW.isoformat()
    assert marks == [("a1", {"status": "failed", "error": lq._CANCELED_MESSAGE})]


def test_cancel_running_job_with_live_lease_returns_none_and_leaves_it_alone(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)  # lease (NOW+900s) is healthy
    job = lq.enqueue(lq.JOB_BOQ, target_id="a1")
    lq._claim(_settings(), 1)
    spec, _, marks = _record_spec()
    monkeypatch.setattr(lq, "_spec", lambda jt: spec)
    assert lq.cancel(job["id"]) is None
    assert db.tables["llm_jobs"][0]["status"] == "running"
    assert marks == []


def test_cancel_running_zombie_with_expired_lease_cancels(monkeypatch):
    # A running job whose lease already expired has no live worker; the dev
    # can clear it from the monitor instead of waiting for the sweep.
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW + timedelta(seconds=901))
    job = lq.enqueue(lq.JOB_BOQ, target_id="a1")
    lq._claim(_settings(), 1)  # lease = NOW + 900s, now already past it
    spec, _, marks = _record_spec()
    monkeypatch.setattr(lq, "_spec", lambda jt: spec)
    canceled = lq.cancel(job["id"])
    assert canceled is not None and canceled["status"] == "canceled"
    row = db.tables["llm_jobs"][0]
    assert row["status"] == "canceled"
    assert row["claimed_by"] is None and row["lease_expires_at"] is None
    assert marks == [("a1", {"status": "failed", "error": lq._CANCELED_MESSAGE})]


def test_requeue_terminal_creates_a_fresh_job_and_repends_domain(monkeypatch):
    db = _queue_env(monkeypatch)
    failed = {
        "id": "f1", "job_type": lq.JOB_BOQ, "feature": "boq", "target_id": "a1",
        "project_id": "p1", "payload": {"analysis_id": "a1"}, "status": "failed",
        "priority": 100, "attempts": 6, "max_attempts": 6,
        "created_at": db.next_created_at(),
    }
    db.tables.setdefault("llm_jobs", []).append(dict(failed))
    spec, _, marks = _record_spec()
    monkeypatch.setattr(lq, "_spec", lambda jt: spec)
    fresh = lq.requeue_terminal(failed, created_by="u2")
    assert fresh["id"] != "f1"
    assert fresh["status"] == "queued" and fresh["attempts"] == 0
    assert fresh["payload"] == {"analysis_id": "a1"}
    assert len(db.tables["llm_jobs"]) == 2  # the failed row stays as history
    assert marks == [("a1", {"status": "pending", "error": None})]


def test_requeue_terminal_rejects_a_non_terminal_job(monkeypatch):
    _queue_env(monkeypatch)
    with pytest.raises(ValueError, match="failed or canceled"):
        lq.requeue_terminal({"id": "q1", "status": "queued"}, created_by="u2")


# ── llm_queue: rfp_ingest (sandbox) job type ─────────────────────────────


class _Boom:
    """A get_supabase() stand-in for paths that must not touch the DB."""

    def table(self, *_a, **_k):  # pragma: no cover - reaching here is the failure
        raise AssertionError("the DB must not be touched here")

    def rpc(self, *_a, **_k):  # pragma: no cover
        raise AssertionError("the DB must not be touched here")


def _install_rfp_ingest(monkeypatch, *, run=None, current_status=None, prune=None):
    """Stand in for app.services.rfp_ingest (a later package) with exactly the
    surface llm_queue wires: execute / mark_from_queue / current_status /
    prune_expired and the two self-classifying exception classes. Installed
    in sys.modules AND as the package attribute so `from app.services import
    rfp_ingest` resolves to the stand-in even once the real module exists."""
    import app.services as services_pkg

    mod = ModuleType("app.services.rfp_ingest")
    runs, marks, prunes = [], [], []

    class RfpIngestTransient(RuntimeError):
        llm_error_kind = "infrastructure"

    class RfpIngestPermanent(ValueError):
        llm_error_kind = "bad_input"

    def execute(run_id):
        runs.append(run_id)
        if run is not None:
            run(run_id)

    mod.RfpIngestTransient = RfpIngestTransient
    mod.RfpIngestPermanent = RfpIngestPermanent
    mod.execute = execute
    mod.mark_from_queue = lambda run_id, status, error: marks.append((run_id, status, error))
    mod.current_status = current_status or (lambda run_id: None)
    mod.prune_expired = prune or (lambda: prunes.append(True))
    monkeypatch.setitem(sys.modules, "app.services.rfp_ingest", mod)
    monkeypatch.setattr(services_pkg, "rfp_ingest", mod, raising=False)
    return mod, runs, marks, prunes


def _rfp_job(db, job_id="j1", run_id="r1", attempts=1):
    return _running_row(
        db, job_id=job_id, target=run_id, attempts=attempts,
        job_type=lq.JOB_RFP_INGEST, payload={"run_id": run_id},
    )


def _enqueue_rfp(run_id):
    return lq.enqueue(
        lq.JOB_RFP_INGEST, target_id=run_id, payload={"run_id": run_id}, priority=200
    )


def _row(db, job_id):
    return next(r for r in db.tables["llm_jobs"] if r["id"] == job_id)


def test_rfp_ingest_is_a_job_type_but_not_an_llm_one():
    assert lq.JOB_RFP_INGEST == "rfp_ingest"
    assert lq.JOB_RFP_INGEST not in lq.LLM_JOB_TYPES
    assert set(lq.LLM_JOB_TYPES) == {
        lq.JOB_BOQ, lq.JOB_GENERAL_MATERIAL, lq.JOB_PROPOSAL, lq.JOB_BID_SPLIT
    }


def test_enqueue_rfp_ingest_records_its_feature_and_priority(monkeypatch):
    db = _queue_env(monkeypatch)
    job = _enqueue_rfp("r1")
    row = db.tables["llm_jobs"][0]
    assert job["id"] == row["id"]
    assert row["feature"] == "rfp_ingest"
    assert row["priority"] == 200  # never outranks a user-facing LLM job (100)
    assert row["payload"] == {"run_id": "r1"}


def test_claim_honors_the_job_types_filter(monkeypatch):
    db = _queue_env(monkeypatch)
    boq = lq.enqueue(lq.JOB_BOQ, target_id="a")
    rfp = _enqueue_rfp("r1")
    # A sandbox-only claim leaves the LLM row queued, and vice versa.
    assert [j["id"] for j in lq._claim(_settings(), 10, [lq.JOB_RFP_INGEST])] == [rfp["id"]]
    assert _row(db, boq["id"])["status"] == "queued"
    assert db.rpc_calls[-1]["job_types"] == [lq.JOB_RFP_INGEST]
    assert [j["id"] for j in lq._claim(_settings(), 10, list(lq.LLM_JOB_TYPES))] == [boq["id"]]


def test_claim_without_a_filter_takes_any_type_and_omits_the_parameter(monkeypatch):
    db = _queue_env(monkeypatch)
    lq.enqueue(lq.JOB_BOQ, target_id="a")
    _enqueue_rfp("r1")
    claimed = lq._claim(_settings(), 10)
    assert {j["job_type"] for j in claimed} == {lq.JOB_BOQ, lq.JOB_RFP_INGEST}
    # None means "no filter"; the key is left out so the RPC default applies.
    assert "job_types" not in db.rpc_calls[-1]


def test_claim_tick_llm_pass_never_takes_a_sandbox_row(monkeypatch):
    db = _queue_env(monkeypatch)
    s = _settings(rfp_ingest_enabled=False)
    rfp = _enqueue_rfp("r1")
    boq = lq.enqueue(lq.JOB_BOQ, target_id="a")
    claimed = lq._claim_tick(s, [])
    assert [j["id"] for j in claimed] == [boq["id"]]
    # Exactly one RPC call (the sandbox pass is skipped while the flag is
    # off) and it named the LLM types, so the RPC could never hand back r1.
    assert len(db.rpc_calls) == 1
    assert db.rpc_calls[0]["job_types"] == list(lq.LLM_JOB_TYPES)
    assert _row(db, rfp["id"])["status"] == "queued"


def test_claim_tick_sandbox_capacity_is_independent_of_llm_slots(monkeypatch):
    db = _queue_env(monkeypatch)
    s = _settings(llm_queue_worker_concurrency=1, rfp_ingest_sandbox_concurrency=1)
    a = lq.enqueue(lq.JOB_BOQ, target_id="a")
    b = lq.enqueue(lq.JOB_BOQ, target_id="b")
    r1 = _enqueue_rfp("r1")
    r2 = _enqueue_rfp("r2")
    # Nothing running: one of each, in creation order within each pass.
    first = lq._claim_tick(s, [])
    assert [j["id"] for j in first] == [a["id"], r1["id"]]
    assert _row(db, b["id"])["status"] == "queued"
    assert _row(db, r2["id"])["status"] == "queued"
    # An LLM job in flight uses up the LLM slot but not the sandbox one.
    llm_busy = lq._claim_tick(s, [lq.JOB_BOQ])
    assert [j["id"] for j in llm_busy] == [r2["id"]]
    assert _row(db, b["id"])["status"] == "queued"
    # And a sandbox run in flight never blocks the LLM pass.
    r3 = _enqueue_rfp("r3")
    sandbox_busy = lq._claim_tick(s, [lq.JOB_RFP_INGEST])
    assert [j["id"] for j in sandbox_busy] == [b["id"]]
    assert _row(db, r3["id"])["status"] == "queued"


def test_claim_tick_second_sandbox_run_waits_while_one_runs(monkeypatch):
    db = _queue_env(monkeypatch)
    s = _settings(rfp_ingest_sandbox_concurrency=1)
    r1 = _enqueue_rfp("r1")
    r2 = _enqueue_rfp("r2")
    assert [j["id"] for j in lq._claim_tick(s, [])] == [r1["id"]]
    # While r1 runs the sandbox pass has zero capacity and is not even called.
    calls_before = len(db.rpc_calls)
    assert lq._claim_tick(s, [lq.JOB_RFP_INGEST]) == []
    # The LLM pass and the third pass (the promotion type rides it under the
    # master switch alone, docs/RFP_CREATE.md 5); never the sandbox pass.
    assert [c["job_types"] for c in db.rpc_calls[calls_before:]] == [
        list(lq.LLM_JOB_TYPES), list(lq.CREATE_JOB_TYPES)
    ]
    assert _row(db, r2["id"])["status"] == "queued"
    # r1 finished: the slot frees and r2 is claimed on the next tick.
    assert [j["id"] for j in lq._claim_tick(s, [])] == [r2["id"]]


def test_renew_lease_extends_the_lease_and_refuses_a_stale_attempt(monkeypatch):
    db = _queue_env(monkeypatch)
    later = NOW + timedelta(seconds=100)
    monkeypatch.setattr(lq, "_now", lambda: later)
    job = _running_row(db, job_id="j1", target="r1", attempts=2)
    assert lq.renew_lease(job) is True
    assert _row(db, "j1")["lease_expires_at"] == (later + timedelta(seconds=900)).isoformat()
    # A stale attempt (the lease expired, the sweep requeued, this process
    # reclaimed: attempts moved on) must not extend the newer claim's lease.
    stale = dict(job, attempts=1)
    assert lq.renew_lease(stale) is False
    # Not ours, or no longer running: refused too.
    assert lq.renew_lease(dict(job, claimed_by="someone-else")) is False
    _row(db, "j1")["status"] = "queued"
    assert lq.renew_lease(job) is False


def test_renew_and_requeue_are_no_ops_outside_a_queue_job(monkeypatch):
    # BackgroundTasks fallback: no current job, nothing to fence, no DB touch.
    monkeypatch.setattr(lq, "get_supabase", lambda: _Boom())
    assert lq.current_job.get() is None
    assert lq.renew_lease() is True
    assert lq.requeue_self() is True


def test_renew_lease_keeps_the_lease_on_a_database_error(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(lq, "get_supabase", boom)
    monkeypatch.setattr(lq, "get_settings", lambda: _settings())
    job = {"id": "j1", "claimed_by": lq._WORKER_TOKEN, "attempts": 1}
    # A hiccup is not a lost lease: most of the window is still ahead and the
    # next renewal retries. Killing hours of sandbox work for it would be worse.
    assert lq.renew_lease(job) is True


def test_requeue_self_hands_the_job_back_as_interrupted(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    job = _running_row(db, job_id="j1", target="r1", attempts=1)
    assert lq.requeue_self(job) is True
    row = _row(db, "j1")
    assert row["status"] == "queued"
    assert row["next_attempt_at"] == NOW.isoformat()  # due immediately
    assert row["error_kind"] == "interrupted"
    assert row["last_error"] == lq._INTERRUPTED_MESSAGE
    assert row["claimed_by"] is None and row["lease_expires_at"] is None
    assert row["attempts"] == 1  # the next claim spends the next attempt
    assert job["status"] == "queued"  # the local dict mirrors the hand-off
    # Already handed on (or reclaimed by someone else): nothing to do.
    assert lq.requeue_self(dict(job, status="running")) is False


def test_execute_exposes_the_current_job_and_a_requeued_run_is_not_marked_done(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    seen = []

    def run(run_id):
        seen.append(lq.current_job.get())
        assert lq.renew_lease() is True  # the default is the current job
        assert lq.requeue_self() is True  # shutdown path: hand the job on

    _install_rfp_ingest(monkeypatch, run=run)
    job = _rfp_job(db)
    lq._execute(job)
    assert seen == [job]
    assert lq.current_job.get() is None  # reset after the run
    row = _row(db, "j1")
    assert row["status"] == "queued" and row["error_kind"] == "interrupted"
    assert row["finished_at"] is None  # never marked succeeded


def test_rfp_spec_wires_run_mark_status_and_the_sandbox_label(monkeypatch):
    _, runs, marks, _ = _install_rfp_ingest(monkeypatch, current_status=lambda r: "done")
    spec = lq._spec(lq.JOB_RFP_INGEST)
    assert spec.feature == "rfp_ingest"
    spec.run({"run_id": "r1"})
    assert runs == ["r1"]
    spec.mark("r1", {"status": "pending", "error": None})
    spec.mark("r1", {"status": "failed", "error": "boom"})
    assert marks == [("r1", "pending", None), ("r1", "failed", "boom")]
    assert spec.current_status("r1") == "done"
    assert spec.model_label(_settings()) == "sandbox"


def test_default_model_label_is_the_features_active_model():
    spec = lq._JobSpec("boq", lambda p: None, lambda t, f: None, lambda t: None)
    s = _settings()
    assert spec.model_label(s) == llm.active_model("boq", s)


def test_execute_rfp_transient_failure_requeues_with_its_own_message(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    message = "A storage operation failed; retry the run."
    mod, _, marks, _ = _install_rfp_ingest(monkeypatch)

    def run(run_id):
        raise mod.RfpIngestTransient(message)

    mod.execute = run
    lq._execute(_rfp_job(db))
    row = _row(db, "j1")
    assert row["status"] == "queued"
    assert row["error_kind"] == "infrastructure"
    assert row["last_error"] == message  # app-authored, stored verbatim
    assert row["next_attempt_at"] == (NOW + timedelta(seconds=10)).isoformat()
    assert marks == [("r1", "pending", None)]


def test_execute_rfp_permanent_failure_fails_with_the_app_authored_message(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    message = "The PDF has no pages."
    mod, _, marks, _ = _install_rfp_ingest(monkeypatch)

    def run(run_id):
        raise mod.RfpIngestPermanent(message)

    mod.execute = run
    lq._execute(_rfp_job(db))
    row = _row(db, "j1")
    assert row["status"] == "failed"
    assert row["error_kind"] == "bad_input"
    assert row["last_error"] == message
    assert row["finished_at"] == NOW.isoformat()
    assert marks == [("r1", "failed", message)]


def test_execute_rfp_lease_lost_writes_nothing(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)

    class LeaseLost(Exception):
        __module__ = "app.services.rfp_sandbox_runner"

    def run(run_id):
        raise LeaseLost()

    _, _, marks, _ = _install_rfp_ingest(monkeypatch, run=run)
    job = _rfp_job(db)
    before = dict(_row(db, "j1"))
    lq._execute(job)
    # The sweep already handed the job on; our fence would refuse anyway and
    # the domain row belongs to the new owner. Nothing moved, nothing marked.
    assert _row(db, "j1") == before
    assert marks == []


def test_execute_rfp_undeclared_failure_stores_the_sandbox_authored_sentence(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)

    def run(run_id):
        raise RuntimeError("raw text with /tmp/rfp-ingest-abc/source.pdf")

    _install_rfp_ingest(monkeypatch, run=run)
    lq._execute(_rfp_job(db))
    row = _row(db, "j1")
    assert row["status"] == "queued"  # unknown is transient
    assert row["error_kind"] == "unknown"
    assert row["last_error"] == protocol.VERDICT_MESSAGES[protocol.FAIL_INTERRUPTED]
    assert "/tmp/" not in row["last_error"]  # raw text never stored


def test_execute_rfp_undeclared_timeout_never_blames_the_ai_provider(monkeypatch):
    """A run that never touched an LLM cannot be described as one. httpx and
    postgrest errors escape execute outside the guarded per-file loop, and
    llm_errors would classify them as timeout/server_error and write text
    about "the AI provider"; the rfp spec's error_message maps anything it did
    not declare to the sandbox's own interrupted sentence, in the domain row
    (the bell renders it) as well as in the job row."""
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)

    def run(run_id):
        raise httpx.ReadTimeout("timed out", request=httpx.Request("GET", "http://db"))

    _, _, marks, _ = _install_rfp_ingest(monkeypatch, run=run)
    lq._execute(_rfp_job(db, attempts=6))  # ladder exhausted: terminal
    row = _row(db, "j1")
    assert row["status"] == "failed"
    assert row["error_kind"] == "timeout"
    message = protocol.VERDICT_MESSAGES[protocol.FAIL_INTERRUPTED]
    assert row["last_error"] == message
    assert marks == [("r1", "failed", message + " (failed after 6 attempts)")]
    for text in (row["last_error"], marks[0][2]):
        assert "AI provider" not in text and "the model" not in text


def test_execute_rfp_undeclared_value_error_does_not_leak_developer_text(monkeypatch):
    """bad_input is the one kind llm_errors passes through verbatim, so a raw
    ValueError from a mark helper would otherwise land in the error column."""
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)

    def run(run_id):
        raise ValueError("_mark cannot move a run to 'nonsense'")

    _, _, marks, _ = _install_rfp_ingest(monkeypatch, run=run)
    lq._execute(_rfp_job(db))
    row = _row(db, "j1")
    assert row["status"] == "failed" and row["error_kind"] == "bad_input"
    assert row["last_error"] == protocol.VERDICT_MESSAGES[protocol.FAIL_INTERRUPTED]
    assert marks == [("r1", "failed", protocol.VERDICT_MESSAGES[protocol.FAIL_INTERRUPTED])]


def test_llm_specs_keep_the_llm_flavored_message(monkeypatch):
    """Only the sandbox spec overrides the mapper: an LLM job still gets
    llm_errors.user_message, naming the provider and the model."""
    spec = lq._spec(lq.JOB_BOQ)
    exc = httpx.ReadTimeout("t", request=httpx.Request("POST", "http://llm"))
    assert spec.error_message(exc, "claude-x") == llm_errors.user_message(exc, "claude-x")
    assert "the AI provider" in spec.error_message(exc, "claude-x")


def test_handle_failure_forces_a_terminal_state_when_the_handler_itself_errors(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    job = _running_row(db, job_id="j1", target="a1", attempts=1)
    marks = []

    def bad_label(s):
        raise KeyError("no such feature")

    spec = lq._JobSpec(
        "nope", _raiser(ValueError("x")), lambda t, f: marks.append((t, f)),
        lambda t: None, model_label=bad_label,
    )
    monkeypatch.setattr(lq, "_spec", lambda jt: spec)
    lq._execute(job)  # must not raise
    row = _row(db, "j1")
    assert row["status"] == "failed"
    assert row["error_kind"] == "unknown"
    assert row["last_error"] == lq._HANDLER_FAILED_MESSAGE
    assert marks == [("a1", {"status": "failed", "error": lq._HANDLER_FAILED_MESSAGE})]


def test_handle_failure_fallback_leaves_an_already_requeued_job_alone(monkeypatch):
    # The inner handler requeued the job and THEN errored (the domain mark
    # exploding past its own guard, say): the forced terminal CAS is fenced
    # on status=running and must not overwrite the requeue.
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    job = _running_row(db, job_id="j1", target="a1", attempts=1)
    spec, _, _ = _record_spec(run=_raiser(llm.LlmBadOutput("bad")))
    monkeypatch.setattr(lq, "_spec", lambda jt: spec)

    def exploding_mark(spec_, job_, fields):
        raise RuntimeError("mark exploded")

    monkeypatch.setattr(lq, "_mark_domain", exploding_mark)
    lq._execute(job)
    row = _row(db, "j1")
    assert row["status"] == "queued"
    assert row["error_kind"] == "invalid_output"


def test_handle_failure_fallback_does_not_mark_the_domain_when_its_cas_loses(monkeypatch):
    # The inner handler requeued the job, then errored on something else: the
    # forced terminal CAS loses its fence (status is queued), so the domain
    # row must NOT be marked failed behind the requeue's back.
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    job = _running_row(db, job_id="j1", target="a1", attempts=1)
    spec, _, marks = _record_spec(run=_raiser(llm.LlmBadOutput("bad")))
    monkeypatch.setattr(lq, "_spec", lambda jt: spec)
    real_inner = lq._handle_failure_inner

    def requeue_then_explode(sb, job_, spec_, exc, s):
        real_inner(sb, job_, spec_, exc, s)
        raise RuntimeError("something after the requeue")

    monkeypatch.setattr(lq, "_handle_failure_inner", requeue_then_explode)
    lq._execute(job)
    row = _row(db, "j1")
    assert row["status"] == "queued" and row["error_kind"] == "invalid_output"
    assert row["last_error"] == "bad"
    assert marks == [("a1", {"status": "pending", "error": None})]  # no failed mark


def test_sweep_prune_slot_runs_the_sandbox_retention_behind_the_flag(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    _, _, _, prunes = _install_rfp_ingest(monkeypatch)
    monkeypatch.setattr(lq, "_last_prune", -lq._PRUNE_EVERY_SECONDS)
    lq._sweep(_settings(rfp_ingest_enabled=False))
    assert prunes == []  # a disabled feature never runs its prune
    monkeypatch.setattr(lq, "_last_prune", -lq._PRUNE_EVERY_SECONDS)
    lq._sweep(_settings(rfp_ingest_enabled=True))
    assert prunes == [True]
    # Not due again until the next hour.
    lq._sweep(_settings(rfp_ingest_enabled=True))
    assert prunes == [True]
    assert db.tables.get("llm_call_log", []) == []


def test_sweep_survives_either_prune_failing(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)

    def bad_prune():
        raise RuntimeError("storage down")

    _install_rfp_ingest(monkeypatch, prune=bad_prune)
    monkeypatch.setattr(lq, "_last_prune", -lq._PRUNE_EVERY_SECONDS)
    lq._sweep(_settings())  # the sandbox prune raising must not escape

    # And a ledger prune failure must not skip the sandbox prune.
    _, _, _, prunes = _install_rfp_ingest(monkeypatch)
    real_table = db.table

    class _Exploding:
        def __getattr__(self, _name):
            raise RuntimeError("ledger table gone")

    monkeypatch.setattr(
        db, "table", lambda name: _Exploding() if name == "llm_call_log" else real_table(name)
    )
    monkeypatch.setattr(lq, "_last_prune", -lq._PRUNE_EVERY_SECONDS)
    lq._sweep(_settings())
    assert prunes == [True]


# ── Routers: queue-aware dispatch ────────────────────────────────────────


def _route(router, path, method):
    for r in router.routes:
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set()):
            return r
    raise AssertionError(f"route {method} {path} not found")


def _install_boq(monkeypatch, db, settings=None):
    monkeypatch.setattr(boq_router, "get_supabase", lambda: db)
    monkeypatch.setattr(boq_router, "get_settings", lambda: settings or _settings())
    monkeypatch.setattr(boq_router, "audit", lambda *a, **k: None)
    monkeypatch.setattr(
        boq_router.estimator_rounds, "resolve_boq_file_id", lambda pid, fid: "bf1"
    )


def test_start_analysis_409_when_a_queue_job_is_live_even_past_the_window(monkeypatch):
    # The pending row is far older than the 15-minute backstop; the live
    # llm_jobs row is the authority and still blocks a second paid run.
    db = FakeDB({
        "boq_analyses": [
            {"id": "a1", "project_id": "p1", "status": "pending",
             "created_at": "2020-01-01T00:00:00+00:00"},
        ],
    })
    _install_boq(monkeypatch, db)
    monkeypatch.setattr(lq, "active_job", lambda jt, t: {"id": "j1"})
    with pytest.raises(HTTPException) as exc:
        boq_router.start_analysis("p1", BoqAnalysisStart(), BackgroundTasks(), user=_user())
    assert exc.value.status_code == 409


def test_start_analysis_allows_a_stale_row_whose_job_vanished(monkeypatch):
    db = FakeDB({
        "boq_analyses": [
            {"id": "a1", "project_id": "p1", "status": "pending",
             "created_at": "2020-01-01T00:00:00+00:00"},
        ],
    })
    _install_boq(monkeypatch, db)
    monkeypatch.setattr(lq, "active_job", lambda jt, t: None)
    captured = {}
    monkeypatch.setattr(
        lq, "enqueue", lambda jt, **kw: captured.update(job_type=jt, **kw) or {"id": "qj"}
    )
    row = boq_router.start_analysis("p1", BoqAnalysisStart(), BackgroundTasks(), user=_user())
    assert captured["job_type"] == lq.JOB_BOQ
    assert captured["target_id"] == row["id"]


def test_start_analysis_enqueues_instead_of_background_when_enabled(monkeypatch):
    db = FakeDB({"boq_analyses": []})
    _install_boq(monkeypatch, db)
    captured = {}
    monkeypatch.setattr(
        lq, "enqueue", lambda jt, **kw: captured.update(job_type=jt, **kw) or {"id": "qj"}
    )
    background = BackgroundTasks()
    row = boq_router.start_analysis("p1", BoqAnalysisStart(), background, user=_user())
    assert captured["job_type"] == lq.JOB_BOQ
    assert captured["payload"] == {"analysis_id": row["id"]}
    assert captured["created_by"] == "u1"
    assert background.tasks == []


def test_start_analysis_background_fallback_when_queue_disabled(monkeypatch):
    db = FakeDB({"boq_analyses": []})
    _install_boq(monkeypatch, db, settings=_settings(llm_queue_enabled=False))
    monkeypatch.setattr(
        lq, "enqueue", lambda *a, **k: pytest.fail("queue must not be used when disabled")
    )
    background = BackgroundTasks()
    row = boq_router.start_analysis("p1", BoqAnalysisStart(), background, user=_user())
    assert len(background.tasks) == 1
    assert background.tasks[0].func is bx.run_extraction
    assert background.tasks[0].args == (row["id"],)


def test_rerun_extraction_resets_row_and_enqueues(monkeypatch):
    db = FakeDB({
        "general_material_estimates": [
            {"id": "g1", "project_id": "p1", "status": "done", "amount": "5",
             "tax_included": True, "tax_rate": "8.375"},
        ],
    })
    monkeypatch.setattr(gm_router, "get_supabase", lambda: db)
    monkeypatch.setattr(gm_router, "get_settings", lambda: _settings())
    monkeypatch.setattr(gm_router, "audit", lambda *a, **k: None)
    captured = {}
    monkeypatch.setattr(
        lq, "enqueue", lambda jt, **kw: captured.update(job_type=jt, **kw) or {"id": "qj"}
    )
    out = gm_router.rerun_extraction("p1", BackgroundTasks(), user=_user())
    assert out == {"status": "pending"}
    row = db.tables["general_material_estimates"][0]
    assert row["status"] == "pending" and row["error"] is None
    assert row["tax_included"] is None  # attestation re-armed up front
    assert row["tax_rate"] == "8.375"  # kept as the re-ask prefill
    assert captured["job_type"] == lq.JOB_GENERAL_MATERIAL
    assert captured["target_id"] == "p1" and captured["payload"] == {"project_id": "p1"}


def test_rerun_extraction_route_has_ai_rate_limit():
    route = _route(
        gm_router.router, "/projects/{project_id}/general-material/extract", "POST"
    )
    assert any(d.call is ai_rate_limit for d in route.dependant.dependencies)


def test_start_lines_generation_409_on_a_fresh_active_draft(monkeypatch):
    now_iso = datetime.now(timezone.utc).isoformat()
    db = FakeDB({
        "proposal_drafts": [
            {"id": "d1", "project_id": "p1", "status": "pending",
             "created_at": now_iso, "updated_at": now_iso},
        ],
    })
    monkeypatch.setattr(proposals_router, "get_supabase", lambda: db)
    with pytest.raises(HTTPException) as exc:
        proposals_router.start_lines_generation(
            "p1", ProposalGenerateIn(), BackgroundTasks(), user=_user()
        )
    assert exc.value.status_code == 409


def test_latest_draft_attaches_queue_info_to_pending_rows(monkeypatch):
    now_iso = datetime.now(timezone.utc).isoformat()
    db = FakeDB({
        "proposal_drafts": [
            {"id": "d1", "project_id": "p1", "status": "pending", "lines_json": None,
             "created_at": now_iso, "updated_at": now_iso},
        ],
    })
    monkeypatch.setattr(proposals_router, "get_supabase", lambda: db)
    queue_info = {"state": "queued", "position": 2, "retrying": False}
    monkeypatch.setattr(lq, "poll_info", lambda jt, t: queue_info)
    out = proposals_router.latest_draft("p1", user=_user())
    assert out["queue"] == queue_info
    assert out["lines"] == []


def test_latest_draft_skips_queue_info_for_done_rows(monkeypatch):
    db = FakeDB({
        "proposal_drafts": [
            {"id": "d1", "project_id": "p1", "status": "done",
             "lines_json": ["Furnish and install panels."],
             "created_at": "2026-08-01T00:00:00+00:00",
             "updated_at": "2026-08-01T00:00:00+00:00"},
        ],
    })
    monkeypatch.setattr(proposals_router, "get_supabase", lambda: db)
    monkeypatch.setattr(
        lq, "poll_info", lambda *a: pytest.fail("done rows must not hit the queue")
    )
    out = proposals_router.latest_draft("p1", user=_user())
    assert "queue" not in out
    assert out["lines"] == ["Furnish and install panels."]


# ── proposal_scope.fail_if_stale (queue-aware) ───────────────────────────


def _stale_draft_db():
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    return FakeDB({
        "proposal_drafts": [
            {"id": "d1", "project_id": "p1", "status": "pending",
             "created_at": stale, "updated_at": stale},
        ],
    })


def test_fail_if_stale_keeps_a_row_with_a_live_queue_job(monkeypatch):
    db = _stale_draft_db()
    monkeypatch.setattr(ps, "get_supabase", lambda: db)
    monkeypatch.setattr(lq, "active_job", lambda jt, t: {"id": "j1"})
    out = ps.fail_if_stale(dict(db.tables["proposal_drafts"][0]))
    assert out["status"] == "pending"
    assert db.tables["proposal_drafts"][0]["status"] == "pending"  # no fail write


def test_fail_if_stale_fails_a_genuinely_stranded_row(monkeypatch):
    db = _stale_draft_db()
    monkeypatch.setattr(ps, "get_supabase", lambda: db)
    monkeypatch.setattr(lq, "active_job", lambda jt, t: None)
    out = ps.fail_if_stale(dict(db.tables["proposal_drafts"][0]))
    assert out["status"] == "failed"
    assert out["error"] == "Generation was interrupted (server restarted). Run it again."
    assert db.tables["proposal_drafts"][0]["status"] == "failed"


def test_fail_if_stale_survives_a_queue_lookup_error(monkeypatch):
    db = _stale_draft_db()
    monkeypatch.setattr(ps, "get_supabase", lambda: db)

    def boom(jt, t):
        raise RuntimeError("queue table missing")

    monkeypatch.setattr(lq, "active_job", boom)
    out = ps.fail_if_stale(dict(db.tables["proposal_drafts"][0]))
    assert out["status"] == "failed"  # polling still releases the row


# ── Service split: execute raises, run_* terminal-marks ──────────────────


def test_boq_execute_reraises_and_leaves_the_row_running(monkeypatch):
    db = FakeDB({
        "boq_analyses": [
            {"id": "a1", "project_id": "p1", "boq_file_id": "bf1", "status": "pending"},
        ],
    })
    monkeypatch.setattr(bx, "get_supabase", lambda: db)
    monkeypatch.setattr(bx, "_load_boq_text", lambda analysis: "doc")
    monkeypatch.setattr(bx, "_active_material_category_names", lambda: ["Lighting"])
    monkeypatch.setattr(
        bx, "_call_llm", lambda *a, **k: (_ for _ in ()).throw(llm.LlmBadOutput("bad"))
    )
    with pytest.raises(llm.LlmBadOutput):
        bx.execute("a1")
    row = db.tables["boq_analyses"][0]
    assert row["status"] == "running"  # the queue owns the terminal mark
    assert row["input_snapshot"]["user"]  # snapshot taken before the call


def test_proposal_execute_raises_bad_output_when_no_lines_come_back(monkeypatch):
    db = FakeDB({
        "proposal_drafts": [
            {"id": "d1", "project_id": "p1", "boq_file_id": "bf1", "status": "pending"},
        ],
    })
    monkeypatch.setattr(ps, "get_supabase", lambda: db)
    monkeypatch.setattr(ps, "_load_boq_text", lambda draft: "doc")
    monkeypatch.setattr(ps, "_call_llm", lambda doc: {"lines": [], "notes": None})
    with pytest.raises(llm.LlmBadOutput, match="no usable scope lines"):
        ps.execute("d1")
    assert db.tables["proposal_drafts"][0]["status"] == "running"


# ── llm_queue: rfp_harvest job type (docs/RFP_HARVEST.md 2.2) ─────────────


def _enqueue_harvest(email_id, force=False):
    return lq.enqueue(
        lq.JOB_RFP_HARVEST, target_id=email_id,
        payload={"email_id": email_id, "force": force}, priority=150,
    )


def _harvest_job(db, job_id="h1", email_id="e1", attempts=1, force=False):
    return _running_row(
        db, job_id=job_id, target=email_id, attempts=attempts,
        job_type=lq.JOB_RFP_HARVEST, payload={"email_id": email_id, "force": force},
    )


def _harvest_settings(**over):
    base = dict(
        rfp_ingest_enabled=True, rfp_harvest_enabled=True,
        procore_login_email="bot@example.com", procore_login_password="pw",
    )
    base.update(over)
    return _settings(**base)


def _install_rfp_harvest(monkeypatch, *, run=None, current_status=None):
    """Recording stand-ins on the real rfp_harvest module for exactly the
    surface llm_queue wires: execute / mark_from_queue / current_status."""
    from app.services import rfp_harvest as mod

    runs, marks = [], []

    def execute(email_id, *, force=False):
        runs.append((email_id, force))
        if run is not None:
            run(email_id)

    monkeypatch.setattr(mod, "execute", execute)
    monkeypatch.setattr(mod, "mark_from_queue", lambda e, s, err: marks.append((e, s, err)))
    monkeypatch.setattr(mod, "current_status", current_status or (lambda e: None))
    return mod, runs, marks


def test_rfp_harvest_is_a_job_type_but_never_an_llm_one():
    assert lq.JOB_RFP_HARVEST == "rfp_harvest"
    assert lq.JOB_RFP_HARVEST in lq.NON_LLM_JOB_TYPES
    assert lq.JOB_RFP_HARVEST not in lq.LLM_JOB_TYPES
    assert lq._FEATURE_BY_TYPE[lq.JOB_RFP_HARVEST] == "rfp_harvest"
    assert set(lq.NON_LLM_JOB_TYPES) == {
        lq.JOB_RFP_INGEST, lq.JOB_RFP_HARVEST, lq.JOB_PORTAL_SCAN, lq.JOB_PORTAL_HARVEST,
        lq.JOB_RFP_CREATE_FILES,
    }


def test_enqueue_rfp_harvest_records_its_feature_priority_and_force(monkeypatch):
    db = _queue_env(monkeypatch)
    job = _enqueue_harvest("e1", force=True)
    row = db.tables["llm_jobs"][0]
    assert job["id"] == row["id"] and row["feature"] == "rfp_harvest"
    assert row["priority"] == 150 and row["payload"] == {"email_id": "e1", "force": True}


@pytest.mark.parametrize(
    "flags",
    [
        {"rfp_ingest_enabled": True, "rfp_harvest_enabled": False},
        {"rfp_ingest_enabled": False, "rfp_harvest_enabled": True},
        {"rfp_ingest_enabled": False, "rfp_harvest_enabled": False},
    ],
)
def test_claim_tick_third_pass_needs_both_flags(monkeypatch, flags):
    db = _queue_env(monkeypatch)
    s = _harvest_settings(**flags)
    job = _enqueue_harvest("e1")
    boq = lq.enqueue(lq.JOB_BOQ, target_id="a")
    claimed = lq._claim_tick(s, [])
    assert [j["id"] for j in claimed] == [boq["id"]]
    assert _row(db, job["id"])["status"] == "queued"
    # No RPC call ever named the harvest type.
    assert all(lq.JOB_RFP_HARVEST not in c.get("job_types", []) for c in db.rpc_calls)


def test_claim_tick_third_pass_claims_harvest_jobs_with_their_own_capacity(monkeypatch):
    db = _queue_env(monkeypatch)
    s = _harvest_settings(
        llm_queue_worker_concurrency=1, rfp_ingest_sandbox_concurrency=1, rfp_harvest_concurrency=1,
    )
    a = lq.enqueue(lq.JOB_BOQ, target_id="a")
    b = lq.enqueue(lq.JOB_BOQ, target_id="b")
    r1 = _enqueue_rfp("r1")
    h1 = _enqueue_harvest("e1")
    h2 = _enqueue_harvest("e2")
    # Three passes, three independent slots: one of each in creation order.
    first = lq._claim_tick(s, [])
    assert [j["id"] for j in first] == [a["id"], r1["id"], h1["id"]]
    # The promotion type rides the same pass (docs/RFP_CREATE.md 5).
    assert db.rpc_calls[-1]["job_types"] == [lq.JOB_RFP_HARVEST, lq.JOB_RFP_CREATE_FILES]
    assert db.rpc_calls[-1]["max_jobs"] == 1
    assert _row(db, h2["id"])["status"] == "queued"
    # A harvest in flight fills only the harvest slot: the LLM pass still runs,
    # and the harvest pass is not even called.
    calls_before = len(db.rpc_calls)
    llm_only = lq._claim_tick(s, [lq.JOB_RFP_HARVEST, lq.JOB_RFP_INGEST])
    assert [j["id"] for j in llm_only] == [b["id"]]
    assert [c["job_types"] for c in db.rpc_calls[calls_before:]] == [list(lq.LLM_JOB_TYPES)]
    assert _row(db, h2["id"])["status"] == "queued"
    # An LLM job and a sandbox run in flight never block the harvest pass.
    busy = lq._claim_tick(s, [lq.JOB_BOQ, lq.JOB_RFP_INGEST])
    assert [j["id"] for j in busy] == [h2["id"]]


def test_claim_tick_harvest_capacity_is_concurrency_minus_running_harvests(monkeypatch):
    db = _queue_env(monkeypatch)
    s = _harvest_settings(rfp_harvest_concurrency=3, llm_queue_worker_concurrency=2)
    for i in range(4):
        _enqueue_harvest(f"e{i}")
    claimed = lq._claim_tick(s, [lq.JOB_RFP_HARVEST, lq.JOB_RFP_HARVEST])
    assert [j["target_id"] for j in claimed] == ["e0"]
    third_pass = [lq.JOB_RFP_HARVEST, lq.JOB_RFP_CREATE_FILES]
    harvest_call = next(c for c in db.rpc_calls if c.get("job_types") == third_pass)
    assert harvest_call["max_jobs"] == 1
    # Running harvests never count against the LLM slots.
    llm_call = next(c for c in db.rpc_calls if c.get("job_types") == list(lq.LLM_JOB_TYPES))
    assert llm_call["max_jobs"] == 2
    # At full harvest capacity the pass is skipped outright; a running
    # promotion fills a slot the same way.
    calls_before = len(db.rpc_calls)
    assert lq._claim_tick(s, [lq.JOB_RFP_HARVEST] * 3) == []
    assert lq._claim_tick(s, [lq.JOB_RFP_HARVEST] * 2 + [lq.JOB_RFP_CREATE_FILES]) == []
    assert all(c.get("job_types") != third_pass for c in db.rpc_calls[calls_before:])


def test_rfp_harvest_spec_wires_run_mark_status_label_and_message(monkeypatch):
    from app.services import rfp_harvest as real

    mod, runs, marks = _install_rfp_harvest(monkeypatch, current_status=lambda e: "done")
    spec = lq._spec(lq.JOB_RFP_HARVEST)
    assert spec.feature == "rfp_harvest"
    spec.run({"email_id": "e1"})
    spec.run({"email_id": "e2", "force": True})
    spec.run({"email_id": "e3", "force": 0})
    assert runs == [("e1", False), ("e2", True), ("e3", False)]
    spec.mark("e1", {"status": "pending", "error": None})
    spec.mark("e1", {"status": "failed", "error": "boom"})
    assert marks == [("e1", "pending", None), ("e1", "failed", "boom")]
    assert spec.current_status("e1") == "done"
    assert spec.model_label(_settings()) == "procore" == real.MODEL_LABEL
    assert spec.error_message is real.error_message
    assert spec.error_message(real.RfpHarvestTransient("Procore storage did not answer."), "procore") == (
        "Procore storage did not answer."
    )
    assert spec.error_message(ValueError("raw /tmp/x"), "procore") == real._MSG_INTERRUPTED


def test_execute_rfp_harvest_transient_failure_requeues_with_its_own_sentence(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    from app.services import rfp_harvest as real

    def run(email_id):
        raise real.RfpHarvestTransient("Procore answered 503; the harvest will be retried.")

    _, _, marks = _install_rfp_harvest(monkeypatch, run=run)
    lq._execute(_harvest_job(db))
    row = _row(db, "h1")
    assert row["status"] == "queued" and row["error_kind"] == "infrastructure"
    assert row["last_error"] == "Procore answered 503; the harvest will be retried."
    assert row["next_attempt_at"] == (NOW + timedelta(seconds=10)).isoformat()
    assert marks == [("e1", "pending", None)]


def test_execute_rfp_harvest_permanent_failure_fails_and_marks_the_domain(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    from app.services import rfp_harvest as real

    def run(email_id):
        raise real.RfpHarvestPermanent("The email carries no usable Procore bid link.")

    _, _, marks = _install_rfp_harvest(monkeypatch, run=run)
    lq._execute(_harvest_job(db))
    row = _row(db, "h1")
    assert row["status"] == "failed" and row["error_kind"] == "bad_input"
    assert row["last_error"] == "The email carries no usable Procore bid link."
    assert marks == [("e1", "failed", "The email carries no usable Procore bid link.")]


def test_execute_rfp_harvest_undeclared_error_never_blames_the_ai_provider(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    from app.services import rfp_harvest as real

    def run(email_id):
        raise httpx.ReadTimeout("timed out", request=httpx.Request("GET", "http://db"))

    _, _, marks = _install_rfp_harvest(monkeypatch, run=run)
    lq._execute(_harvest_job(db, attempts=6))
    row = _row(db, "h1")
    assert row["status"] == "failed" and row["error_kind"] == "timeout"
    assert row["last_error"] == real._MSG_INTERRUPTED
    assert marks == [("e1", "failed", real._MSG_INTERRUPTED + " (failed after 6 attempts)")]
    for text in (row["last_error"], marks[0][2]):
        assert "AI provider" not in text and "the model" not in text and "procore" not in text.lower()


def test_execute_rfp_harvest_success_keeps_the_force_flag_through_a_requeue(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    from app.services import rfp_harvest as real

    outcomes = [real.RfpHarvestTransient("blip"), None]

    def run(email_id):
        exc = outcomes.pop(0)
        if exc:
            raise exc

    _, runs, marks = _install_rfp_harvest(monkeypatch, run=run)
    job = _harvest_job(db, force=True)
    lq._execute(job)
    assert _row(db, "h1")["status"] == "queued" and _row(db, "h1")["payload"]["force"] is True
    db.now = NOW + timedelta(seconds=30)
    [reclaimed] = db.rpc("claim_llm_jobs", {"worker_id": lq._WORKER_TOKEN, "lease_seconds": 900,
                                             "max_jobs": 1, "job_types": [lq.JOB_RFP_HARVEST]}).execute().data
    lq._execute(reclaimed)
    assert runs == [("e1", True), ("e1", True)]
    assert _row(db, "h1")["status"] == "succeeded"
    assert marks == [("e1", "pending", None)]


def test_requeue_terminal_refuses_a_harvest_whose_email_is_done(monkeypatch):
    db = _queue_env(monkeypatch)
    _install_rfp_harvest(monkeypatch, current_status=lambda e: "done")
    job = _harvest_job(db)
    _row(db, "h1").update(status="failed", finished_at=NOW.isoformat())
    with pytest.raises(ValueError, match="since completed"):
        lq.requeue_terminal(_row(db, "h1"), created_by="u1")
    assert job["id"] == "h1" and len(db.tables["llm_jobs"]) == 1


# ── llm_queue: the NGEM portal job types (docs/RFP_NGEM_PORTAL.md 2.2) ─────


def _portal_settings(**over):
    base = dict(
        rfp_ingest_enabled=True, rfp_ngem_enabled=True, ngem_login_username="acct",
        ngem_login_password="pw",
        ngem_entry_url="https://supplier.ionwave.net/VendorResponse/ResponseList.aspx?e=x",
    )
    base.update(over)
    return _settings(**base)


def _install_rfp_portal(monkeypatch, *, scan=None, harvest=None, scan_status=None, current_status=None):
    """Recording stand-ins on the real rfp_portal_ingest module for exactly
    the surface llm_queue wires."""
    from app.services import rfp_portal_ingest as mod

    scans, harvests, marks = [], [], []

    def execute_scan(run_id):
        scans.append(run_id)
        if scan is not None:
            scan(run_id)

    def execute_harvest(invitation_id, *, force=False):
        harvests.append((invitation_id, force))
        if harvest is not None:
            harvest(invitation_id)

    monkeypatch.setattr(mod, "execute_scan", execute_scan)
    monkeypatch.setattr(mod, "execute_harvest", execute_harvest)
    monkeypatch.setattr(mod, "mark_scan_from_queue", lambda t, s, e: marks.append(("scan", t, s, e)))
    monkeypatch.setattr(mod, "mark_harvest_from_queue", lambda t, s, e: marks.append(("harvest", t, s, e)))
    monkeypatch.setattr(mod, "scan_status", scan_status or (lambda t: None))
    monkeypatch.setattr(mod, "current_status", current_status or (lambda t: None))
    return mod, scans, harvests, marks


def test_portal_job_types_are_non_llm_and_share_the_harvest_family():
    assert lq.JOB_PORTAL_SCAN == "rfp_portal_scan" and lq.JOB_PORTAL_HARVEST == "rfp_portal_harvest"
    assert set(lq.PORTAL_JOB_TYPES) == {lq.JOB_PORTAL_SCAN, lq.JOB_PORTAL_HARVEST}
    assert set(lq.HARVEST_JOB_TYPES) == {lq.JOB_RFP_HARVEST, *lq.PORTAL_JOB_TYPES}
    for jt in lq.PORTAL_JOB_TYPES:
        assert jt in lq.NON_LLM_JOB_TYPES and jt not in lq.LLM_JOB_TYPES
        assert lq._FEATURE_BY_TYPE[jt] == "rfp_portal"


def test_portal_specs_wire_run_mark_status_label_and_message(monkeypatch):
    from app.services import rfp_portal_ingest as real

    mod, scans, harvests, marks = _install_rfp_portal(
        monkeypatch, scan_status=lambda t: "done", current_status=lambda t: "pending"
    )
    scan = lq._spec(lq.JOB_PORTAL_SCAN)
    assert scan.feature == "rfp_portal"
    scan.run({"run_id": "r1"})
    scan.mark("r1", {"status": "failed", "error": "boom"})
    assert scans == ["r1"] and marks == [("scan", "r1", "failed", "boom")]
    assert scan.current_status("r1") == "done"
    assert scan.model_label(_settings()) == "ngem" == real.MODEL_LABEL
    assert scan.error_message is real.error_message
    hv = lq._spec(lq.JOB_PORTAL_HARVEST)
    hv.run({"invitation_id": "i1"})
    hv.run({"invitation_id": "i2", "force": True})
    hv.mark("i1", {"status": "pending", "error": None})
    assert harvests == [("i1", False), ("i2", True)] and marks[-1] == ("harvest", "i1", "pending", None)
    assert hv.current_status("i1") == "pending" and hv.model_label(_settings()) == "ngem"
    assert hv.error_message(real.RfpPortalTransient("NGEM answered 503."), "ngem") == "NGEM answered 503."
    assert hv.error_message(ValueError("raw /tmp/x"), "ngem") == real._MSG_INTERRUPTED


@pytest.mark.parametrize(
    "flags",
    [
        {"rfp_ingest_enabled": True, "rfp_ngem_enabled": False},
        {"rfp_ingest_enabled": False, "rfp_ngem_enabled": True},
    ],
)
def test_claim_tick_portal_types_need_both_flags(monkeypatch, flags):
    db = _queue_env(monkeypatch)
    s = _portal_settings(**flags)
    lq.enqueue(lq.JOB_PORTAL_SCAN, target_id="r1", payload={"run_id": "r1"}, priority=150)
    lq.enqueue(lq.JOB_PORTAL_HARVEST, target_id="i1", payload={"invitation_id": "i1"}, priority=150)
    boq = lq.enqueue(lq.JOB_BOQ, target_id="a")
    claimed = lq._claim_tick(s, [])
    assert [j["id"] for j in claimed] == [boq["id"]]
    assert all(not (set(c.get("job_types", [])) & set(lq.PORTAL_JOB_TYPES)) for c in db.rpc_calls)


def test_claim_tick_portal_types_share_the_harvest_capacity(monkeypatch):
    db = _queue_env(monkeypatch)
    s = _portal_settings(rfp_harvest_enabled=False, rfp_harvest_concurrency=1, llm_queue_worker_concurrency=1)
    scan = lq.enqueue(lq.JOB_PORTAL_SCAN, target_id="r1", payload={"run_id": "r1"}, priority=150)
    hv = lq.enqueue(lq.JOB_PORTAL_HARVEST, target_id="i1", payload={"invitation_id": "i1"}, priority=150)
    first = lq._claim_tick(s, [])
    assert [j["id"] for j in first] == [scan["id"]]
    portal_pass = [*lq.PORTAL_JOB_TYPES, lq.JOB_RFP_CREATE_FILES]
    assert db.rpc_calls[-1]["job_types"] == portal_pass and db.rpc_calls[-1]["max_jobs"] == 1
    # A portal job in flight fills the one harvest slot: the pass is skipped.
    calls_before = len(db.rpc_calls)
    assert lq._claim_tick(s, [lq.JOB_PORTAL_SCAN]) == []
    assert all(c.get("job_types") != portal_pass for c in db.rpc_calls[calls_before:])
    assert _row(db, hv["id"])["status"] == "queued"
    # So does a Procore harvest in flight: one portal request stream per worker.
    assert lq._claim_tick(s, [lq.JOB_RFP_HARVEST]) == []
    # With the Procore slice on too, one pass claims all three types.
    both = _portal_settings(
        rfp_harvest_enabled=True, procore_login_email="bot@example.com", procore_login_password="pw",
        rfp_harvest_concurrency=2,
    )
    claimed = lq._claim_tick(both, [])
    assert [j["id"] for j in claimed] == [hv["id"]]
    assert db.rpc_calls[-1]["job_types"] == [
        lq.JOB_RFP_HARVEST, *lq.PORTAL_JOB_TYPES, lq.JOB_RFP_CREATE_FILES
    ]
    assert db.rpc_calls[-1]["max_jobs"] == 2


def test_execute_portal_scan_permanent_failure_marks_the_run(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    from app.services import rfp_portal_ingest as real

    def run(run_id):
        raise real.RfpPortalPermanent("The page is not the invitations grid.")

    _, _, _, marks = _install_rfp_portal(monkeypatch, scan=run)
    lq._execute(_running_row(db, job_id="s1", target="r1", attempts=1, job_type=lq.JOB_PORTAL_SCAN,
                             payload={"run_id": "r1"}))
    row = _row(db, "s1")
    assert row["status"] == "failed" and row["error_kind"] == "bad_input"
    assert marks == [("scan", "r1", "failed", "The page is not the invitations grid.")]


def test_execute_portal_harvest_transient_failure_requeues_with_its_own_sentence(monkeypatch):
    db = _queue_env(monkeypatch)
    monkeypatch.setattr(lq, "_now", lambda: NOW)
    from app.services import rfp_portal_ingest as real

    def run(invitation_id):
        raise real.RfpPortalTransient("NGEM answered 503; the harvest will be retried.")

    _, _, _, marks = _install_rfp_portal(monkeypatch, harvest=run)
    lq._execute(_running_row(db, job_id="h9", target="i1", attempts=1, job_type=lq.JOB_PORTAL_HARVEST,
                             payload={"invitation_id": "i1", "force": True}))
    row = _row(db, "h9")
    assert row["status"] == "queued" and row["error_kind"] == "infrastructure"
    assert row["last_error"] == "NGEM answered 503; the harvest will be retried."
    assert row["payload"]["force"] is True
    assert marks == [("harvest", "i1", "pending", None)]


def test_requeue_terminal_refuses_a_finished_portal_run(monkeypatch):
    db = _queue_env(monkeypatch)
    _install_rfp_portal(monkeypatch, scan_status=lambda t: "done")
    job = _running_row(db, job_id="s2", target="r2", attempts=1, job_type=lq.JOB_PORTAL_SCAN, payload={"run_id": "r2"})
    job["status"] = "failed"
    _row(db, "s2")["status"] = "failed"
    with pytest.raises(ValueError, match="since completed"):
        lq.requeue_terminal(job, "u1")


# ── llm_queue: rfp_create_files job type (docs/RFP_CREATE.md 5) ───────────


def test_rfp_create_files_is_a_third_pass_job_type_but_never_an_llm_one():
    assert lq.JOB_RFP_CREATE_FILES == "rfp_create_files"
    assert lq.CREATE_JOB_TYPES == (lq.JOB_RFP_CREATE_FILES,)
    assert lq.JOB_RFP_CREATE_FILES in lq.THIRD_PASS_JOB_TYPES
    assert lq.JOB_RFP_CREATE_FILES in lq.NON_LLM_JOB_TYPES
    assert lq.JOB_RFP_CREATE_FILES not in lq.LLM_JOB_TYPES
    assert lq.JOB_RFP_CREATE_FILES not in lq.HARVEST_JOB_TYPES   # not a platform session
    assert lq._FEATURE_BY_TYPE[lq.JOB_RFP_CREATE_FILES] == "rfp_create"


def test_rfp_create_files_spec_wires_run_mark_status_label_and_message(monkeypatch):
    from app.services import rfp_create_files as real

    runs, marks = [], []
    monkeypatch.setattr(real, "execute",
                        lambda project_id, harvest_id=None: runs.append((project_id, harvest_id)))
    monkeypatch.setattr(real, "mark_from_queue", lambda p, s, err: marks.append((p, s, err)))
    monkeypatch.setattr(real, "current_status", lambda p: "done")
    spec = lq._spec(lq.JOB_RFP_CREATE_FILES)
    assert spec.feature == "rfp_create"
    spec.run({"project_id": "p1"})
    # The harvest the payload names wins over the record's (RFP_CREATE.md 4.6).
    spec.run({"project_id": "p1", "harvest_id": "hv-9"})
    assert runs == [("p1", None), ("p1", "hv-9")]
    spec.mark("p1", {"status": "pending", "error": None})
    spec.mark("p1", {"status": "failed", "error": "boom"})
    assert marks == [("p1", "pending", None), ("p1", "failed", "boom")]
    assert spec.current_status("p1") == "done"
    assert spec.model_label(_settings()) == "storage" == real.MODEL_LABEL
    assert spec.error_message is real.error_message
    assert spec.error_message(real.RfpCreateFilesTransient("Storage did not answer."), "storage") == (
        "Storage did not answer."
    )
    assert spec.error_message(ValueError("raw /tmp/x"), "storage") == real._MSG_INTERRUPTED


def test_claim_tick_promotion_type_needs_only_the_master_switch(monkeypatch):
    db = _queue_env(monkeypatch)
    job = lq.enqueue(lq.JOB_RFP_CREATE_FILES, target_id="p1", payload={"project_id": "p1"}, priority=160)
    # Harvest and NGEM off: the third pass still runs for the promotion alone.
    s = _harvest_settings(rfp_harvest_enabled=False, rfp_ngem_enabled=False)
    claimed = lq._claim_tick(s, [])
    assert [j["id"] for j in claimed] == [job["id"]]
    assert db.rpc_calls[-1]["job_types"] == [lq.JOB_RFP_CREATE_FILES]
    # The master switch off: nothing names the type.
    db.tables["llm_jobs"][0]["status"] = "queued"
    off = _harvest_settings(rfp_ingest_enabled=False)
    calls_before = len(db.rpc_calls)
    assert lq._claim_tick(off, []) == []
    assert all(lq.JOB_RFP_CREATE_FILES not in c.get("job_types", []) for c in db.rpc_calls[calls_before:])

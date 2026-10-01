"""The RFP Ingestion Sandbox router (app/routers/rfp_ingest) called as plain
functions against an in-memory fake Supabase, with the service's storage,
queue and runner collaborators stubbed. No DB, no network, no child process:
`rfp_sandbox_runner.run_sandbox` is a landmine (a router test must never
spawn), the two RFP buckets are a recorder, and llm_queue's enqueue / cancel /
active_job / poll_info are captured.

What is pinned (docs/RFP_INGESTION_SANDBOX.md sections 3.8 and 5):

- The route table: exactly the fourteen (method, path) pairs of the spec,
  every one carrying the env-flag gate (router-level, so it resolves before
  auth), `require_dev` in its signature and exactly one rate limiter (the
  tool's own on reads, create, cancel and delete; `ai_rate_limit` on start and
  retry; `upload_rate_limit` on the multipart upload); the literal sub-paths
  registered before /files/{file_id}; mounted on the shared spine with no
  sub-app guard (it must not ride the Bidding flag, splitter precedent).
- The flag: with RFP_INGEST_ENABLED=false every route 404s through a
  TestClient with no credentials and the bare "Not Found" body; with it on
  the same requests 401 (mounted, merely asking for a token).
- The lifespan: with the flag on boot runs the self-test once off the event
  loop and logs loudly (never raises) when it fails, errors or overruns the
  boot bound; with the flag off it never runs; the teardown sets
  SHUTTING_DOWN either way, and BEFORE the queue task is cancelled.
- Uploads: a real PDF is quarantined (row before object), a not_pdf /
  polyglot / empty upload is RECORDED as rejected with no object and still
  answered normally, a duplicate is a 409 naming the winning file, uploads
  are refused unless the run is staging (409), over the per-run file cap
  (413) and over upload_max_bytes before the service is touched (413).
- Start: 409 for an empty or all-rejected run or one already started; the
  audit payload {file_count, rejected}; dispatch to the queue at the sandbox
  priority, or the BackgroundTasks fallback when the queue is off.
- Email runs: 404 unknown email, 409 no attachments, 413 over the cap, and
  the happy path (pending, one file per attachment, dispatched, audited).
- Cancel / retry / delete state rules mapped to 409; the queued job canceled
  after the sticky mark; retry refusing an active job and putting the run
  back on an enqueue collision; delete removing the row and both prefixes.
- Reads: run detail with files (manifest excluded) and queue poll info,
  {rows, total, offset, limit} pages, thumb_url on pages, download names on
  file URLs, 400 on an unknown status filter, 404s for missing rows.
- Every path id goes through the uuid guard (404, DB untouched), including
  the spellings Python's uuid parser accepts but Postgres refuses with 22P02
  (urn:uuid:, uuid:, braces, hyphen-less); `q` on /emails goes through the
  emails router's sanitizer; a transient service fault is a 503; every detail
  string is the service's app-authored sentence.
- /status never lets a read storm turn into a child storm: a plain read only
  ever serves the last report (`cached` true) or builds the cheap one, which
  carries this worker's last self-test verdict and spawns nothing (pinned
  against the service's real `status_report` too); ?self_test=1 is the only
  caller that asks for a fresh child; only one report is built per worker at
  a time, and a reader that waits out that gate takes the last report or a
  503 when this worker has none.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.datastructures import Headers

from app.core.config import Settings, get_settings
from app.core.deps import CurrentUser, require_dev
from app.core.features import require_rfp_ingest
from app.core.ratelimit import ai_rate_limit, rfp_ingest_rate_limit, upload_rate_limit
from app.core.roles import Role
from app.routers import rfp_ingest as rr
from app.routers.emails import _sanitize_query
from app.sandbox import protocol
from app.services import llm_queue, notifications
from app.services import rfp_ingest as ri
from app.services import rfp_ingest_storage as rs
from app.services import rfp_sandbox_runner as runner

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
PDF = runner.embedded_selftest_pdf()
ENTITY = "rfp_ingest_run"

# Path ids must be uuid-shaped: the router refuses anything else before any
# query runs, and the fake database returns exactly what was seeded.
RUN1 = "5b0e7d0a-0000-4000-8000-000000000001"
RUN2 = "5b0e7d0a-0000-4000-8000-000000000002"
RUN3 = "5b0e7d0a-0000-4000-8000-000000000003"
RUN4 = "5b0e7d0a-0000-4000-8000-000000000004"
FILE1 = "5b0e7d0a-0000-4000-8000-000000000101"
FILE2 = "5b0e7d0a-0000-4000-8000-000000000102"
FILE3 = "5b0e7d0a-0000-4000-8000-000000000103"
PAGE0 = "5b0e7d0a-0000-4000-8000-000000000201"
PAGE1 = "5b0e7d0a-0000-4000-8000-000000000202"
EMAIL1 = "5b0e7d0a-0000-4000-8000-000000000301"
EMAIL2 = "5b0e7d0a-0000-4000-8000-000000000302"
MISSING = "5b0e7d0a-0000-4000-8000-00000000ffff"
NOT_A_UUID = "not-a-uuid"
# Spellings Python's uuid parser accepts and Postgres' uuid input does not
# (22P02, an unhandled 500 with its CORS headers gone): they must be refused
# by the guard exactly like plain garbage. The braced and hyphen-less forms
# Postgres would accept are refused too, fail-closed: only the canonical
# spelling the frontend emits gets through.
NON_CANONICAL_IDS = (
    NOT_A_UUID,
    f"urn:uuid:{RUN1}",
    f"uuid:{RUN1}",
    f"urn:{RUN1}",
    "{" + RUN1 + "}",
    RUN1.replace("-", ""),
)


# ── Fake Supabase ────────────────────────────────────────────────────────


def _ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return value


class _Query:
    """PostgREST-shaped builder over FakeDB.tables. Column lists ARE honored
    (a select of "id, status" answers only those keys), which is what lets
    the tests prove the manifest jsonb stays out of the run view."""

    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._op = None
        self._payload = None
        self._filters = []
        self._orders = []
        self._limit = None
        self._range = None
        self._count = None
        self._columns = None

    def select(self, sel="*", count=None, **k):
        self._op = "select"
        self._count = count
        sel = (sel or "*").strip()
        self._columns = None if sel == "*" else [c.strip() for c in sel.split(",") if c.strip()]
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

    def neq(self, col, val):
        self._filters.append(("neq", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, list(vals)))
        return self

    def lt(self, col, val):
        self._filters.append(("lt", col, val))
        return self

    def is_(self, col, val):
        self._filters.append(("is", col, val))
        return self

    def or_(self, expr):
        self._filters.append(("or", None, expr))
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

    @staticmethod
    def _or_matches(row, expr):
        for clause in expr.split(","):
            col, op, val = clause.split(".", 2)
            if op == "ilike" and val.strip("%").lower() in str(row.get(col) or "").lower():
                return True
        return False

    def _matches(self, row):
        for op, col, val in self._filters:
            have = row.get(col)
            if op == "eq" and have != val:
                return False
            if op == "neq" and have == val:
                return False
            if op == "in" and have not in val:
                return False
            if op == "lt" and (have is None or not _ts(have) < _ts(val)):
                return False
            if op == "is" and val == "null" and have is not None:
                return False
            if op == "or" and not self._or_matches(row, val):
                return False
        return True

    def _project(self, row):
        if self._columns is None:
            return dict(row)
        return {k: row.get(k) for k in self._columns}

    def _check_unique(self, row, rows):
        """The partial unique index on rfp_ingest_files (run_id, sha256)."""
        if self.table != ri.FILES_TABLE or row.get("sha256") is None:
            return
        for other in rows:
            if (
                other is not row
                and other.get("run_id") == row.get("run_id")
                and other.get("sha256") == row.get("sha256")
            ):
                raise Exception(
                    "duplicate key value violates unique constraint "
                    '"rfp_ingest_files_run_sha_uidx" (code 23505)'
                )

    def _insert_one(self, payload, rows):
        row = dict(payload)
        row.setdefault("id", uuid.uuid4().hex)
        row.setdefault("created_at", self.db.next_created_at())
        self._check_unique(row, rows)
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
            return SimpleNamespace(
                data=[self._project(r) for r in hits], count=total if self._count else None
            )
        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            return SimpleNamespace(data=[self._insert_one(p, rows) for p in payloads])
        if self._op == "update":
            matched = [r for r in rows if self._matches(r)]
            for r in matched:
                self._check_unique({**r, **self._payload}, [x for x in rows if x is not r])
            out = []
            for r in matched:
                r.update(self._payload)
                out.append(dict(r))
            return SimpleNamespace(data=out)
        if self._op == "delete":
            gone = [dict(r) for r in rows if self._matches(r)]
            self.db.tables[self.table] = [r for r in rows if not self._matches(r)]
            return SimpleNamespace(data=gone)
        return SimpleNamespace(data=[])


class FakeDB:
    def __init__(self, tables=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self._seq = 0

    def next_created_at(self):
        self._seq += 1
        return (NOW + timedelta(seconds=self._seq)).isoformat()

    def table(self, name):
        return _Query(self, name)

    def row(self, table, row_id):
        return next(r for r in self.tables[table] if r["id"] == row_id)

    def files(self, run_id):
        return sorted(
            (r for r in self.tables.get(ri.FILES_TABLE, []) if r["run_id"] == run_id),
            key=lambda r: r["created_at"],
        )


class _Boom:
    """A database that must never be reached (the uuid guard runs first)."""

    def table(self, *_a, **_k):  # pragma: no cover - reaching here is the failure
        raise AssertionError("the DB must not be touched for a malformed id")


# ── Fake storage / queue; environment ────────────────────────────────────


class _FakeRs:
    def __init__(self):
        self.uploads = []       # upload_bytes: (bucket, path, data, content_type)
        self.deleted = []       # delete_prefix: (bucket, prefix)
        self.signed = []        # signed_url: (bucket, path, download)

    def upload_bytes(self, bucket, path, data, content_type):
        self.uploads.append((bucket, path, bytes(data), content_type))

    def delete_prefix(self, bucket, prefix, *, keep=()):
        self.deleted.append((bucket, prefix))
        return 0

    def signed_url(self, bucket, path, *, download=None):
        self.signed.append((bucket, path, download))
        return f"https://signed.test/{bucket}/{path}"


def _settings(tmp_path, **over):
    over.setdefault("llm_queue_enabled", True)
    return Settings(
        _env_file=None,
        supabase_url="https://sb.test",
        supabase_service_role_key="svc",
        rfp_ingest_scratch_dir=str(tmp_path / "scratch"),
        **over,
    )


def _user(uid="u1", is_dev=True):
    return CurrentUser(id=uid, email="dev@g3.com", role=Role.IT_ADMIN, is_active=True, is_dev=is_dev)


def _env(monkeypatch, tmp_path, tables=None, **over):
    """Point the router AND the service at the fake; capture audits, bells and
    queue calls; forbid every storage transfer the router path must not make
    and any child spawn."""
    db = FakeDB(tables)
    s = _settings(tmp_path, **over)
    store = _FakeRs()
    env = SimpleNamespace(
        db=db, settings=s, store=store, audits=[], bells=[],
        queue=SimpleNamespace(enqueued=[], canceled=[], active=None, poll=None),
    )
    monkeypatch.setattr(ri, "get_supabase", lambda: db)
    monkeypatch.setattr(ri, "get_settings", lambda: s)
    monkeypatch.setattr(rr, "get_settings", lambda: s)
    monkeypatch.setattr(
        rr, "audit",
        lambda actor, action, entity, entity_id, payload: env.audits.append(
            (actor, action, entity, entity_id, payload)
        ),
    )
    monkeypatch.setattr(ri, "_disk_free", lambda path: 10**12)
    monkeypatch.setattr(ri, "_last_self_test", None)
    ri.SHUTTING_DOWN.clear()
    for name in ("upload_bytes", "delete_prefix", "signed_url"):
        monkeypatch.setattr(rs, name, getattr(store, name))
    for name in ("upload_file", "upload_many", "download_to_file"):
        monkeypatch.setattr(
            rs, name, lambda *a, _n=name, **k: pytest.fail(f"rs.{_n} must not run in a router test")
        )
    monkeypatch.setattr(
        notifications, "notify_user",
        lambda user_id, project_id, type_, message, **kw: env.bells.append(
            {"user_id": user_id, "type": type_, "message": message, **kw}
        ),
    )
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: True)
    monkeypatch.setattr(llm_queue, "requeue_self", lambda job=None: True)
    monkeypatch.setattr(llm_queue, "active_job", lambda jt, t: env.queue.active)
    monkeypatch.setattr(
        llm_queue, "enqueue",
        lambda jt, **kw: env.queue.enqueued.append({"job_type": jt, **kw}) or {"id": "job1"},
    )
    monkeypatch.setattr(
        llm_queue, "cancel", lambda job_id: env.queue.canceled.append(job_id) or {"id": job_id}
    )
    monkeypatch.setattr(llm_queue, "poll_info", lambda jt, t: env.queue.poll)
    monkeypatch.setattr(
        runner, "run_sandbox",
        lambda *a, **k: pytest.fail("a router test must never spawn the sandbox"),
    )
    return env


# ── Seeding helpers ──────────────────────────────────────────────────────


def _run_row(run_id=RUN1, status=protocol.RUN_STAGING, source_kind="upload", **over):
    row = {
        "id": run_id, "source_kind": source_kind, "email_id": None, "status": status,
        "error": None, "file_count": 0, "files_verified": 0, "files_gapped": 0,
        "files_rejected": 0, "files_failed": 0, "pages_total": 0, "limits": None,
        "limits_hash": None, "sandbox_version": None, "protocol_version": None,
        "created_by": "u1", "started_at": None, "completed_at": None,
        "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
    }
    row.update(over)
    return row


def _file_row(file_id, run_id=RUN1, status=protocol.STATUS_PENDING, seq=0, **over):
    row = {
        "id": file_id, "run_id": run_id, "source_attachment_id": None,
        "filename": f"{file_id[-3:]}.pdf", "declared_mime": "application/pdf",
        "size_bytes": len(PDF), "sha256": None,
        "quarantine_path": f"{run_id}/{file_id}/source.pdf", "status": status,
        "reject_code": None, "error": None, "claim_token": None, "phase": None,
        "pages_done": 0, "last_progress_at": None, "child_pid": None, "page_count": None,
        "pages_ok": None, "pages_failed": None, "hazards": None, "manifest": None,
        "derived_prefix": None, "images_pdf_paths": None, "text_path": None, "restarts": 0,
        "elapsed_ms": None, "sandbox_version": None, "protocol_version": None,
        "limits_hash": None, "started_at": None, "finished_at": None,
        "source_format": "pdf", "converted_path": None,
        "created_at": (NOW + timedelta(seconds=seq)).isoformat(),
        "updated_at": NOW.isoformat(),
    }
    row.update(over)
    return row


def _email_row(email_id=EMAIL1, **over):
    row = {
        "id": email_id, "mailbox": "bids@g3.test", "subject": "RFP: Kiel Ranch",
        "from_name": "GC Estimating", "from_address": "bids@gc.example", "direction": "inbound",
        "message_at": NOW.isoformat(), "status": "unknown", "has_attachments": True,
        "project_id": None, "graph_message_id": "msg-1", "created_at": NOW.isoformat(),
    }
    row.update(over)
    return row


def _attachment(att_id, email_id=EMAIL1, **over):
    row = {
        "id": att_id, "email_id": email_id, "graph_attachment_id": f"graph-{att_id}",
        "filename": f"{att_id}.pdf", "mime_type": "application/pdf", "size_bytes": 10,
        "storage_path": None, "skipped_reason": None, "created_at": NOW.isoformat(),
    }
    row.update(over)
    return row


def _upload(name, content, content_type="application/pdf"):
    headers = Headers({"content-type": content_type}) if content_type else None
    return UploadFile(io.BytesIO(content), filename=name, size=len(content), headers=headers)


def _post_file(run_id, upload, user=None):
    return asyncio.run(rr.upload_run_file(run_id, file=upload, user=user or _user()))


def _raises(status_code, fn, *args, **kwargs) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        fn(*args, **kwargs)
    assert exc.value.status_code == status_code, exc.value.detail
    return exc.value


# ── Route table ──────────────────────────────────────────────────────────

SPEC_ROUTES = {
    ("GET", "/rfp-ingest/status"),
    ("GET", "/rfp-ingest/emails"),
    ("POST", "/rfp-ingest/runs"),
    ("POST", "/rfp-ingest/runs/{run_id}/files"),
    ("POST", "/rfp-ingest/runs/{run_id}/start"),
    ("GET", "/rfp-ingest/runs"),
    ("GET", "/rfp-ingest/runs/{run_id}"),
    ("GET", "/rfp-ingest/files/{file_id}"),
    ("GET", "/rfp-ingest/files/{file_id}/pages"),
    ("GET", "/rfp-ingest/pages/{page_id}/urls"),
    ("GET", "/rfp-ingest/files/{file_id}/urls"),
    ("POST", "/rfp-ingest/runs/{run_id}/cancel"),
    ("POST", "/rfp-ingest/runs/{run_id}/retry"),
    ("DELETE", "/rfp-ingest/runs/{run_id}"),
}
_LIMITER_EXCEPTIONS = {
    ("POST", "/rfp-ingest/runs/{run_id}/files"): upload_rate_limit,
    ("POST", "/rfp-ingest/runs/{run_id}/start"): ai_rate_limit,
    ("POST", "/rfp-ingest/runs/{run_id}/retry"): ai_rate_limit,
}
_LIMITERS = {ai_rate_limit, upload_rate_limit, rfp_ingest_rate_limit}


def test_route_table_matches_the_design_record_exactly():
    assert rr.router.prefix == "/rfp-ingest"
    found = {(m, r.path) for r in rr.router.routes for m in r.methods}
    assert found == SPEC_ROUTES
    created = {r.path: r.status_code for r in rr.router.routes if "POST" in r.methods}
    # Opening a run and recording a file (rejected at sniff or not) are 201s.
    assert created["/rfp-ingest/runs"] == 201
    assert created["/rfp-ingest/runs/{run_id}/files"] == 201


def test_every_route_carries_the_flag_gate_require_dev_and_one_rate_limiter():
    for route in rr.router.routes:
        calls = {d.call for d in route.dependant.dependencies}
        assert require_rfp_ingest in calls, f"{route.path} missing the env-flag gate"
        assert require_dev in calls, f"{route.path} must be dev-only"
        for method in route.methods:
            expected = _LIMITER_EXCEPTIONS.get((method, route.path), rfp_ingest_rate_limit)
            assert expected in calls, f"{method} {route.path} has the wrong limiter"
            assert len(calls & _LIMITERS) == 1, f"{method} {route.path} limiter count"


def test_literal_sub_paths_are_registered_before_the_file_param_route():
    order = [r.path for r in rr.router.routes]
    bare = order.index("/rfp-ingest/files/{file_id}")
    assert order.index("/rfp-ingest/files/{file_id}/pages") < bare
    assert order.index("/rfp-ingest/files/{file_id}/urls") < bare


def test_router_is_mounted_on_the_spine_without_a_sub_app_guard():
    import app.main as app_main

    mounted = [r for r in app_main.app.routes if getattr(r, "path", "").startswith("/rfp-ingest")]
    assert {r.path for r in mounted} == {r.path for r in rr.router.routes}
    for route in mounted:
        calls = {d.call for d in route.dependant.dependencies}
        assert require_rfp_ingest in calls, route.path
        # Not under _BIDDING / _PM / _CP: switching a sub-app off must not
        # take the sandbox bench with it (and vice versa).
        assert "require_feature.<locals>._dep" not in {c.__qualname__ for c in calls}, route.path


# ── The flag, through a TestClient with no credentials ───────────────────


@contextmanager
def _flag(enabled: bool):
    previous = os.environ.get("RFP_INGEST_ENABLED")
    os.environ["RFP_INGEST_ENABLED"] = "true" if enabled else "false"
    get_settings.cache_clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("RFP_INGEST_ENABLED", None)
        else:
            os.environ["RFP_INGEST_ENABLED"] = previous
        get_settings.cache_clear()


@pytest.fixture(scope="module")
def client() -> TestClient:
    import app.main

    # No context manager: the lifespan (and its self-test) never runs here.
    return TestClient(app.main.app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/rfp-ingest/status"),
        ("GET", "/rfp-ingest/emails"),
        ("GET", "/rfp-ingest/runs"),
        ("POST", "/rfp-ingest/runs"),
        ("POST", f"/rfp-ingest/runs/{RUN1}/files"),
        ("POST", f"/rfp-ingest/runs/{RUN1}/start"),
        ("GET", f"/rfp-ingest/runs/{RUN1}"),
        ("GET", f"/rfp-ingest/files/{FILE1}/pages"),
        ("POST", f"/rfp-ingest/runs/{RUN1}/retry"),
        ("DELETE", f"/rfp-ingest/runs/{RUN1}"),
    ],
)
def test_flag_off_404s_before_auth_and_flag_on_asks_for_a_token(client, method, path):
    """404 with the bare body = the router-level gate fired before any token
    (or body) was read; 401 = mounted and merely wanting credentials."""
    with _flag(False):
        resp = client.request(method, path)
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Not Found"}
    with _flag(True):
        assert client.request(method, path).status_code == 401


# ── Lifespan: boot self-test and the shutdown event ──────────────────────


def _boot_settings(**over):
    """Settings under which only the sandbox branch of the lifespan is live:
    no Supabase URL and no Graph creds skip every poller by its own
    precondition, so the test observes the self-test call alone."""
    over.setdefault("rfp_ingest_enabled", True)
    over.setdefault("supabase_url", "")
    return Settings(
        _env_file=None, ms_client_id="", llm_health_enabled=False, email_ingest_enabled=False,
        due_digest_enabled=False, due_reminders_enabled=False, **over,
    )


def _boot(monkeypatch, settings, self_test):
    """Run app.main's lifespan once. Returns how long the enter took, what
    SHUTTING_DOWN read inside the app's lifetime, and after the teardown."""
    import app.main as app_main

    monkeypatch.setattr(app_main, "settings", settings)
    monkeypatch.setattr(ri, "self_test", self_test)
    ri.SHUTTING_DOWN.clear()
    seen = {}

    async def go():
        started = time.monotonic()
        async with app_main.lifespan(app_main.app):
            seen["enter_seconds"] = time.monotonic() - started
            seen["inside"] = ri.SHUTTING_DOWN.is_set()
            # Let any background task the lifespan created take its first
            # step (a task cancelled before it ever ran never sees the
            # CancelledError the ordering test below observes).
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    try:
        asyncio.run(go())
    finally:
        seen["after"] = ri.SHUTTING_DOWN.is_set()
        ri.SHUTTING_DOWN.clear()
    return seen


def test_lifespan_runs_the_self_test_once_when_the_flag_is_on(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="app.main")
    calls = []

    def self_test():
        calls.append(threading.current_thread() is threading.main_thread())
        return {"ok": True, "detail": "fine", "elapsed_ms": 7, "uid_switch_applied": False}

    seen = _boot(monkeypatch, _boot_settings(), self_test)
    assert calls == [False]                       # once, and off the event loop's thread
    assert seen["inside"] is False and seen["after"] is True
    assert "rfp ingest self-test ok in 7 ms (uid switch not applied)" in caplog.text


def test_lifespan_never_runs_the_self_test_while_the_flag_is_off(monkeypatch):
    seen = _boot(
        monkeypatch,
        _boot_settings(rfp_ingest_enabled=False),
        lambda: pytest.fail("the self-test must not run with the flag off"),
    )
    # The shutdown event is set regardless: it is inert with nothing running.
    assert seen["after"] is True


def test_lifespan_logs_a_failed_self_test_loudly_and_still_boots(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.main")
    seen = _boot(
        monkeypatch, _boot_settings(),
        lambda: {"ok": False, "detail": ri._MSG_SELF_TEST_ERROR, "elapsed_ms": 3},
    )
    assert seen["after"] is True
    assert "RFP INGEST SELF-TEST FAILED" in caplog.text
    assert ri._MSG_SELF_TEST_ERROR in caplog.text
    assert "runs will be refused" in caplog.text


def test_lifespan_survives_a_self_test_that_raises(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.main")

    def boom():
        raise RuntimeError("scratch root vanished")

    seen = _boot(monkeypatch, _boot_settings(), boom)
    assert seen["after"] is True
    assert "RFP INGEST SELF-TEST could not run" in caplog.text


def test_lifespan_never_waits_past_the_boot_bound_for_the_self_test(monkeypatch, caplog):
    """A hung spawn must not hold the worker's boot: past the bound the app
    comes up and the thread finishes on its own (caching its verdict)."""
    import app.main as app_main

    caplog.set_level(logging.ERROR, logger="app.main")
    monkeypatch.setattr(app_main, "_RFP_SELF_TEST_BOOT_SECONDS", 0.05)
    finished = threading.Event()

    def slow():
        time.sleep(0.3)
        finished.set()
        return {"ok": True}

    seen = _boot(monkeypatch, _boot_settings(), slow)
    assert seen["enter_seconds"] < 0.25
    assert "RFP INGEST SELF-TEST did not finish within" in caplog.text
    assert finished.wait(2.0)                    # join the stray thread before moving on


def test_lifespan_sets_shutting_down_before_cancelling_the_queue_task(monkeypatch):
    """The runner's monitor loop reads the event from its worker thread (a
    CancelledError never reaches a thread), so the event must already be set
    when the queue task receives its cancellation."""
    seen = {}

    async def fake_worker_loop():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            seen["set_at_cancel"] = ri.SHUTTING_DOWN.is_set()
            raise

    monkeypatch.setattr(llm_queue, "worker_loop", fake_worker_loop)
    settings = _boot_settings(
        rfp_ingest_enabled=False, supabase_url="https://sb.test", llm_queue_enabled=True
    )
    boot = _boot(monkeypatch, settings, lambda: pytest.fail("flag off"))
    assert seen == {"set_at_cancel": True}
    assert boot["after"] is True


# ── Uploads ──────────────────────────────────────────────────────────────


def test_upload_of_a_real_pdf_is_quarantined_and_answered_with_its_row(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row()]})
    row = _post_file(RUN1, _upload("Spec Book.pdf", PDF))
    assert row["status"] == protocol.STATUS_PENDING
    assert row["run_id"] == RUN1 and row["filename"] == "Spec Book.pdf"
    assert row["declared_mime"] == "application/pdf"       # recorded, never trusted
    assert row["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert row["size_bytes"] == len(PDF)
    assert row["quarantine_path"] == f"{RUN1}/{row['id']}/source.pdf"
    assert env.store.uploads == [
        (rs.QUARANTINE_BUCKET, row["quarantine_path"], PDF, "application/pdf")
    ]
    assert row["manifest"]["sniff"]["verdict"] is None
    assert env.db.row(ri.RUNS_TABLE, RUN1)["file_count"] == 1
    # Uploads are not audited (only create/start/cancel/retry/delete are).
    assert env.audits == [] and env.queue.enqueued == []


@pytest.mark.parametrize(
    "content,code",
    [
        (b"hello world, definitely not a pdf", protocol.REJECT_NOT_PDF),
        (b"PK\x03\x04" + PDF, protocol.REJECT_POLYGLOT),    # foreign magic at offset 0
        (b"", protocol.REJECT_EMPTY),
    ],
)
def test_upload_rejected_at_sniff_is_recorded_without_an_object(monkeypatch, tmp_path, content, code):
    """Rejected-at-sniff files are RECORDED (status rejected, no quarantine
    object) and answered normally, so the run's history shows them."""
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row()]})
    row = _post_file(RUN1, _upload("evil.pdf", content))
    assert row["status"] == protocol.STATUS_REJECTED
    assert row["reject_code"] == code
    assert row["error"] == protocol.VERDICT_MESSAGES[code]      # app-authored, never the name
    assert row["quarantine_path"] is None and row["finished_at"]
    assert env.store.uploads == []
    assert env.db.row(ri.FILES_TABLE, row["id"])["status"] == protocol.STATUS_REJECTED
    assert env.db.row(ri.RUNS_TABLE, RUN1)["file_count"] == 1


def test_upload_of_duplicate_content_is_a_409_naming_the_winner(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row()]})
    first = _post_file(RUN1, _upload("a.pdf", PDF))
    exc = _raises(409, _post_file, RUN1, _upload("b.pdf", PDF))
    assert exc.detail == (
        f"{protocol.VERDICT_MESSAGES[protocol.REJECT_DUPLICATE]} (existing file {first['id']})"
    )
    assert [r["id"] for r in env.db.tables[ri.FILES_TABLE]] == [first["id"]]
    assert len(env.store.uploads) == 1                       # no second object, no orphan
    assert env.db.row(ri.RUNS_TABLE, RUN1)["file_count"] == 1


def test_uploads_are_refused_unless_the_run_is_staging(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row(RUN1, status=protocol.RUN_PENDING),
                        _run_row(RUN2, status=protocol.RUN_DONE)],
    })
    for run_id in (RUN1, RUN2):
        exc = _raises(409, _post_file, run_id, _upload("a.pdf", PDF))
        assert "staging" in exc.detail
    _raises(404, _post_file, MISSING, _upload("a.pdf", PDF))
    assert env.store.uploads == [] and env.db.tables.get(ri.FILES_TABLE, []) == []


def test_upload_over_the_per_run_file_cap_is_a_413(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row(file_count=1)],
        ri.FILES_TABLE: [_file_row(FILE1)],
    }, rfp_ingest_max_files_per_run=1)
    exc = _raises(413, _post_file, RUN1, _upload("b.pdf", PDF))
    assert "per-run limit" in exc.detail
    assert len(env.db.tables[ri.FILES_TABLE]) == 1 and env.store.uploads == []


def test_upload_over_upload_max_bytes_is_refused_before_the_service(monkeypatch, tmp_path):
    """_read_capped 413s in the request path; the service (and the DB) are
    never reached, so nothing is recorded for a body that was refused."""
    _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row()]},
         upload_max_bytes=1024, rfp_ingest_max_file_bytes=1024)
    monkeypatch.setattr(
        ri, "add_upload_file", lambda *a, **k: pytest.fail("the service must not see the body")
    )
    exc = _raises(413, _post_file, RUN1, _upload("big.pdf", b"x" * 2048))
    assert "too large" in exc.detail


OOXML = b"PK\x03\x04\x14\x00\x06\x00" + b"[Content_Types].xml" + b"\x00" * 200
OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.mark.parametrize(
    "name, content, fmt, ctype",
    [
        ("Bid Form.docx", OOXML, "docx", DOCX_MIME),
        ("Schedule.xlsx", OOXML, "xlsx",
         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("Old Spec.doc", OLE2, "doc", "application/msword"),
        ("Takeoff.xls", OLE2, "xls", "application/vnd.ms-excel"),
    ],
)
def test_upload_of_an_office_file_is_quarantined_under_its_format(
    monkeypatch, tmp_path, name, content, fmt, ctype
):
    """Spec 2.1: the upload route takes Word and Excel files through the
    same request; the service records the sniffed format and stores the raw
    bytes under `source.<ext>` with the format's content type."""
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row()]})
    row = _post_file(RUN1, _upload(name, content, content_type="application/octet-stream"))
    assert row["status"] == protocol.STATUS_PENDING
    assert row["source_format"] == fmt
    assert row["quarantine_path"] == f"{RUN1}/{row['id']}/source.{fmt}"
    assert env.store.uploads == [(rs.QUARANTINE_BUCKET, row["quarantine_path"], content, ctype)]
    assert row["manifest"]["sniff"]["verdict"] is None
    assert row["manifest"]["sniff"]["source_format"] == fmt
    assert row["manifest"]["identity"]["source_format"] == fmt
    assert row["declared_mime"] == "application/octet-stream"     # recorded, never trusted


def test_upload_of_a_zip_named_pdf_is_still_not_pdf_with_the_new_sentence(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row()]})
    row = _post_file(RUN1, _upload("Bid Form.pdf", OOXML))
    assert row["status"] == protocol.STATUS_REJECTED
    assert row["reject_code"] == protocol.REJECT_NOT_PDF
    assert row["error"] == "The file is not a PDF, Word or Excel file."
    assert env.store.uploads == []


def test_upload_of_an_office_file_with_the_setting_off_is_not_pdf(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row()]},
               rfp_ingest_office_files_enabled=False)
    row = _post_file(RUN1, _upload("Bid Form.docx", OOXML, content_type=DOCX_MIME))
    assert row["status"] == protocol.STATUS_REJECTED
    assert row["reject_code"] == protocol.REJECT_NOT_PDF
    assert env.store.uploads == []


def test_upload_without_a_filename_gets_the_default_name(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row()]})
    row = _post_file(RUN1, _upload(None, PDF, content_type=None))
    assert row["filename"] == "upload.pdf" and row["declared_mime"] is None


# ── Runs: create ─────────────────────────────────────────────────────────


def test_create_upload_run_answers_a_staging_detail_and_audits(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path)
    background = BackgroundTasks()
    out = rr.create_run(rr.RunCreateIn(source="upload"), background, user=_user())
    assert out["status"] == protocol.RUN_STAGING and out["source_kind"] == "upload"
    assert out["created_by"] == "u1" and out["file_count"] == 0
    assert out["files"] == [] and out["queue"] is None
    assert env.audits == [("u1", "rfp_ingest.create", ENTITY, out["id"], {"source": "upload"})]
    # Nothing is dispatched until /start.
    assert env.queue.enqueued == [] and background.tasks == []


def test_run_create_body_rules():
    assert rr.RunCreateIn(source="upload").email_id is None
    assert rr.RunCreateIn(source="email", email_id=EMAIL1).email_id == EMAIL1
    with pytest.raises(ValidationError, match="email_id is required"):
        rr.RunCreateIn(source="email")
    with pytest.raises(ValidationError):
        rr.RunCreateIn(source="dropbox")


def test_create_email_run_dispatches_and_answers_the_pending_detail(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {
        "ingested_emails": [_email_row()],
        "ingested_email_attachments": [_attachment("a1"), _attachment("a2")],
    })
    env.queue.poll = {"state": "queued", "position": 1}
    background = BackgroundTasks()
    out = rr.create_run(rr.RunCreateIn(source="email", email_id=EMAIL1), background, user=_user())
    assert out["status"] == protocol.RUN_PENDING and out["source_kind"] == "email"
    assert out["email_id"] == EMAIL1 and out["file_count"] == 2
    assert [f["source_attachment_id"] for f in out["files"]] == ["a1", "a2"]
    assert all(f["status"] == protocol.STATUS_PENDING for f in out["files"])
    assert all(f["quarantine_path"] is None and f["size_bytes"] is None for f in out["files"])
    assert out["queue"] == {"state": "queued", "position": 1}
    assert env.queue.enqueued == [{
        "job_type": llm_queue.JOB_RFP_INGEST, "target_id": out["id"],
        "payload": {"run_id": out["id"]}, "created_by": "u1", "priority": 200,
        "raise_on_active": False,
    }]
    assert background.tasks == []
    assert env.audits == [(
        "u1", "rfp_ingest.create", ENTITY, out["id"],
        {"source": "email", "email_id": EMAIL1, "file_count": 2, "job_id": "job1"},
    )]


def test_create_email_run_refusals(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {
        "ingested_emails": [_email_row(EMAIL1), _email_row(EMAIL2)],
        "ingested_email_attachments": [_attachment("a1", email_id=EMAIL2),
                                       _attachment("a2", email_id=EMAIL2)],
    }, rfp_ingest_max_files_per_run=1)
    user = _user()
    # A malformed id never reaches PostgREST; an unknown one is the same 404.
    _raises(404, rr.create_run, rr.RunCreateIn(source="email", email_id=NOT_A_UUID),
            BackgroundTasks(), user=user)
    _raises(404, rr.create_run, rr.RunCreateIn(source="email", email_id=MISSING),
            BackgroundTasks(), user=user)
    exc = _raises(409, rr.create_run, rr.RunCreateIn(source="email", email_id=EMAIL1),
                  BackgroundTasks(), user=user)
    assert "no attachments" in exc.detail
    _raises(413, rr.create_run, rr.RunCreateIn(source="email", email_id=EMAIL2),
            BackgroundTasks(), user=user)
    assert env.db.tables.get(ri.RUNS_TABLE, []) == []        # nothing was created
    assert env.queue.enqueued == [] and env.audits == []


def test_create_email_run_falls_back_to_background_tasks_when_the_queue_is_off(
    monkeypatch, tmp_path
):
    env = _env(monkeypatch, tmp_path, {
        "ingested_emails": [_email_row()],
        "ingested_email_attachments": [_attachment("a1")],
    }, llm_queue_enabled=False)
    monkeypatch.setattr(llm_queue, "enqueue", lambda *a, **k: pytest.fail("queue is off"))
    background = BackgroundTasks()
    out = rr.create_run(rr.RunCreateIn(source="email", email_id=EMAIL1), background, user=_user())
    assert background.tasks[0].func is ri.run_in_background
    assert background.tasks[0].args == (out["id"],)
    assert env.audits[0][4]["job_id"] is None


# ── Runs: start ──────────────────────────────────────────────────────────


def test_start_refusals_map_to_409(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [
            _run_row(RUN1),                                   # staging, no files
            _run_row(RUN2),                                   # staging, all rejected
            _run_row(RUN3, status=protocol.RUN_PENDING),      # already started
        ],
        ri.FILES_TABLE: [_file_row(FILE1, run_id=RUN2, status=protocol.STATUS_REJECTED)],
    })
    user = _user()
    for run_id, words in ((RUN1, "no files"), (RUN2, "rejected"), (RUN3, "already")):
        exc = _raises(409, rr.start_run, run_id, BackgroundTasks(), user=user)
        assert words in exc.detail
    _raises(404, rr.start_run, MISSING, BackgroundTasks(), user=user)
    assert env.queue.enqueued == [] and env.audits == []
    assert env.db.row(ri.RUNS_TABLE, RUN1)["status"] == protocol.RUN_STAGING


def test_start_dispatches_and_audits_the_file_counts(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row(file_count=1)],   # the plan is re-stamped with reality
        ri.FILES_TABLE: [
            _file_row(FILE1, status=protocol.STATUS_REJECTED,
                      reject_code=protocol.REJECT_NOT_PDF),
            _file_row(FILE2, seq=1),
            _file_row(FILE3, seq=2),
        ],
    })
    env.queue.poll = {"state": "queued", "position": 2}
    background = BackgroundTasks()
    out = rr.start_run(RUN1, background, user=_user())
    assert out["status"] == protocol.RUN_PENDING and out["file_count"] == 3
    assert [f["id"] for f in out["files"]] == [FILE1, FILE2, FILE3]
    assert out["queue"] == {"state": "queued", "position": 2}
    assert env.queue.enqueued == [{
        "job_type": llm_queue.JOB_RFP_INGEST, "target_id": RUN1, "payload": {"run_id": RUN1},
        "created_by": "u1", "priority": 200, "raise_on_active": False,
    }]
    assert background.tasks == []
    assert env.audits == [("u1", "rfp_ingest.start", ENTITY, RUN1,
                           {"file_count": 3, "rejected": 1})]


def test_start_uses_background_tasks_when_the_queue_is_off(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row()], ri.FILES_TABLE: [_file_row(FILE1)],
    }, llm_queue_enabled=False)
    monkeypatch.setattr(llm_queue, "enqueue", lambda *a, **k: pytest.fail("queue is off"))
    background = BackgroundTasks()
    out = rr.start_run(RUN1, background, user=_user())
    assert out["status"] == protocol.RUN_PENDING and out["queue"] is None
    assert background.tasks[0].func is ri.run_in_background
    assert background.tasks[0].args == (RUN1,)


# ── Reads ────────────────────────────────────────────────────────────────


def test_list_runs_pages_newest_first_and_refuses_an_unknown_status(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [
        _run_row(RUN1, status=protocol.RUN_DONE),
        _run_row(RUN2, status=protocol.RUN_PENDING,
                 created_at=(NOW + timedelta(minutes=1)).isoformat()),
    ]})
    user = _user()
    page = rr.list_runs(status_filter=None, limit=1, offset=0, _=user)
    assert page["total"] == 2 and page["limit"] == 1 and page["offset"] == 0
    assert [r["id"] for r in page["rows"]] == [RUN2]
    page = rr.list_runs(status_filter=None, limit=1, offset=1, _=user)
    assert [r["id"] for r in page["rows"]] == [RUN1]
    done = rr.list_runs(status_filter=protocol.RUN_DONE, limit=10, offset=0, _=user)
    assert [r["id"] for r in done["rows"]] == [RUN1] and done["total"] == 1
    exc = _raises(400, rr.list_runs, status_filter="bogus", limit=10, offset=0, _=user)
    assert exc.detail == "Unknown run status."


def test_get_run_detail_excludes_manifests_and_carries_queue_info(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row(status=protocol.RUN_RUNNING)],
        ri.FILES_TABLE: [
            _file_row(FILE2, seq=1, status=protocol.STATUS_RUNNING, phase="sandbox",
                      pages_done=12, manifest={"big": True}, hazards={"uri_links": 1}),
            _file_row(FILE1, status=protocol.STATUS_VERIFIED, manifest={"big": True}),
        ],
    })
    env.queue.poll = {"state": "running", "attempts": 1}
    out = rr.get_run(RUN1, _=_user())
    assert out["id"] == RUN1 and out["status"] == protocol.RUN_RUNNING
    assert [f["id"] for f in out["files"]] == [FILE1, FILE2]       # created_at order
    for f in out["files"]:
        assert "manifest" not in f, "the manifest jsonb must stay out of the run view"
    assert out["files"][1]["phase"] == "sandbox" and out["files"][1]["pages_done"] == 12
    assert out["files"][1]["hazards"] == {"uri_links": 1}
    assert out["queue"] == {"state": "running", "attempts": 1}
    _raises(404, rr.get_run, MISSING, _=_user())


def test_file_page_and_url_reads(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row(status=protocol.RUN_DONE)],
        ri.FILES_TABLE: [_file_row(
            FILE1, status=protocol.STATUS_VERIFIED_WITH_GAPS, filename="Spec Book (Rev 2).pdf",
            manifest={"verdict": {"status": "verified_with_gaps"}}, hazards={"uri_links": 2},
            derived_prefix=f"{RUN1}/{FILE1}", text_path=f"{RUN1}/{FILE1}/text.json",
            images_pdf_paths=[f"{RUN1}/{FILE1}/images-001.pdf"],
        )],
        ri.PAGES_TABLE: [
            {"id": PAGE1, "file_id": FILE1, "page_index": 1, "status": "failed",
             "code": "verify", "thumb_path": None, "full_path": None},
            {"id": PAGE0, "file_id": FILE1, "page_index": 0, "status": "ok",
             "thumb_path": f"{RUN1}/{FILE1}/thumb/0000.jpg",
             "full_path": f"{RUN1}/{FILE1}/full/0000.jpg"},
        ],
    })
    user = _user()
    frow = rr.get_file(FILE1, _=user)
    assert frow["manifest"] == {"verdict": {"status": "verified_with_gaps"}}
    assert frow["hazards"] == {"uri_links": 2}
    pages = rr.list_pages(FILE1, offset=0, limit=10, _=user)
    assert pages["total"] == 2 and [p["page_index"] for p in pages["rows"]] == [0, 1]
    assert pages["rows"][0]["thumb_url"] == (
        f"https://signed.test/rfp-derived/{RUN1}/{FILE1}/thumb/0000.jpg"
    )
    assert pages["rows"][1]["thumb_url"] is None               # a failed page has no image
    assert rr.list_pages(FILE1, offset=1, limit=1, _=user)["rows"][0]["id"] == PAGE1
    assert rr.list_pages(MISSING, offset=0, limit=10, _=user) == {
        "rows": [], "total": 0, "offset": 0, "limit": 10,
    }
    assert rr.page_urls(PAGE0, _=user) == {
        "full": f"https://signed.test/rfp-derived/{RUN1}/{FILE1}/full/0000.jpg"
    }
    assert rr.page_urls(PAGE1, _=user) == {"full": None}
    urls = rr.file_urls(FILE1, _=user)
    assert urls["images_pdf"] == [f"https://signed.test/rfp-derived/{RUN1}/{FILE1}/images-001.pdf"]
    assert urls["text"].endswith(f"/{RUN1}/{FILE1}/text.json")
    assert urls["manifest"].endswith(f"/{RUN1}/{FILE1}/manifest.json")
    assert {d for _, _, d in env.store.signed if d} == {
        "Spec_Book_Rev_2-images-001.pdf", "Spec_Book_Rev_2-text.json",
        "Spec_Book_Rev_2-manifest.json",
    }
    # Signed URLs are only ever minted in the derived bucket, never quarantine.
    assert {b for b, _, _ in env.store.signed} == {rs.DERIVED_BUCKET}
    _raises(404, rr.get_file, MISSING, _=user)
    _raises(404, rr.page_urls, MISSING, _=user)
    _raises(404, rr.file_urls, MISSING, _=user)


def test_file_detail_exposes_the_format_and_conversion_but_never_a_quarantine_url(
    monkeypatch, tmp_path
):
    """Spec 2.1: the file detail carries `source_format` and the manifest's
    `conversion` block; the quarantine object (the raw office file) and the
    converted PDF are never signed, and the run detail's file rows carry the
    format for the table chip."""
    conversion = {
        "engine": "gotenberg", "route": "/forms/libreoffice/convert", "source_format": "docx",
        "duration_ms": 812, "http_status": 200, "pdf_sha256": "a" * 64, "pdf_bytes": 4096,
        "reused": False, "converted_path": f"{RUN1}/{FILE1}/converted.pdf",
    }
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row(status=protocol.RUN_DONE)],
        ri.FILES_TABLE: [_file_row(
            FILE1, status=protocol.STATUS_VERIFIED, filename="Bid Form.docx",
            source_format="docx", quarantine_path=f"{RUN1}/{FILE1}/source.docx",
            converted_path=f"{RUN1}/{FILE1}/converted.pdf",
            manifest={"identity": {"source_format": "docx"}, "conversion": conversion,
                      "verdict": {"status": "verified"}},
            derived_prefix=f"{RUN1}/{FILE1}", text_path=f"{RUN1}/{FILE1}/text.json",
            images_pdf_paths=[f"{RUN1}/{FILE1}/images-001.pdf"],
        )],
    })
    user = _user()
    frow = rr.get_file(FILE1, _=user)
    assert frow["source_format"] == "docx"
    assert frow["converted_path"] == f"{RUN1}/{FILE1}/converted.pdf"
    assert frow["manifest"]["conversion"] == conversion
    assert "claim_token" not in frow
    files = rr.get_run(RUN1, _=user)["files"]
    assert files[0]["source_format"] == "docx" and files[0]["converted_path"]
    assert "manifest" not in files[0]
    urls = rr.file_urls(FILE1, _=user)
    assert set(urls) == {"images_pdf", "text", "manifest"}
    assert all("source.docx" not in (u or "") for u in [*urls["images_pdf"], urls["text"],
                                                       urls["manifest"]])
    assert all("converted.pdf" not in (u or "") for u in [*urls["images_pdf"], urls["text"],
                                                          urls["manifest"]])
    assert {b for b, _, _ in env.store.signed} == {rs.DERIVED_BUCKET}
    assert not any(path.endswith("source.docx") or path.endswith("converted.pdf")
                   for _, path, _ in env.store.signed)
    # The download stem drops a .pdf suffix only; an office name keeps its
    # extension in the safe stem.
    assert {d for _, _, d in env.store.signed if d} == {
        "Bid_Form.docx-images-001.pdf", "Bid_Form.docx-text.json", "Bid_Form.docx-manifest.json",
    }


def test_status_report_carries_the_office_files_entry(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path)
    monkeypatch.setattr(ri, "self_test", lambda: {"ok": True})
    monkeypatch.setattr(rr, "_status_cache", None)
    report = rr.sandbox_status(self_test=False, _=_user())
    assert report["office_files"]["enabled"] is True
    assert report["office_files"]["engine"] == "gotenberg"
    assert report["office_files"]["convert_timeout_seconds"] == (
        env.settings.rfp_ingest_office_convert_timeout_seconds
    )


@pytest.mark.parametrize("bad", NON_CANONICAL_IDS)
def test_malformed_ids_404_before_any_query(monkeypatch, tmp_path, bad):
    """An id PostgREST could not cast would 500 inside the uuid cast; every
    path id is checked first and answers the same 404 as a missing row (ids
    stay non-enumerable). Parsing with uuid.UUID is not the test: it accepts
    urn:uuid: / uuid: / urn: / braced / hyphen-less spellings that Postgres
    refuses, so only the canonical spelling passes."""
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(ri, "get_supabase", lambda: _Boom())
    user = _user()
    _raises(404, rr.get_run, bad, _=user)
    _raises(404, rr.get_file, bad, _=user)
    _raises(404, rr.list_pages, bad, offset=0, limit=10, _=user)
    _raises(404, rr.file_urls, bad, _=user)
    _raises(404, rr.page_urls, bad, _=user)
    _raises(404, rr.start_run, bad, BackgroundTasks(), user=user)
    _raises(404, rr.cancel_run, bad, user=user)
    _raises(404, rr.retry_run, bad, BackgroundTasks(), user=user)
    _raises(404, rr.delete_run, bad, user=user)
    _raises(404, _post_file, bad, _upload("a.pdf", PDF), user)
    _raises(404, rr.create_run, rr.RunCreateIn(source="email", email_id=bad),
            BackgroundTasks(), user=user)


def test_the_uuid_guard_passes_a_canonical_id_through_unchanged():
    """The guard narrows; it never rewrites. The id PostgREST (and the fake
    database) sees is the one the client sent, upper case included."""
    assert rr._uuid_or_404(RUN1, "nope") == RUN1
    assert rr._uuid_or_404(RUN1.upper(), "nope") == RUN1.upper()
    assert _raises(404, rr._uuid_or_404, None, rr._RUN_NOT_FOUND).detail == rr._RUN_NOT_FOUND


# ── Emails and status ────────────────────────────────────────────────────


def test_emails_query_is_sanitized_before_the_service_sees_it(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr(ri, "recent_emails", lambda *, q, limit: seen.append((q, limit)) or [])
    user = _user()
    raw = "a,b(c)%d_e*f\\g"
    rr.list_emails(q=raw, limit=5, _=user)
    assert seen[-1] == (_sanitize_query(raw), 5)
    assert not set(",()%_*\\") & set(seen[-1][0])           # no filter syntax, no wildcards
    rr.list_emails(q="%_*", limit=5, _=user)               # sanitizes to nothing: no filter
    assert seen[-1] == (None, 5)
    rr.list_emails(q=None, limit=25, _=user)
    assert seen[-1] == (None, 25)
    rr.list_emails(q="x" * 500, limit=5, _=user)
    assert len(seen[-1][0]) == 200                          # capped like the emails router


def test_emails_lists_inbound_with_attachment_counts(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, {
        "ingested_emails": [
            _email_row(EMAIL1),
            _email_row(EMAIL2, subject="Addendum 3",
                       message_at=(NOW + timedelta(hours=1)).isoformat()),
            _email_row("out", subject="RFP reply", direction="outbound"),
        ],
        "ingested_email_attachments": [
            _attachment("a1"), _attachment("a2", storage_path="emails/e1/a2.pdf"),
            _attachment("a3", email_id=EMAIL2, skipped_reason="too_large"),
        ],
    })
    user = _user()
    rows = rr.list_emails(q=None, limit=10, _=user)
    assert [r["id"] for r in rows] == [EMAIL2, EMAIL1]
    assert (rows[0]["attachment_count"], rows[0]["stored_count"]) == (1, 0)
    assert (rows[1]["attachment_count"], rows[1]["stored_count"]) == (2, 1)
    assert [r["id"] for r in rr.list_emails(q="ddend", limit=10, _=user)] == [EMAIL2]
    assert rr.list_emails(q="(%)", limit=10, _=user) == rows


def _status_env(monkeypatch, tmp_path, report_fn, **over):
    """The status route with an empty router cache and a stubbed service
    report, so the gate itself is what these tests measure. The stub takes
    `self_test_fresh`: the router is what decides whether the service is
    allowed to spawn a child for the verdict."""
    env = _env(monkeypatch, tmp_path)
    monkeypatch.setattr(rr, "_status_cache", None)
    monkeypatch.setattr(ri, "status_report", report_fn)
    for name, value in over.items():
        monkeypatch.setattr(rr, name, value)
    return env


def test_status_answers_the_service_report(monkeypatch, tmp_path):
    report = {"enabled": True, "self_test": {"ok": True, "detail": ri._MSG_SELF_TEST_OK}}
    _status_env(monkeypatch, tmp_path, lambda *, self_test_fresh=False: dict(report))
    assert rr.sandbox_status(self_test=True, _=_user()) == {**report, "cached": False}


def test_only_the_self_test_query_asks_the_service_for_a_fresh_child(monkeypatch, tmp_path):
    """A FRESH self-test is a real child, so the router must ask for one only
    on ?self_test=1. A plain read builds at most the cheap report (last
    verdict, no spawn), and inside the cache window builds nothing at all."""
    builds = []

    def report(*, self_test_fresh=False):
        builds.append(self_test_fresh)
        return {"enabled": True, "n": len(builds), "self_test_fresh": self_test_fresh}

    _status_env(monkeypatch, tmp_path, report)
    user = _user()
    first = rr.sandbox_status(self_test=False, _=user)
    # Nothing cached yet: the cheap report is built, and never with a spawn.
    assert first["cached"] is False and builds == [False]
    assert first["self_test_fresh"] is False
    second = rr.sandbox_status(self_test=False, _=user)
    assert second["cached"] is True and second["n"] == 1 and builds == [False]
    fresh = rr.sandbox_status(self_test=True, _=user)        # the opt-in, and only it
    assert fresh["cached"] is False and fresh["n"] == 2 and fresh["self_test_fresh"] is True
    assert builds == [False, True]
    assert rr.sandbox_status(self_test=False, _=user)["n"] == 2   # the new one is cached
    monkeypatch.setattr(rr, "_STATUS_CACHE_SECONDS", 0.0)         # the window expires
    assert rr.sandbox_status(self_test=False, _=user)["cached"] is False
    assert builds == [False, True, False]                    # a cold plain read stays cheap


def test_a_plain_status_read_spawns_nothing_through_the_real_report(monkeypatch, tmp_path):
    """The end-to-end version of the rule, over the service's real
    `status_report`: with a cold router cache a plain read must not reach
    `self_test()` at all, and it reports this worker's last verdict (the
    never-ran placeholder until one has run)."""
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(rr, "_status_cache", None)
    monkeypatch.setattr(
        ri, "self_test", lambda: pytest.fail("a plain read must not spawn the sandbox")
    )
    user = _user()
    cold = rr.sandbox_status(self_test=False, _=user)
    assert cold["cached"] is False and cold["self_test_fresh"] is False
    assert cold["self_test"] == {"ok": None, "detail": ri._MSG_SELF_TEST_NOT_RUN,
                                 "at": None, "elapsed_ms": None}
    assert cold["enabled"] is True and cold["scratch"]["free_bytes"] > 0
    assert cold["versions"]["sandbox_version"] == protocol.SANDBOX_VERSION

    # Once a verdict exists (the boot self-test, or ?self_test=1) the plain
    # read carries it, still without a child of its own.
    verdict = {"ok": True, "detail": ri._MSG_SELF_TEST_OK,
               "at": NOW.isoformat(), "elapsed_ms": 321}
    monkeypatch.setattr(ri, "_last_self_test", dict(verdict))
    monkeypatch.setattr(rr, "_STATUS_CACHE_SECONDS", 0.0)    # do not serve the cold one
    warm = rr.sandbox_status(self_test=False, _=user)
    assert warm["self_test"] == verdict and warm["self_test_fresh"] is False

    # ?self_test=1 is the one caller that does run it.
    spawns = []
    monkeypatch.setattr(ri, "self_test", lambda: spawns.append(1) or dict(verdict))
    fresh = rr.sandbox_status(self_test=True, _=user)
    assert fresh["self_test"] == verdict and fresh["self_test_fresh"] is True
    assert len(spawns) == 1


def test_concurrent_status_reads_build_one_report_at_a_time(monkeypatch, tmp_path):
    """~40 readers must not park the whole anyio threadpool on 40 child
    spawns: one report is built per worker at a time and everyone else takes
    the last one, or a 503 while this worker has never built one."""
    live = []
    blocking = threading.Event()
    entered = threading.Event()
    release = threading.Event()

    def report(*, self_test_fresh=False):
        live.append(1)
        if blocking.is_set():
            entered.set()
            assert release.wait(5)
        return {"enabled": True, "n": len(live)}

    _status_env(monkeypatch, tmp_path, report,
                _STATUS_WAIT_SECONDS=0.05, _STATUS_CACHE_SECONDS=0.0)
    user = _user()
    out: dict = {}

    def hold():
        out["report"] = rr.sandbox_status(self_test=True, _=user)

    # Nothing cached: a reader that waits out the busy builder is refused
    # with the router's own sentence rather than spawning a child of its own.
    blocking.set()
    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(5)
    assert _raises(503, rr.sandbox_status, self_test=False, _=user).detail == rr._MSG_STATUS_BUSY
    assert _raises(503, rr.sandbox_status, self_test=True, _=user).detail == rr._MSG_STATUS_BUSY
    assert len(live) == 1
    release.set()
    holder.join(5)
    assert out["report"]["cached"] is False and out["report"]["n"] == 1

    # With a report on hand the busy path answers it (stale, and marked so)
    # instead of queueing behind the builder.
    entered.clear()
    release.clear()
    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(5)
    waited = rr.sandbox_status(self_test=False, _=user)
    assert waited["cached"] is True and waited["n"] == 1
    assert len(live) == 2
    release.set()
    holder.join(5)
    assert out["report"]["n"] == 2


# ── Cancel / retry / delete ──────────────────────────────────────────────


def test_cancel_marks_the_run_canceled_then_cancels_the_queued_job(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row(status=protocol.RUN_RUNNING)],
        ri.FILES_TABLE: [_file_row(FILE1, status=protocol.STATUS_RUNNING, claim_token="tok",
                                   phase="sandbox", child_pid=4242)],
    })
    env.queue.active = {"id": "j1", "status": "queued"}
    out = rr.cancel_run(RUN1, user=_user())
    assert out["status"] == protocol.RUN_CANCELED and out["error"] == "The run was canceled."
    assert out["completed_at"]
    # The in-flight file is handed back for a retry, its live fields cleared.
    frow = out["files"][0]
    assert frow["status"] == protocol.STATUS_PENDING
    assert frow["reject_code"] == protocol.FAIL_INTERRUPTED
    assert frow["error"] == protocol.VERDICT_MESSAGES[protocol.FAIL_INTERRUPTED]
    assert frow["phase"] is None and frow["child_pid"] is None
    # claim_token is the write fence and is no longer sent to the browser; read it off the row.
    assert env.db.row(ri.FILES_TABLE, FILE1)["claim_token"] is None
    assert env.queue.canceled == ["j1"]
    assert len(env.bells) == 1 and env.bells[0]["type"] == "rfp_ingest.finished"
    assert env.audits == [("u1", "rfp_ingest.cancel", ENTITY, RUN1, {})]
    # canceled is sticky: a second cancel is a 409 and nothing changes.
    _raises(409, rr.cancel_run, RUN1, user=_user())
    assert len(env.bells) == 1 and env.queue.canceled == ["j1"]


@pytest.mark.parametrize(
    "status", [protocol.RUN_DONE, protocol.RUN_DONE_WITH_ERRORS, protocol.RUN_FAILED,
               protocol.RUN_EXPIRED],
)
def test_cancel_refuses_a_settled_run(monkeypatch, tmp_path, status):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row(status=status)]})
    env.queue.active = {"id": "j1", "status": "queued"}
    exc = _raises(409, rr.cancel_run, RUN1, user=_user())
    assert exc.detail == "The run can no longer be canceled."
    assert env.db.row(ri.RUNS_TABLE, RUN1)["status"] == status
    assert env.queue.canceled == [] and env.audits == [] and env.bells == []
    _raises(404, rr.cancel_run, MISSING, user=_user())


def test_cancel_a_staging_run_that_never_started(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row()]})
    out = rr.cancel_run(RUN1, user=_user())
    assert out["status"] == protocol.RUN_CANCELED
    assert env.queue.canceled == []                          # there was never a job


def test_retry_refuses_the_wrong_status_or_an_active_job(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [
        _run_row(RUN1, status=protocol.RUN_RUNNING),
        _run_row(RUN2, status=protocol.RUN_DONE),
        _run_row(RUN3, status=protocol.RUN_STAGING),
        _run_row(RUN4, status=protocol.RUN_FAILED),
    ]})
    user = _user()
    for run_id in (RUN1, RUN2, RUN3):
        exc = _raises(409, rr.retry_run, run_id, BackgroundTasks(), user=user)
        assert "Only failed, canceled or partially failed runs" in exc.detail
    env.queue.active = {"id": "j1", "status": "queued"}
    exc = _raises(409, rr.retry_run, RUN4, BackgroundTasks(), user=user)
    assert "already queued" in exc.detail
    _raises(404, rr.retry_run, MISSING, BackgroundTasks(), user=user)
    assert env.db.row(ri.RUNS_TABLE, RUN4)["status"] == protocol.RUN_FAILED
    assert env.queue.enqueued == [] and env.audits == []


@pytest.mark.parametrize(
    "status", [protocol.RUN_FAILED, protocol.RUN_DONE_WITH_ERRORS, protocol.RUN_CANCELED]
)
def test_retry_resets_the_retryable_files_reenqueues_and_audits(monkeypatch, tmp_path, status):
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row(status=status, error="x", completed_at=NOW.isoformat())],
        ri.FILES_TABLE: [
            _file_row(FILE1, status=protocol.STATUS_FAILED, reject_code=protocol.FAIL_STORAGE,
                      error=protocol.VERDICT_MESSAGES[protocol.FAIL_STORAGE],
                      derived_prefix=f"{RUN1}/{FILE1}", page_count=3),
            _file_row(FILE2, seq=1, status=protocol.STATUS_REJECTED,
                      reject_code=protocol.REJECT_NOT_PDF),
            _file_row(FILE3, seq=2, status=protocol.STATUS_VERIFIED, page_count=9),
        ],
        ri.PAGES_TABLE: [{"id": PAGE0, "file_id": FILE1, "page_index": 0, "status": "ok"}],
    })
    env.queue.poll = {"state": "queued", "position": 1}
    background = BackgroundTasks()
    out = rr.retry_run(RUN1, background, user=_user())
    assert out["status"] == protocol.RUN_PENDING and out["error"] is None
    assert out["completed_at"] is None
    by_id = {f["id"]: f for f in out["files"]}
    assert by_id[FILE1]["status"] == protocol.STATUS_PENDING
    assert by_id[FILE1]["reject_code"] is None and by_id[FILE1]["error"] is None
    assert by_id[FILE1]["derived_prefix"] is None and by_id[FILE1]["page_count"] is None
    assert by_id[FILE2]["status"] == protocol.STATUS_REJECTED   # never revisited
    assert by_id[FILE3]["status"] == protocol.STATUS_VERIFIED   # kept
    assert env.db.tables[ri.PAGES_TABLE] == []                   # the failed file's pages
    assert env.store.deleted == [(rs.DERIVED_BUCKET, f"{RUN1}/{FILE1}")]
    assert out["queue"] == {"state": "queued", "position": 1}
    assert env.queue.enqueued == [{
        "job_type": llm_queue.JOB_RFP_INGEST, "target_id": RUN1, "payload": {"run_id": RUN1},
        "created_by": "u1", "priority": 200, "raise_on_active": True,
    }]
    assert background.tasks == []
    assert env.audits == [("u1", "rfp_ingest.retry", ENTITY, RUN1, {})]


def test_retry_puts_the_run_back_when_the_enqueue_collides(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row(status=protocol.RUN_FAILED)],
        ri.FILES_TABLE: [_file_row(FILE1, status=protocol.STATUS_FAILED)],
    })

    def collide(jt, **kw):
        raise llm_queue.JobAlreadyActive({"id": "j9", "status": "queued"})

    monkeypatch.setattr(llm_queue, "enqueue", collide)
    exc = _raises(409, rr.retry_run, RUN1, BackgroundTasks(), user=_user())
    assert "already queued or running" in exc.detail
    assert env.db.row(ri.RUNS_TABLE, RUN1)["status"] == protocol.RUN_FAILED
    assert env.audits == []


def test_retry_uses_background_tasks_when_the_queue_is_off(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row(status=protocol.RUN_CANCELED)],
        ri.FILES_TABLE: [_file_row(FILE1)],
    }, llm_queue_enabled=False)
    monkeypatch.setattr(llm_queue, "enqueue", lambda *a, **k: pytest.fail("queue is off"))
    background = BackgroundTasks()
    out = rr.retry_run(RUN1, background, user=_user())
    assert out["status"] == protocol.RUN_PENDING
    assert background.tasks[0].func is ri.run_in_background
    assert background.tasks[0].args == (RUN1,)


@pytest.mark.parametrize("status", [protocol.RUN_PENDING, protocol.RUN_RUNNING])
def test_delete_refuses_a_live_run(monkeypatch, tmp_path, status):
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row(status=status)],
        ri.FILES_TABLE: [_file_row(FILE1)],
    })
    exc = _raises(409, rr.delete_run, RUN1, user=_user())
    assert "Cancel the run" in exc.detail
    assert env.db.row(ri.RUNS_TABLE, RUN1)["status"] == status
    assert env.store.deleted == [] and env.audits == []
    _raises(404, rr.delete_run, MISSING, user=_user())


@pytest.mark.parametrize(
    "status", [protocol.RUN_STAGING, protocol.RUN_DONE, protocol.RUN_DONE_WITH_ERRORS,
               protocol.RUN_FAILED, protocol.RUN_CANCELED, protocol.RUN_EXPIRED],
)
def test_delete_removes_the_row_then_both_prefixes(monkeypatch, tmp_path, status):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row(status=status),
                                                        _run_row(RUN2)]})
    assert rr.delete_run(RUN1, user=_user()) == {"id": RUN1, "deleted": True}
    assert [r["id"] for r in env.db.tables[ri.RUNS_TABLE]] == [RUN2]
    assert env.store.deleted == [(rs.DERIVED_BUCKET, RUN1), (rs.QUARANTINE_BUCKET, RUN1)]
    assert env.audits == [("u1", "rfp_ingest.delete", ENTITY, RUN1, {})]


# ── Error mapping ────────────────────────────────────────────────────────


def test_transient_service_faults_answer_503_with_the_app_authored_sentence(
    monkeypatch, tmp_path
):
    _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row(status=protocol.RUN_PENDING)]})

    def transient(run_id):
        raise ri.RfpIngestTransient("The sandbox scratch root could not be prepared.")

    monkeypatch.setattr(ri, "cancel_run", transient)
    exc = _raises(503, rr.cancel_run, RUN1, user=_user())
    assert exc.detail == "The sandbox scratch root could not be prepared."


def test_permanent_refusals_carry_their_own_http_status(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    for code in (400, 404, 409, 413):
        def refuse(*a, _code=code, **k):
            raise ri.RfpIngestPermanent("An app-authored sentence.", http_status=_code)

        monkeypatch.setattr(ri, "list_runs", refuse)
        exc = _raises(code, rr.list_runs, status_filter=None, limit=10, offset=0, _=_user())
        assert exc.detail == "An app-authored sentence."

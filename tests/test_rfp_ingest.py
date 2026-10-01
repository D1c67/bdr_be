"""Run orchestration for the RFP Ingestion Sandbox (app/services/rfp_ingest)
against an in-memory fake Supabase, with the sandbox runner faked. No DB, no
network, no child process: `rfp_sandbox_runner.run_sandbox` is replaced by a
scripted function that returns hand-built SandboxResult objects (and can call
the `should_abort` / `renew` callbacks it is handed, exactly as the real
monitor loop would, and delivers a complete result's pages through the
`page_sink` contract the real runner uses), the two RFP buckets are a
recorder, the queue's lease and enqueue helpers are stubs, and the Graph
`$value` stream is httpx.MockTransport behind graph_email._graph_client.

The fake mirrors PostgREST where it matters: a select returns the columns it
asked for and nothing else, `neq` drops NULL rows (`NULL <> 'x'` is NULL, not
true), and the partial unique index on (run_id, sha256) raises 23505.

What is pinned, in the spec's order (docs/RFP_INGESTION_SANDBOX.md section 5):

- `_mark` is a compare-and-swap: pending/running/failed/done only from
  pending|running, canceled sticky from staging|pending|running (nothing moves
  a run out of it), expired only from a terminal status; the bell fires on the
  ONE write that wins a terminal edge and never again; failed/canceled hand
  the run's running files back to pending as `interrupted`.
- Status derivation: done (including all-rejected), done_with_errors, failed,
  counters on the run row, and orphaned leftovers (only when no other job
  owns the run).
- The gap threshold is the runner's; here a complete result with failed pages
  becomes verified_with_gaps with placeholder rows, and the runner's
  too_many_failed_pages verdict is recorded with nothing uploaded.
- Duplicate content: 23505 at upload raises RfpIngestDuplicate with the
  winning row; the same index during a run records rejected/duplicate.
- Cancel between files and mid-child (child killed via should_abort, the
  in-flight file handed back, its outputs removed, the run sticky canceled),
  lease lost between files and mid-child (nothing written), shutdown mid-child
  (file left running, requeue_self called, no bell).
- Retry ordering (409 on an active job or a wrong status, files reset and
  their outputs removed, rejected files never revisited, the run put back
  when the enqueue collides), delete fencing (409 while active; rows then both
  prefixes).
- Email materialization: stored copy from project-files, Graph stream (200,
  404 -> not_stored, cap -> too_large), item attachments, a null graph id;
  upload materialization from quarantine and its verdict mapping.
- An images PDF builder failure is bounds_hit images_pdf only; a failed
  self-test refuses runs; the run page budget; a spawn failure aborts the
  run; the retention prune; the upload path (staging only, 413, sniff
  rejects recorded, upload failure recorded failed/storage); dispatch to the
  queue with the BackgroundTasks fallback; the queue adapters.
- The page delivery: batches of _UPLOAD_BATCH_PAGES in index order, bounded
  in bytes as well (a fat artifact flushes early, one bigger than the whole
  budget ships alone), no image bytes left on the result, a lease renewal and
  an abort re-check per batch,
  a cancel or a lost claim stopping the upload, a verdict after a delivery
  taking its uploads with it, and the pages rows replaced (never merged) by
  the attempt that holds the claim.
- The self-test gate's three states: a fresh failure refuses the run, a stale
  one is re-run first, and a busy uid slot is unknown (never cached, the run
  retried).
- Retention beyond expiry: the expired CAS before the deletes, the path
  columns cleared with the objects (one statement per chunk, never one per
  file), abandoned staging runs reclaimed onto `expired` so /retry refuses a
  run whose bytes are gone, and orphaned scratch trees swept, with an
  entry-count bomb left in place instead of walked.
- What the BackgroundTasks fallback owes a run nothing will retry, what
  DELETE owes a runner that may still be uploading, and that the internal
  write fence never leaves the process.
- Office files (spec 2.1), with Gotenberg behind httpx.MockTransport: an
  upload records its source_format and lands in quarantine under its own
  extension; the runner converts the raw bytes, sniffs the answer, uploads
  converted.pdf and runs the child over the PDF (the manifest's conversion
  block says so); 5xx and a timeout are failed/conversion_unavailable, 4xx
  and a non-PDF answer are rejected/conversion_rejected, an answer past the
  cap is rejected/too_large; a re-run reuses converted.pdf only when its
  digest matches the earlier manifest (and the re-run cleanup spares it);
  the email path takes an office attachment the same way; and with the
  setting off an office file is rejected/not_pdf as before.

Every string that reaches an error column is asserted to be an app-authored
sentence from protocol.VERDICT_MESSAGES (never a filename or exception text).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import BackgroundTasks

from app.core.config import Settings
from app.sandbox import protocol
from app.services import graph_email, llm_queue, notifications, storage
from app.services import rfp_ingest as ri
from app.services import rfp_ingest_storage as rs
from app.services import rfp_office_convert as office
from app.services import rfp_sandbox_runner as runner
from app.services.rfp_sandbox_runner import PageOutput, SandboxResult

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
PDF = runner.embedded_selftest_pdf()
USER = "u1"

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
        self._ignore_dup = False
        self._filters = []
        self._single = False
        self._orders = []
        self._limit = None
        self._range = None
        self._count = None
        self._columns = None

    def select(self, sel="*", count=None, **k):
        self._op = "select"
        self._count = count
        self._columns = None if sel == "*" else [c.strip() for c in sel.split(",")]
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def upsert(self, payload, on_conflict=None, ignore_duplicates=False, **k):
        self._op, self._payload = "upsert", payload
        self._on_conflict, self._ignore_dup = on_conflict, ignore_duplicates
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

    @staticmethod
    def _or_matches(row, expr):
        for clause in expr.split(","):
            col, op, val = clause.split(".", 2)
            if op == "ilike":
                needle = val.strip("%").lower()
                if needle in str(row.get(col) or "").lower():
                    return True
        return False

    def _matches(self, row):
        for op, col, val in self._filters:
            have = row.get(col)
            if op == "eq" and have != val:
                return False
            if op == "neq" and (have is None or have == val):
                # PostgREST evaluates `col <> 'x'` in SQL, where NULL <> 'x'
                # is NULL, so a row whose column is NULL is NOT returned.
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
        row.setdefault("id", self.db.next_id())
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
            # PostgREST returns the selected columns and nothing else.
            hits = [
                dict(r) if self._columns is None
                else {c: r.get(c) for c in self._columns}
                for r in hits
            ]
            count = total if self._count else None
            if self._single:
                return SimpleNamespace(data=(hits[0] if hits else None), count=count)
            return SimpleNamespace(data=hits, count=count)
        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            return SimpleNamespace(data=[self._insert_one(p, rows) for p in payloads])
        if self._op == "upsert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            keys = [k.strip() for k in (self._on_conflict or "id").split(",")]
            out = []
            for payload in payloads:
                existing = next(
                    (r for r in rows if all(r.get(k) == payload.get(k) for k in keys)), None
                )
                if existing is not None:
                    if not self._ignore_dup:
                        existing.update(payload)
                    out.append(dict(existing))
                    continue
                out.append(self._insert_one(payload, rows))
            return SimpleNamespace(data=out)
        if self._op == "update":
            matched = [r for r in rows if self._matches(r)]
            for r in matched:
                probe = {**r, **self._payload}
                self._check_unique(probe, [x for x in rows if x is not r])
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

    def next_id(self):
        return uuid.uuid4().hex

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


# ── Fake storage / queue / runner ────────────────────────────────────────


class _FakeRs:
    """Recorder for the rfp_ingest_storage surface the service calls."""

    def __init__(self):
        self.uploads = []       # upload_bytes: (bucket, path, data, ct)
        self.files = []         # upload_file: (bucket, path, data, ct)
        self.many = []          # upload_many: (bucket, [(path, data, ct)])
        self.deleted = []       # delete_prefix: (bucket, prefix)
        self.kept = []          # delete_prefix: the `keep` names of every call
        self.signed = []
        self.download_bytes = PDF
        self.download_exc = None
        self.upload_exc = None
        # download_to_file answers per path when set (the converted.pdf
        # reuse tests); a value of an exception instance is raised.
        self.objects: dict[str, object] = {}

    def upload_bytes(self, bucket, path, data, content_type):
        if self.upload_exc is not None:
            raise self.upload_exc
        self.uploads.append((bucket, path, bytes(data), content_type))

    def upload_file(self, bucket, path, file_path, content_type):
        if self.upload_exc is not None:
            raise self.upload_exc
        self.files.append((bucket, path, Path(file_path).read_bytes(), content_type))

    def upload_many(self, bucket, items, max_workers=4):
        if self.upload_exc is not None:
            raise self.upload_exc
        self.many.append((bucket, [(p, bytes(d), c) for p, d, c in items]))

    def delete_prefix(self, bucket, prefix, *, keep=()):
        self.deleted.append((bucket, prefix))
        self.kept.append(tuple(keep))
        return 0

    def signed_url(self, bucket, path, *, download=None):
        self.signed.append((bucket, path, download))
        return f"https://signed.test/{bucket}/{path}"

    def download_to_file(self, bucket, path, dest, *, max_bytes):
        if self.download_exc is not None:
            raise self.download_exc
        if path in self.objects:
            data = self.objects[path]
            if isinstance(data, BaseException):
                raise data
        else:
            data = self.content_for(path)
        if Path(dest).exists():
            raise FileExistsError(str(dest))
        Path(dest).write_bytes(data)
        return len(data)

    def content_for(self, path: str) -> bytes:
        """Distinct bytes per object (a trailing comment), so two files of one
        run do not collide on the sha256 index unless a test wants them to."""
        if self.download_bytes is not PDF:
            return self.download_bytes
        return PDF + b"%" + path.encode() + b"\n"

    def all_paths(self):
        paths = [p for _, p, *_ in self.uploads] + [p for _, p, *_ in self.files]
        for _, items in self.many:
            paths.extend(p for p, _, _ in items)
        return paths


class _Env:
    def __init__(self, db, settings, store):
        self.db = db
        self.settings = settings
        self.store = store
        self.bells = []
        self.queue = SimpleNamespace(enqueued=[], canceled=[], requeued=0, active=None)
        self.sandbox_calls = []


def _settings(tmp_path, **over):
    over.setdefault("llm_queue_enabled", True)
    return Settings(
        _env_file=None,
        supabase_url="https://sb.test",
        supabase_service_role_key="svc",
        rfp_ingest_scratch_dir=str(tmp_path / "scratch"),
        **over,
    )


def _env(monkeypatch, tmp_path, tables=None, **over) -> _Env:
    db = FakeDB(tables)
    s = _settings(tmp_path, **over)
    store = _FakeRs()
    env = _Env(db, s, store)
    monkeypatch.setattr(ri, "get_supabase", lambda: db)
    monkeypatch.setattr(ri, "get_settings", lambda: s)
    monkeypatch.setattr(ri, "_disk_free", lambda path: 10**12)
    monkeypatch.setattr(ri, "_last_self_test", None)
    monkeypatch.setattr(ri, "_active_runs", 0)
    ri.SHUTTING_DOWN.clear()
    for name in (
        "upload_bytes", "upload_file", "upload_many", "delete_prefix", "signed_url",
        "download_to_file",
    ):
        monkeypatch.setattr(rs, name, getattr(store, name))
    monkeypatch.setattr(
        notifications, "notify_user",
        lambda user_id, project_id, type_, message, **kw: env.bells.append(
            {"user_id": user_id, "type": type_, "message": message, **kw}
        ),
    )
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: True)
    monkeypatch.setattr(
        llm_queue, "requeue_self",
        lambda job=None: setattr(env.queue, "requeued", env.queue.requeued + 1) or True,
    )
    monkeypatch.setattr(llm_queue, "active_job", lambda jt, t: env.queue.active)
    monkeypatch.setattr(
        llm_queue, "enqueue",
        lambda jt, **kw: env.queue.enqueued.append({"job_type": jt, **kw}) or {"id": "job1"},
    )
    monkeypatch.setattr(llm_queue, "cancel", lambda job_id: env.queue.canceled.append(job_id))
    monkeypatch.setattr(llm_queue, "poll_info", lambda jt, t: None)
    monkeypatch.setattr(
        runner, "run_sandbox",
        lambda pdf_path, **kw: pytest.fail("run_sandbox must be scripted by the test"),
    )
    return env


def _script_sandbox(monkeypatch, env: _Env, fn):
    """Install `fn(call_index, pdf_path, kwargs) -> SandboxResult` as the
    runner, wrapped in the real `page_sink` contract: when the scripted result
    is complete, every ok page is delivered to the sink in index order with
    its two JPEGs, and the bytes are dropped from the PageOutput first, so the
    result the service gets back carries no images at all (spec section 2)."""

    def run_sandbox(pdf_path, **kw):
        env.sandbox_calls.append({
            "pdf_path": Path(pdf_path), "pdf_bytes": Path(pdf_path).read_bytes(), **kw,
        })
        result = fn(len(env.sandbox_calls) - 1, Path(pdf_path), kw)
        sink = kw.get("page_sink")
        if sink is not None and result.status == runner.STATUS_COMPLETE:
            ok = sorted(
                (p for p in result.pages if p.status == protocol.PAGE_OK),
                key=lambda p: p.index,
            )
            for page in ok:
                thumb, full = page.thumb, page.full
                page.thumb, page.full = None, None
                sink(page, thumb, full)
        return result

    monkeypatch.setattr(runner, "run_sandbox", run_sandbox)


def _jpeg(seed: int) -> bytes:
    return b"\xff\xd8" + bytes([seed % 256]) * 40 + b"\xff\xd9"


def _meta(data: bytes, w: int, h: int) -> dict:
    return {"w": w, "h": h, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _page_ok(index: int, text: str = "Section 26 05 00 electrical") -> PageOutput:
    thumb, full = _jpeg(index), _jpeg(index + 100)
    return PageOutput(
        index=index, status=protocol.PAGE_OK, code=None, detail=None,
        width_pt=612.0, height_pt=792.0, rotation=0, tier="full_small",
        thumb=thumb, full=full, thumb_meta=_meta(thumb, 120, 155), full_meta=_meta(full, 240, 310),
        text=text, text_chars=len(text), text_truncated=False,
        text_hazards=dict.fromkeys(protocol.TEXT_HAZARD_KEYS, 0),
        hazards=dict.fromkeys(protocol.PAGE_HAZARD_KEYS, 0), render_ms=7,
    )


def _page_sized(index: int, nbytes: int) -> PageOutput:
    """An ok page whose two artifacts are `nbytes` each: what the sink's byte
    budget is about is how fat an artifact the protocol tolerates, not how
    many pages a file has."""
    page = _page_ok(index)
    thumb = b"\xff\xd8" + bytes([index % 256]) * (nbytes - 4) + b"\xff\xd9"
    full = b"\xff\xd8" + bytes([(index + 100) % 256]) * (nbytes - 4) + b"\xff\xd9"
    page.thumb, page.full = thumb, full
    page.thumb_meta, page.full_meta = _meta(thumb, 120, 155), _meta(full, 240, 310)
    return page


def _page_failed(index: int, code: str = protocol.PAGE_FAIL_RENDER) -> PageOutput:
    return PageOutput(
        index=index, status=protocol.PAGE_FAILED, code=code,
        detail=runner.PAGE_DETAIL_MESSAGES[code], width_pt=None, height_pt=None,
        rotation=None, tier=None, thumb=None, full=None, thumb_meta=None, full_meta=None,
        text=None, text_chars=0, text_truncated=False,
        text_hazards=dict.fromkeys(protocol.TEXT_HAZARD_KEYS, 0),
        hazards=dict.fromkeys(protocol.PAGE_HAZARD_KEYS, 0), render_ms=None,
    )


def _result(pages, status=runner.STATUS_COMPLETE, code=None, bounds=(), **over) -> SandboxResult:
    hazards = dict.fromkeys(protocol.DOC_HAZARD_KEYS + protocol.PAGE_HAZARD_KEYS, 0)
    fields = dict(
        status=status, code=code,
        detail=protocol.VERDICT_MESSAGES.get(code) if code else None,
        start={"sandbox_version": protocol.SANDBOX_VERSION,
               "protocol_version": protocol.PROTOCOL_VERSION,
               "limits_applied": {"memory": False, "cpu": True}},
        ready={"versions": {"python": "3.12.13", "pypdfium2": "5.13.0"},
               "pdfium_flags": None, "platform": "test"},
        document={"page_count": len(pages), "pdf_version": "1.4", "owner_restricted": False,
                  "security_handler_revision": None, "form_type": "none",
                  "metadata": {"title": "t"}, "hazards": {}},
        end={"pages_ok": 0, "pages_failed": 0, "elapsed_ms": 10, "peak_rss_kb": 1000,
             "output_bytes": 100, "aborted": None},
        pages=list(pages), hazards=hazards, bounds_hit=list(bounds), restarts=0,
        elapsed_ms=42, uid=None, gid=None, slot=None, stderr_tail="", spawns=2,
        peak_rss_kb=1000,
    )
    fields.update(over)
    return SandboxResult(**fields)


def _complete(n_pages: int, failed=()):
    pages = [_page_failed(i) if i in failed else _page_ok(i) for i in range(n_pages)]
    return _result(pages)


# ── Seeding helpers ──────────────────────────────────────────────────────


def _run_row(run_id="r1", status=protocol.RUN_PENDING, source_kind="upload", **over):
    row = {
        "id": run_id, "source_kind": source_kind, "email_id": None, "status": status,
        "error": None, "file_count": 0, "files_verified": 0, "files_gapped": 0,
        "files_rejected": 0, "files_failed": 0, "pages_total": 0, "limits": None,
        "limits_hash": None, "sandbox_version": None, "protocol_version": None,
        "created_by": USER, "started_at": None, "completed_at": None,
        "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
    }
    row.update(over)
    return row


def _file_row(file_id, run_id="r1", status=protocol.STATUS_PENDING, seq=0, **over):
    row = {
        "id": file_id, "run_id": run_id, "source_attachment_id": None,
        "filename": f"{file_id}.pdf", "declared_mime": "application/pdf",
        "size_bytes": len(PDF), "sha256": None, "quarantine_path": f"{run_id}/{file_id}/source.pdf",
        "status": status, "reject_code": None, "error": None, "claim_token": None,
        "phase": None, "pages_done": 0, "last_progress_at": None, "child_pid": None,
        "page_count": None, "pages_ok": None, "pages_failed": None, "hazards": None,
        "manifest": None, "derived_prefix": None, "images_pdf_paths": None, "text_path": None,
        "restarts": 0, "elapsed_ms": None, "sandbox_version": None, "protocol_version": None,
        "limits_hash": None, "started_at": None, "finished_at": None,
        "source_format": "pdf", "converted_path": None,
        "created_at": (NOW + timedelta(seconds=seq)).isoformat(),
        "updated_at": NOW.isoformat(),
    }
    row.update(over)
    return row


def _upload_run(monkeypatch, tmp_path, files=("f1", "f2"), **over) -> _Env:
    tables = {
        ri.RUNS_TABLE: [_run_row(file_count=len(files))],
        ri.FILES_TABLE: [_file_row(fid, seq=i) for i, fid in enumerate(files)],
    }
    return _env(monkeypatch, tmp_path, tables, **over)


def _assert_verdict_text(row: dict) -> None:
    """Error columns hold an app-authored sentence, never a name or an exc."""
    if row.get("error") is not None:
        assert row["error"] in protocol.VERDICT_MESSAGES.values(), row["error"]


# ── _mark: CAS rules and the once-only bell ──────────────────────────────


def test_mark_active_statuses_apply_only_from_pending_or_running(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [
        _run_row("staging", status=protocol.RUN_STAGING),
        _run_row("done", status=protocol.RUN_DONE),
        _run_row("pending", status=protocol.RUN_PENDING),
    ]})
    # staging -> pending is start_run's own CAS; _mark refuses every active
    # transition from staging, and nothing moves a finished run.
    for status in (protocol.RUN_PENDING, protocol.RUN_RUNNING, protocol.RUN_FAILED,
                   protocol.RUN_DONE):
        assert ri._mark("staging", status=status) is None
        assert ri._mark("done", status=status) is None
    assert env.db.row(ri.RUNS_TABLE, "staging")["status"] == protocol.RUN_STAGING
    assert env.db.row(ri.RUNS_TABLE, "done")["status"] == protocol.RUN_DONE
    row = ri._mark("pending", status=protocol.RUN_RUNNING)
    assert row["status"] == protocol.RUN_RUNNING and row["started_at"]
    with pytest.raises(ValueError):
        ri._mark("pending", status=protocol.RUN_STAGING)
    assert env.bells == []


def test_mark_canceled_is_sticky(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row(status=protocol.RUN_RUNNING)]})
    assert ri._mark("r1", status=protocol.RUN_CANCELED)["status"] == protocol.RUN_CANCELED
    assert len(env.bells) == 1
    for status in (protocol.RUN_PENDING, protocol.RUN_RUNNING, protocol.RUN_FAILED,
                   protocol.RUN_DONE, protocol.RUN_DONE_WITH_ERRORS, protocol.RUN_CANCELED):
        assert ri._mark("r1", status=status) is None
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_CANCELED
    assert len(env.bells) == 1  # the second canceled write lost, so no second bell
    # canceled also applies from staging (a run that never started).
    env.db.tables[ri.RUNS_TABLE].append(_run_row("s", status=protocol.RUN_STAGING))
    assert ri._mark("s", status=protocol.RUN_CANCELED)["status"] == protocol.RUN_CANCELED


def test_mark_expired_only_from_terminal_and_sends_no_bell(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [
        _run_row("running", status=protocol.RUN_RUNNING),
        _run_row("done", status=protocol.RUN_DONE),
        _run_row("canceled", status=protocol.RUN_CANCELED),
    ]})
    assert ri._mark("running", status=protocol.RUN_EXPIRED) is None
    assert ri._mark("done", status=protocol.RUN_EXPIRED)["status"] == protocol.RUN_EXPIRED
    assert ri._mark("canceled", status=protocol.RUN_EXPIRED)["status"] == protocol.RUN_EXPIRED
    assert ri._mark("done", status=protocol.RUN_EXPIRED) is None  # not from expired itself
    assert env.bells == []


@pytest.mark.parametrize(
    "status", [protocol.RUN_DONE, protocol.RUN_DONE_WITH_ERRORS, protocol.RUN_FAILED,
               protocol.RUN_CANCELED],
)
def test_terminal_edge_sends_the_bell_exactly_once(monkeypatch, tmp_path, status):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row(status=protocol.RUN_RUNNING)]})
    row = ri._mark("r1", status=status, error="Every file in the run failed; retry the run."
                   if status == protocol.RUN_FAILED else None,
                   files_verified=2, files_gapped=1, files_rejected=0, files_failed=1)
    assert row["status"] == status and row["completed_at"]
    assert ri._mark("r1", status=status) is None
    assert len(env.bells) == 1
    bell = env.bells[0]
    assert bell["user_id"] == USER and bell["type"] == "rfp_ingest.finished"
    assert bell["mirror_email"] is False
    assert bell["metadata"] == {
        "run_id": "r1", "status": status, "files_verified": 2, "files_gapped": 1,
        "files_rejected": 0, "files_failed": 1,
    }
    if status == protocol.RUN_FAILED:
        assert bell["message"] == (
            "RFP ingestion run failed: Every file in the run failed; retry the run."
        )
    elif status == protocol.RUN_CANCELED:
        assert bell["message"].startswith("RFP ingestion run canceled")
    else:
        assert bell["message"] == (
            "RFP ingestion run finished: 2 verified, 1 with gaps, 0 rejected, 1 failed"
        )


def test_bell_failure_never_fails_the_mark(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row(status=protocol.RUN_RUNNING)]})

    def boom(*a, **k):
        raise RuntimeError("notifications down")

    monkeypatch.setattr(notifications, "notify_user", boom)
    assert ri._mark("r1", status=protocol.RUN_DONE)["status"] == protocol.RUN_DONE
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_DONE


def test_no_bell_without_a_creator(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [
        _run_row(status=protocol.RUN_RUNNING, created_by=None)
    ]})
    assert ri._mark("r1", status=protocol.RUN_DONE)
    assert env.bells == []


@pytest.mark.parametrize("status", [protocol.RUN_FAILED, protocol.RUN_CANCELED])
def test_failed_and_canceled_hand_running_files_back_as_interrupted(monkeypatch, tmp_path, status):
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row(status=protocol.RUN_RUNNING)],
        ri.FILES_TABLE: [
            _file_row("running", status=protocol.STATUS_RUNNING, claim_token="tok",
                      phase="sandbox", child_pid=42, pages_done=3),
            _file_row("done", status=protocol.STATUS_VERIFIED, seq=1),
            _file_row("waiting", status=protocol.STATUS_PENDING, seq=2),
        ],
    })
    assert ri._mark("r1", status=status, error="x" if status == protocol.RUN_FAILED else None)
    reset = env.db.row(ri.FILES_TABLE, "running")
    assert reset["status"] == protocol.STATUS_PENDING
    assert reset["reject_code"] == protocol.FAIL_INTERRUPTED
    assert reset["claim_token"] is None and reset["phase"] is None
    assert reset["child_pid"] is None and reset["pages_done"] == 0
    _assert_verdict_text(reset)
    assert env.db.row(ri.FILES_TABLE, "done")["status"] == protocol.STATUS_VERIFIED
    assert env.db.row(ri.FILES_TABLE, "waiting")["reject_code"] is None


def test_done_does_not_touch_running_files(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row(status=protocol.RUN_RUNNING)],
        ri.FILES_TABLE: [_file_row("f", status=protocol.STATUS_RUNNING, claim_token="tok")],
    })
    ri._mark("r1", status=protocol.RUN_DONE)
    assert env.db.row(ri.FILES_TABLE, "f")["status"] == protocol.STATUS_RUNNING


# ── Queue adapters ───────────────────────────────────────────────────────


def test_mark_from_queue_maps_pending_and_failed_through_the_cas(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [
        _run_row("active", status=protocol.RUN_RUNNING),
        _run_row("canceled", status=protocol.RUN_CANCELED),
    ]})
    ri.mark_from_queue("active", "pending", None)
    assert env.db.row(ri.RUNS_TABLE, "active")["status"] == protocol.RUN_PENDING
    ri.mark_from_queue("active", "failed", "The run failed (failed after 6 attempts)")
    row = env.db.row(ri.RUNS_TABLE, "active")
    assert row["status"] == protocol.RUN_FAILED
    assert row["error"] == "The run failed (failed after 6 attempts)"
    assert len(env.bells) == 1
    # Sticky canceled ignores the queue's marks entirely.
    ri.mark_from_queue("canceled", "failed", "late")
    ri.mark_from_queue("canceled", "pending", None)
    assert env.db.row(ri.RUNS_TABLE, "canceled")["status"] == protocol.RUN_CANCELED
    ri.mark_from_queue("active", "succeeded", None)  # unknown mark: ignored, no raise
    assert len(env.bells) == 1


def test_current_status_reports_every_terminal_run_as_done(monkeypatch, tmp_path):
    rows = [_run_row(status, status=status) for status in sorted(protocol.RUN_STATUSES)]
    _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: rows})
    for status in protocol.RUN_TERMINAL_STATUSES:
        assert ri.current_status(status) == "done"
    for status in protocol.RUN_ACTIVE_STATUSES:
        assert ri.current_status(status) == status
    assert ri.current_status("missing") is None


def test_exception_classes_declare_their_queue_kind():
    from app.services import llm_errors

    assert llm_errors.classify(ri.RfpIngestTransient("x")) == llm_errors.KIND_INFRASTRUCTURE
    assert llm_errors.classify(ri.RfpIngestPermanent("x")) == llm_errors.KIND_BAD_INPUT
    assert ri.RfpIngestPermanent("x", http_status=413).http_status == 413
    dup = ri.RfpIngestDuplicate("f9")
    assert isinstance(dup, ri.RfpIngestPermanent) and dup.existing_file_id == "f9"
    assert str(dup) == protocol.VERDICT_MESSAGES[protocol.REJECT_DUPLICATE]


# ── Upload runs: create, add files, start, dispatch ──────────────────────


def test_create_upload_run_is_staging(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    run = ri.create_upload_run(created_by=USER)
    assert run["status"] == protocol.RUN_STAGING
    assert run["source_kind"] == "upload" and run["created_by"] == USER


def test_add_upload_file_inserts_the_row_before_the_object(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row(status=protocol.RUN_STAGING)]})
    order = []
    original = env.store.upload_bytes

    def upload(bucket, path, data, ct):
        order.append(("upload", [r["id"] for r in env.db.tables[ri.FILES_TABLE]]))
        original(bucket, path, data, ct)

    monkeypatch.setattr(rs, "upload_bytes", upload)
    row = ri.add_upload_file("r1", filename="Spec\x00 Book.pdf", declared_mime="x/y", data=PDF)
    assert row["status"] == protocol.STATUS_PENDING
    assert row["filename"] == "Spec Book.pdf"        # display-sanitized, never verbatim
    assert row["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert row["size_bytes"] == len(PDF)
    assert row["quarantine_path"] == f"r1/{row['id']}/source.pdf"
    assert order == [("upload", [row["id"]])]         # the row existed when the upload ran
    assert env.store.uploads[0][:2] == (rs.QUARANTINE_BUCKET, row["quarantine_path"])
    assert env.store.uploads[0][3] == "application/pdf"
    assert row["manifest"]["sniff"]["verdict"] is None
    assert row["manifest"]["sniff"]["byte_markers"]["/OpenAction"] == 0
    assert env.db.row(ri.RUNS_TABLE, "r1")["file_count"] == 1


def test_add_upload_file_records_sniff_rejects_without_an_object(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row(status=protocol.RUN_STAGING)]})
    row = ri.add_upload_file("r1", filename="evil.pdf", declared_mime=None, data=b"MZ not a pdf")
    assert row["status"] == protocol.STATUS_REJECTED
    assert row["reject_code"] == protocol.REJECT_NOT_PDF
    assert row["error"] == protocol.VERDICT_MESSAGES[protocol.REJECT_NOT_PDF]
    assert row["quarantine_path"] is None and row["finished_at"]
    assert env.store.uploads == []
    empty = ri.add_upload_file("r1", filename="empty.pdf", declared_mime=None, data=b"")
    assert empty["reject_code"] == protocol.REJECT_EMPTY


def test_add_upload_file_over_the_byte_cap_is_rejected_too_large(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row(status=protocol.RUN_STAGING)]},
               rfp_ingest_max_file_bytes=64)
    row = ri.add_upload_file("r1", filename="big.pdf", declared_mime=None, data=PDF)
    assert row["status"] == protocol.STATUS_REJECTED
    assert row["reject_code"] == protocol.REJECT_TOO_LARGE
    assert env.store.uploads == []


def test_add_upload_file_duplicate_raises_with_the_winning_row(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row(status=protocol.RUN_STAGING)]})
    first = ri.add_upload_file("r1", filename="a.pdf", declared_mime=None, data=PDF)
    with pytest.raises(ri.RfpIngestDuplicate) as exc:
        ri.add_upload_file("r1", filename="b.pdf", declared_mime=None, data=PDF)
    assert exc.value.existing_file_id == first["id"]
    assert exc.value.http_status == 409
    assert len(env.db.tables[ri.FILES_TABLE]) == 1
    assert len(env.store.uploads) == 1                # no second object


def test_add_upload_file_refuses_non_staging_and_the_file_cap(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row("p", status=protocol.RUN_PENDING),
                        _run_row("s", status=protocol.RUN_STAGING)],
        ri.FILES_TABLE: [_file_row("f1", run_id="s"), _file_row("f2", run_id="s", seq=1)],
    }, rfp_ingest_max_files_per_run=2)
    with pytest.raises(ri.RfpIngestPermanent) as exc:
        ri.add_upload_file("p", filename="a.pdf", declared_mime=None, data=PDF)
    assert exc.value.http_status == 409
    with pytest.raises(ri.RfpIngestPermanent) as exc:
        ri.add_upload_file("s", filename="a.pdf", declared_mime=None, data=PDF)
    assert exc.value.http_status == 413
    with pytest.raises(ri.RfpIngestPermanent) as exc:
        ri.add_upload_file("missing", filename="a.pdf", declared_mime=None, data=PDF)
    assert exc.value.http_status == 404


def test_add_upload_file_upload_failure_is_failed_storage_not_an_orphan(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row(status=protocol.RUN_STAGING)]})
    env.store.upload_exc = rs.RfpStorageError("Storage returned HTTP 500.")
    row = ri.add_upload_file("r1", filename="a.pdf", declared_mime=None, data=PDF)
    assert row["status"] == protocol.STATUS_FAILED
    assert row["reject_code"] == protocol.FAIL_STORAGE
    assert row["error"] == protocol.VERDICT_MESSAGES[protocol.FAIL_STORAGE]
    assert row["quarantine_path"] is None


def test_start_run_rules(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [
            _run_row("empty", status=protocol.RUN_STAGING),
            _run_row("rejected", status=protocol.RUN_STAGING),
            _run_row("ok", status=protocol.RUN_STAGING),
            _run_row("started", status=protocol.RUN_PENDING),
        ],
        ri.FILES_TABLE: [
            _file_row("x", run_id="rejected", status=protocol.STATUS_REJECTED),
            _file_row("y", run_id="ok", status=protocol.STATUS_REJECTED),
            _file_row("z", run_id="ok", status=protocol.STATUS_PENDING, seq=1),
        ],
    })
    for run_id in ("empty", "rejected", "started"):
        with pytest.raises(ri.RfpIngestPermanent) as exc:
            ri.start_run(run_id)
        assert exc.value.http_status == 409
    row = ri.start_run("ok")
    assert row["status"] == protocol.RUN_PENDING and row["file_count"] == 2
    assert env.db.row(ri.RUNS_TABLE, "ok")["status"] == protocol.RUN_PENDING


def test_dispatch_enqueues_one_job_per_run_at_the_sandbox_priority(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path)
    background = BackgroundTasks()
    job = ri.dispatch("r1", created_by=USER, background=background)
    assert job == {"id": "job1"}
    assert env.queue.enqueued == [{
        "job_type": llm_queue.JOB_RFP_INGEST, "target_id": "r1", "payload": {"run_id": "r1"},
        "created_by": USER, "priority": 200, "raise_on_active": False,
    }]
    assert background.tasks == []


def test_dispatch_falls_back_to_background_tasks(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, llm_queue_enabled=False)
    monkeypatch.setattr(llm_queue, "enqueue", lambda *a, **k: pytest.fail("queue is off"))
    background = BackgroundTasks()
    assert ri.dispatch("r1", created_by=USER, background=background) is None
    assert background.tasks[0].func is ri.run_in_background
    assert background.tasks[0].args == ("r1",)
    # A queue outage degrades the same way.
    env = _env(monkeypatch, tmp_path)

    def down(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(llm_queue, "enqueue", down)
    background = BackgroundTasks()
    assert ri.dispatch("r1", created_by=USER, background=background) is None
    assert background.tasks[0].func is ri.run_in_background
    assert env.queue.enqueued == []


def test_run_in_background_owns_its_terminal_mark(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row(status=protocol.RUN_RUNNING)]})

    def boom(run_id):
        raise ri.RfpIngestPermanent("The run has no files.")

    monkeypatch.setattr(ri, "execute", boom)
    ri.run_in_background("r1")
    row = env.db.row(ri.RUNS_TABLE, "r1")
    assert row["status"] == protocol.RUN_FAILED
    assert row["error"] == "The run has no files."


def test_inline_dispatch_never_promises_a_retry_it_cannot_deliver(monkeypatch, tmp_path):
    """Nothing retries the BackgroundTasks path, so a transient fault (the
    per-worker capacity assertion says "the run will be retried") must not be
    recorded with the queue's wording."""
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row(status=protocol.RUN_PENDING)]})
    monkeypatch.setattr(ri, "_active_runs", 1)
    ri.run_in_background("r1")
    row = env.db.row(ri.RUNS_TABLE, "r1")
    assert row["status"] == protocol.RUN_FAILED and row["completed_at"]
    assert row["error"] == ri._MSG_INLINE_TRANSIENT
    assert "will be retried" not in row["error"]
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] in ri._RUN_RETRYABLE


def test_inline_dispatch_fails_a_run_a_shutdown_left_active(monkeypatch, tmp_path):
    """With no llm_jobs row there is no lease to expire and no sweep to find
    it: a run left `running` by the shutdown would hang there forever, since
    /retry and DELETE both refuse a running run."""
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))

    def sandbox(i, pdf, kw):
        ri.SHUTTING_DOWN.set()
        assert kw["should_abort"]() is True
        return _result([], status=runner.STATUS_FAILED, code=protocol.FAIL_INTERRUPTED)

    _script_sandbox(monkeypatch, env, sandbox)
    try:
        ri.run_in_background("r1")
    finally:
        ri.SHUTTING_DOWN.clear()
    row = env.db.row(ri.RUNS_TABLE, "r1")
    assert row["status"] == protocol.RUN_FAILED and row["status"] in ri._RUN_RETRYABLE
    assert row["error"] == ri._MSG_INLINE_STOPPED
    assert env.db.files("r1")[0]["status"] == protocol.STATUS_PENDING   # handed back


# ── execute: the happy path and status derivation ────────────────────────


def test_execute_verifies_every_file_and_marks_the_run_done(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path)
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(2))
    ri.execute("r1")
    run = env.db.row(ri.RUNS_TABLE, "r1")
    assert run["status"] == protocol.RUN_DONE and run["error"] is None
    assert run["limits_hash"] and run["limits"]["max_pages"] == 3000
    assert run["sandbox_version"] == protocol.SANDBOX_VERSION
    assert (run["file_count"], run["files_verified"], run["pages_total"]) == (2, 2, 4)
    assert run["completed_at"] and run["started_at"]
    assert len(env.bells) == 1 and "2 verified" in env.bells[0]["message"]
    for frow in env.db.files("r1"):
        assert frow["status"] == protocol.STATUS_VERIFIED
        assert frow["claim_token"] and frow["phase"] is None and frow["child_pid"] is None
        assert (frow["page_count"], frow["pages_ok"], frow["pages_failed"]) == (2, 2, 0)
        content = env.store.content_for(frow["quarantine_path"])
        assert frow["sha256"] == hashlib.sha256(content).hexdigest()
        assert frow["size_bytes"] == len(content)
        assert frow["derived_prefix"] == f"r1/{frow['id']}"
        assert frow["text_path"] == f"r1/{frow['id']}/text.json"
        assert frow["images_pdf_paths"] == [f"r1/{frow['id']}/images-001.pdf"]
        assert frow["limits_hash"] == run["limits_hash"]
        assert frow["finished_at"] and frow["reject_code"] is None
        manifest = frow["manifest"]
        assert manifest["verdict"] == {"status": "verified", "code": None, "detail": None}
        assert manifest["identity"]["sha256"] == frow["sha256"]
        assert manifest["identity"]["source"] == {
            "kind": "upload", "quarantine_path": frow["quarantine_path"],
        }
        assert manifest["sniff"]["verdict"] is None and "byte_markers" in manifest["sniff"]
        assert manifest["structure"]["page_count"] == 2
        assert manifest["provenance"]["uid_switch_applied"] is False
        assert manifest["provenance"]["limits_hash"] == run["limits_hash"]
        assert manifest["output"]["images_pdf"]["parts"] == 1
        assert manifest["resources"]["bounds_hit"] == []
    # The sandbox was called once per file with the run's limits and budget.
    assert len(env.sandbox_calls) == 2
    first, second = env.sandbox_calls
    assert first["max_pages_remaining"] == 20000 and second["max_pages_remaining"] == 19998
    assert first["limits"].limits_hash() == run["limits_hash"]
    assert first["file_timeout_seconds"] == 300 + 1500 * 3000 // 1000
    assert first["pdf_path"].name == "source.pdf" and not first["pdf_path"].exists()
    assert first["renew"] is ri._renew and first["uid_slot"] is None
    # Derived objects: pages in parallel, then text.json, manifest.json, the parts.
    fid = env.db.files("r1")[0]["id"]
    assert env.store.many[0][0] == rs.DERIVED_BUCKET
    assert [p for p, _, _ in env.store.many[0][1]] == [
        f"r1/{fid}/thumb/0000.jpg", f"r1/{fid}/full/0000.jpg",
        f"r1/{fid}/thumb/0001.jpg", f"r1/{fid}/full/0001.jpg",
    ]
    assert env.store.many[0][1][0][1] == _jpeg(0)
    uploads = [(p, ct) for _, p, _, ct in env.store.uploads]
    assert uploads[:2] == [(f"r1/{fid}/text.json", "application/json"),
                           (f"r1/{fid}/manifest.json", "application/json")]
    text = json.loads(env.store.uploads[0][2])
    assert [p["index"] for p in text["pages"]] == [0, 1]
    assert text["pages"][0]["text"].startswith("Section 26")
    assert env.store.files[0][:2] == (rs.DERIVED_BUCKET, f"r1/{fid}/images-001.pdf")
    assert env.store.files[0][2].startswith(b"%PDF-1.4")
    assert env.store.files[0][3] == "application/pdf"
    # Pages rows, one per index.
    pages = sorted((r for r in env.db.tables[ri.PAGES_TABLE] if r["file_id"] == fid),
                   key=lambda r: r["page_index"])
    assert [p["page_index"] for p in pages] == [0, 1]
    assert pages[0]["thumb_path"] == f"r1/{fid}/thumb/0000.jpg"
    assert (pages[0]["thumb_w"], pages[0]["full_h"]) == (120, 310)
    assert pages[0]["text_chars"] == len("Section 26 05 00 electrical")
    # Nothing left in scratch.
    assert list((tmp_path / "scratch").glob("rfp-ingest-*")) == []


def test_pages_are_uploaded_in_batches_and_never_held_in_memory(monkeypatch, tmp_path):
    """Spec section 2: the runner delivers each validated page and keeps no
    reference to its bytes, and this process holds at most one upload batch.
    Nothing else bounds the worker: 3000 pages of a 30x42 in drawing set are
    hundreds of MB of JPEG."""
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))
    result = _complete(70)
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: result)
    ri.execute("r1")
    fid = env.db.files("r1")[0]["id"]
    # 32 pages per batch, two objects per page, in index order, no repeats.
    assert [len(items) for _, items in env.store.many] == [64, 64, 12]
    assert all(bucket == rs.DERIVED_BUCKET for bucket, _ in env.store.many)
    uploaded = [path for _, items in env.store.many for path, _, _ in items]
    assert uploaded == [
        f"r1/{fid}/{kind}/{i:04d}.jpg" for i in range(70) for kind in ("thumb", "full")
    ]
    assert len(set(uploaded)) == len(uploaded)
    assert env.store.many[2][1][0][1] == _jpeg(64)          # the tail batch is page 64
    # The sink dropped every buffer as it went.
    assert all(page.thumb is None and page.full is None for page in result.pages)
    rows = [r for r in env.db.tables[ri.PAGES_TABLE] if r["file_id"] == fid]
    assert len(rows) == 70
    frow = env.db.files("r1")[0]
    assert frow["status"] == protocol.STATUS_VERIFIED and frow["pages_ok"] == 70
    assert frow["manifest"]["output"]["images_pdf"]["parts"] == 1


def test_a_fat_artifact_flushes_the_batch_before_the_page_count_does(monkeypatch, tmp_path):
    """The page count alone does not bound memory: the protocol accepts up to
    MAX_THUMB_BYTES + MAX_FULL_BYTES per page, so a child that pads every
    artifact to its cap could park 32 of them in this worker before the first
    flush. The batch is bounded in bytes as well as in pages."""
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))
    monkeypatch.setattr(ri, "_UPLOAD_BATCH_BYTES", 4096)
    pages = [_page_sized(i, 1500) for i in range(10)]
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _result(pages))
    ri.execute("r1")
    # Two pages (4 objects, 6000 bytes) trip the budget long before 32 pages.
    assert [len(items) for _, items in env.store.many] == [4, 4, 4, 4, 4]
    sizes = [sum(len(data) for _, data, _ in items) for _, items in env.store.many]
    assert max(sizes) <= 4096 + 2 * 1500        # the budget plus the page that crossed it
    fid = env.db.files("r1")[0]["id"]
    uploaded = [path for _, items in env.store.many for path, _, _ in items]
    assert uploaded == [
        f"r1/{fid}/{kind}/{i:04d}.jpg" for i in range(10) for kind in ("thumb", "full")
    ]
    frow = env.db.files("r1")[0]
    assert frow["status"] == protocol.STATUS_VERIFIED and frow["pages_ok"] == 10


def test_an_artifact_larger_than_the_whole_budget_is_uploaded_on_its_own(monkeypatch, tmp_path):
    """Both bounds are checked after the page is queued, so one oversized page
    ships alone instead of being stranded in a queue it can never fit."""
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))
    monkeypatch.setattr(ri, "_UPLOAD_BATCH_BYTES", 2048)
    pages = [_page_sized(0, 5000), _page_sized(1, 100), _page_sized(2, 100)]
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _result(pages))
    ri.execute("r1")
    assert [len(items) for _, items in env.store.many] == [2, 4]
    assert sum(len(data) for _, data, _ in env.store.many[0][1]) == 10000
    frow = env.db.files("r1")[0]
    assert frow["status"] == protocol.STATUS_VERIFIED and frow["pages_ok"] == 3


def test_every_upload_batch_renews_the_lease_and_re_checks_the_abort(monkeypatch, tmp_path):
    """The upload phase of a large file is minutes long with no child running:
    without a renew per batch the lease expires, the sweep hands the run to a
    second worker and the first keeps writing into a prefix that worker has
    already deleted."""
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))
    renews = []
    monkeypatch.setattr(
        llm_queue, "renew_lease",
        lambda job=None: renews.append(len(env.store.many)) or len(renews) < 3,
    )
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(40))
    ri.execute("r1")                                   # LeaseLost is swallowed at the top
    assert renews == [0, 0, 1]                         # the loop head, then one per batch
    assert len(env.store.many) == 1                    # the second batch never went up
    frow = env.db.files("r1")[0]
    assert frow["status"] == protocol.STATUS_RUNNING and frow["claim_token"]
    assert env.db.tables.get(ri.PAGES_TABLE, []) == []  # nothing written for this attempt
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_RUNNING
    assert env.bells == []


def test_a_cancel_mid_delivery_stops_the_upload(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))
    monkeypatch.setattr(ri, "_CANCEL_POLL_SECONDS", 0.0)
    store_upload = env.store.upload_many

    def cancel_after_the_first_batch(bucket, items, max_workers=4):
        store_upload(bucket, items, max_workers=max_workers)
        if len(env.store.many) == 1:
            ri.cancel_run("r1")

    monkeypatch.setattr(rs, "upload_many", cancel_after_the_first_batch)
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(70))
    ri.execute("r1")
    assert len(env.store.many) == 1                    # batches 2 and 3 never ran
    frow = env.db.files("r1")[0]
    assert frow["status"] == protocol.STATUS_PENDING   # handed back for the retry
    assert frow["reject_code"] == protocol.FAIL_INTERRUPTED
    _assert_verdict_text(frow)
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_CANCELED
    assert env.db.tables.get(ri.PAGES_TABLE, []) == []
    assert env.store.uploads == [] and env.store.files == []


def test_a_verdict_after_a_delivery_takes_its_uploads_with_it(monkeypatch, tmp_path):
    """The runner re-reads and re-hashes every page as it delivers it, so it
    can still refuse the file (failed/invalid_output) after pages have been
    uploaded; those objects must not outlive the verdict."""
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))

    def deliver_then_refuse(i, pdf, kw):
        result = _complete(40)
        for page in sorted(result.pages, key=lambda p: p.index):
            thumb, full = page.thumb, page.full
            page.thumb, page.full = None, None
            kw["page_sink"](page, thumb, full)       # one full batch goes up
        return _result(result.pages, status=runner.STATUS_FAILED,
                       code=protocol.FAIL_INVALID_OUTPUT)

    _script_sandbox(monkeypatch, env, deliver_then_refuse)
    ri.execute("r1")
    fid = env.db.files("r1")[0]["id"]
    assert len(env.store.many) == 1                    # the first batch did go up
    assert (rs.DERIVED_BUCKET, f"r1/{fid}") in env.store.deleted
    frow = env.db.files("r1")[0]
    assert (frow["status"], frow["reject_code"]) == (
        protocol.STATUS_FAILED, protocol.FAIL_INVALID_OUTPUT,
    )
    _assert_verdict_text(frow)
    assert env.db.tables.get(ri.PAGES_TABLE, []) == []


def test_the_pages_rows_are_replaced_by_the_attempt_that_holds_the_claim(monkeypatch, tmp_path):
    """The rows and the file row's page_count / pages_ok / pages_failed have
    to come from the SAME attempt: an upsert that ignored duplicates let an
    earlier attempt's rows outlive the winner's."""
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))
    env.db.tables[ri.PAGES_TABLE] = [
        {"id": "stale0", "file_id": "f1", "page_index": 0, "status": "failed",
         "code": "render_error", "thumb_path": None},
        {"id": "stale9", "file_id": "f1", "page_index": 9, "status": "ok",
         "thumb_path": "r1/f1/thumb/0009.jpg"},
        {"id": "other", "file_id": "f2", "page_index": 0, "status": "ok"},
    ]
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(2))
    ri.execute("r1")
    rows = sorted((r for r in env.db.tables[ri.PAGES_TABLE] if r["file_id"] == "f1"),
                  key=lambda r: r["page_index"])
    assert [r["page_index"] for r in rows] == [0, 1]
    assert all(r["status"] == protocol.PAGE_OK for r in rows)
    assert rows[0]["id"] not in {"stale0", "stale9"}
    assert rows[0]["thumb_path"] == "r1/f1/thumb/0000.jpg"
    assert {r["id"] for r in env.db.tables[ri.PAGES_TABLE] if r["file_id"] == "f2"} == {"other"}
    frow = env.db.row(ri.FILES_TABLE, "f1")
    assert (frow["page_count"], frow["pages_ok"], frow["pages_failed"]) == (2, 2, 0)


def test_a_lost_claim_during_the_upload_writes_no_verdict(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))

    def steal(bucket, items, max_workers=4):
        env.db.row(ri.FILES_TABLE, "f1")["claim_token"] = "someone-else"
        env.store.upload_many(bucket, items, max_workers=max_workers)

    monkeypatch.setattr(rs, "upload_many", steal)
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(40))
    ri.execute("r1")
    frow = env.db.row(ri.FILES_TABLE, "f1")
    assert frow["claim_token"] == "someone-else"       # the other attempt owns it
    assert frow["status"] == protocol.STATUS_RUNNING and frow["finished_at"] is None
    assert len(env.store.many) == 1                    # we stopped at the next batch
    assert env.db.tables.get(ri.PAGES_TABLE, []) == []


def test_the_child_cannot_poison_the_typed_version_columns(monkeypatch, tmp_path):
    """protocol_version lands in an int4 column: a child claiming something
    else would make the terminal CAS fail with 22003/22P02 forever, and every
    retry would re-render and re-upload the whole file before failing the
    same way."""
    assert ri._child_protocol_version(2**63) == protocol.PROTOCOL_VERSION
    assert ri._child_protocol_version("3") == protocol.PROTOCOL_VERSION
    assert ri._child_protocol_version(True) == protocol.PROTOCOL_VERSION
    assert ri._child_protocol_version(3) == 3
    assert ri._child_sandbox_version({"a": 1}) == protocol.SANDBOX_VERSION
    assert ri._child_sandbox_version("x" * 200) == "x" * 64
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _result(
        [_page_ok(0)],
        start={"protocol_version": 9223372036854775000, "sandbox_version": 7,
               "limits_applied": {}},
    ))
    ri.execute("r1")
    frow = env.db.row(ri.FILES_TABLE, "f1")
    assert frow["status"] == protocol.STATUS_VERIFIED
    assert frow["protocol_version"] == protocol.PROTOCOL_VERSION
    assert frow["sandbox_version"] == protocol.SANDBOX_VERSION


def test_the_write_fence_never_leaves_the_process(monkeypatch, tmp_path):
    """claim_token is the value every fenced write is compared against; the
    run detail has no reason to ship it to the browser."""
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row(status=protocol.RUN_RUNNING)],
        ri.FILES_TABLE: [_file_row("f1", status=protocol.STATUS_RUNNING,
                                   claim_token="SEKRET", manifest={"big": 1})],
    })
    assert "claim_token" not in ri._FILE_COLUMNS
    assert "claim_token" not in ri.get_run("r1")["files"][0]
    assert "claim_token" not in ri.get_file("f1")
    assert ri.get_file("f1")["manifest"] == {"big": 1}
    assert env.db.row(ri.FILES_TABLE, "f1")["claim_token"] == "SEKRET"   # still fenced on


def test_the_fake_drops_null_rows_on_neq_like_postgrest(monkeypatch, tmp_path):
    """`col <> 'x'` is NULL, not true, for a NULL column, so PostgREST does
    not return the row; a fake that kept it would assert this slice against a
    strictly larger row set than production returns."""
    env = _env(monkeypatch, tmp_path, {ri.FILES_TABLE: [
        _file_row("null-sha", sha256=None), _file_row("same", sha256="abc", seq=1),
        _file_row("other", sha256="zzz", seq=2),
    ]})
    rows = env.db.table(ri.FILES_TABLE).select("id").neq("sha256", "abc").execute().data
    assert [r["id"] for r in rows] == ["other"]
    rows = env.db.table(ri.FILES_TABLE).select("id").is_("sha256", "null").execute().data
    assert [r["id"] for r in rows] == ["null-sha"]


def test_execute_marks_gaps_and_writes_placeholder_rows(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(4, failed={2}))
    ri.execute("r1")
    frow = env.db.files("r1")[0]
    assert frow["status"] == protocol.STATUS_VERIFIED_WITH_GAPS
    assert (frow["page_count"], frow["pages_ok"], frow["pages_failed"]) == (4, 3, 1)
    assert frow["manifest"]["verify"]["failed_pages"] == [{"index": 2, "code": "render_error"}]
    gap = next(r for r in env.db.tables[ri.PAGES_TABLE] if r["page_index"] == 2)
    assert gap["status"] == protocol.PAGE_FAILED and gap["code"] == protocol.PAGE_FAIL_RENDER
    assert gap["detail"] == runner.PAGE_DETAIL_MESSAGES[protocol.PAGE_FAIL_RENDER]
    assert gap["thumb_path"] is None
    run = env.db.row(ri.RUNS_TABLE, "r1")
    assert run["status"] == protocol.RUN_DONE and run["files_gapped"] == 1
    assert run["pages_total"] == 4
    text = json.loads(env.store.uploads[0][2])
    assert text["pages"][2] == {"index": 2, "chars": 0, "truncated": False, "hazards": {},
                                "text": None, "status": "failed"}
    assert [p for p, _, _ in env.store.many[0][1]] == [
        f"r1/{frow['id']}/{kind}/{i:04d}.jpg" for i in (0, 1, 3) for kind in ("thumb", "full")
    ]


def test_execute_records_the_runner_gap_verdict_without_uploading(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))
    pages = [_page_failed(i) for i in range(3)]
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _result(
        pages, status=runner.STATUS_REJECTED, code=protocol.REJECT_TOO_MANY_FAILED_PAGES,
        bounds=[protocol.BOUND_FAILED_PAGE_RATIO],
    ))
    ri.execute("r1")
    frow = env.db.files("r1")[0]
    assert frow["status"] == protocol.STATUS_REJECTED
    assert frow["reject_code"] == protocol.REJECT_TOO_MANY_FAILED_PAGES
    _assert_verdict_text(frow)
    assert frow["manifest"]["resources"]["bounds_hit"] == ["failed_page_ratio"]
    assert frow["manifest"]["verify"]["pages_failed"] == 3
    assert frow["page_count"] == 3
    assert env.store.many == [] and env.store.uploads == [] and env.store.files == []
    assert env.db.tables.get(ri.PAGES_TABLE, []) == []
    run = env.db.row(ri.RUNS_TABLE, "r1")
    assert run["status"] == protocol.RUN_DONE and run["files_rejected"] == 1


def test_all_rejected_is_still_done(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path)
    env.store.download_bytes = b"MZ definitely not a pdf" * 10
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: pytest.fail("no sandbox for a reject"))
    ri.execute("r1")
    for frow in env.db.files("r1"):
        assert frow["status"] == protocol.STATUS_REJECTED
        assert frow["reject_code"] == protocol.REJECT_NOT_PDF
        assert frow["manifest"]["sniff"]["verdict"] == protocol.REJECT_NOT_PDF
        assert "sha256" not in frow["manifest"]["identity"]   # rejected before hashing
    run = env.db.row(ri.RUNS_TABLE, "r1")
    assert run["status"] == protocol.RUN_DONE
    assert (run["files_rejected"], run["files_failed"], run["pages_total"]) == (2, 0, 0)


def test_mixed_failures_are_done_with_errors_and_all_failed_is_failed(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path)
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(1) if i == 0 else _result(
        [], status=runner.STATUS_FAILED, code=protocol.FAIL_RESOURCE_LIMIT,
        bounds=[protocol.BOUND_FILE_TIMEOUT],
    ))
    ri.execute("r1")
    run = env.db.row(ri.RUNS_TABLE, "r1")
    assert run["status"] == protocol.RUN_DONE_WITH_ERRORS
    assert (run["files_verified"], run["files_failed"]) == (1, 1)
    failed = env.db.files("r1")[1]
    assert failed["reject_code"] == protocol.FAIL_RESOURCE_LIMIT
    assert failed["error"] == protocol.VERDICT_MESSAGES[protocol.FAIL_RESOURCE_LIMIT]
    assert failed["manifest"]["resources"]["bounds_hit"] == ["file_timeout"]

    env = _upload_run(monkeypatch, tmp_path)
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _result(
        [], status=runner.STATUS_FAILED, code=protocol.FAIL_INVALID_OUTPUT,
    ))
    ri.execute("r1")
    run = env.db.row(ri.RUNS_TABLE, "r1")
    assert run["status"] == protocol.RUN_FAILED and run["files_failed"] == 2
    assert run["error"] == "Every file in the run failed; retry the run."
    assert env.bells[-1]["message"].startswith("RFP ingestion run failed: Every file")


def test_execute_returns_quietly_when_the_run_is_not_pending_or_running(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path)
    env.db.row(ri.RUNS_TABLE, "r1")["status"] = protocol.RUN_CANCELED
    ri.execute("r1")
    assert env.sandbox_calls == []
    assert env.db.files("r1")[0]["status"] == protocol.STATUS_PENDING
    assert env.bells == []


def test_orphaned_leftovers_only_when_no_other_job_owns_the_run(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))
    env.db.row(ri.RUNS_TABLE, "r1")["status"] = protocol.RUN_RUNNING
    limits = runner.SandboxLimits.from_settings(env.settings)
    ctx = ri._RunContext(env.settings, env.db, env.db.row(ri.RUNS_TABLE, "r1"), limits,
                         tmp_path)
    env.queue.active = {"id": "other-job", "status": "queued"}
    ri._orphan_leftovers(ctx)
    assert env.db.files("r1")[0]["status"] == protocol.STATUS_PENDING
    ri._derive_run_status(ctx)     # active files: the run is left for the other job
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_RUNNING
    env.queue.active = None
    ri._orphan_leftovers(ctx)
    frow = env.db.files("r1")[0]
    assert frow["status"] == protocol.STATUS_FAILED
    assert frow["reject_code"] == protocol.FAIL_ORPHANED
    assert frow["error"] == protocol.VERDICT_MESSAGES[protocol.FAIL_ORPHANED]
    ri._derive_run_status(ctx)
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_FAILED
    # The job executing this thread counts as ours.
    env.db.tables[ri.FILES_TABLE].append(_file_row("late", seq=5))
    env.db.row(ri.RUNS_TABLE, "r1")["status"] = protocol.RUN_RUNNING
    env.queue.active = {"id": "mine", "status": "running"}
    token = llm_queue.current_job.set({"id": "mine"})
    try:
        ri._orphan_leftovers(ctx)
    finally:
        llm_queue.current_job.reset(token)
    assert env.db.row(ri.FILES_TABLE, "late")["reject_code"] == protocol.FAIL_ORPHANED


# ── execute: budgets, bounds, refusals ───────────────────────────────────


def test_run_page_budget_rejects_the_overflow_file_without_a_spawn(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path, rfp_ingest_max_pages_per_run=2)
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(2))
    ri.execute("r1")
    assert len(env.sandbox_calls) == 1
    first, second = env.db.files("r1")
    assert first["status"] == protocol.STATUS_VERIFIED
    assert second["status"] == protocol.STATUS_REJECTED
    assert second["reject_code"] == protocol.REJECT_RUN_PAGE_BUDGET
    assert second["manifest"]["resources"]["bounds_hit"] == ["run_page_budget"]
    _assert_verdict_text(second)
    run = env.db.row(ri.RUNS_TABLE, "r1")
    assert run["status"] == protocol.RUN_DONE and run["pages_total"] == 2
    # A retry re-derives consumed from the verified files.
    env.db.tables[ri.FILES_TABLE].append(_file_row("f3", seq=7))
    env.db.row(ri.RUNS_TABLE, "r1")["status"] = protocol.RUN_PENDING
    ri.execute("r1")
    assert len(env.sandbox_calls) == 1
    assert env.db.row(ri.FILES_TABLE, "f3")["reject_code"] == protocol.REJECT_RUN_PAGE_BUDGET


def test_images_pdf_builder_failure_is_a_bound_not_a_file_failure(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))
    page = _page_ok(0)
    page.thumb_meta = {**page.thumb_meta, "w": 0}   # the writer refuses a zero width
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _result([page]))
    ri.execute("r1")
    frow = env.db.files("r1")[0]
    assert frow["status"] == protocol.STATUS_VERIFIED
    assert frow["images_pdf_paths"] == []
    assert frow["manifest"]["resources"]["bounds_hit"] == [protocol.BOUND_IMAGES_PDF]
    assert frow["manifest"]["output"]["images_pdf"] == {
        "parts": 0, "bytes": 0, "built": False, "paths": [],
    }
    assert env.store.files == []                      # no part uploaded
    assert len(env.store.many) == 1                   # the pages still went up
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_DONE


def test_text_json_is_capped_with_the_bound_recorded(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path, files=("f1",), rfp_ingest_max_text_bytes_per_file=500)
    pages = [_page_ok(i, text="x" * 100) for i in range(3)]
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _result(pages))
    ri.execute("r1")
    frow = env.db.files("r1")[0]
    assert frow["status"] == protocol.STATUS_VERIFIED
    assert frow["manifest"]["resources"]["bounds_hit"] == [protocol.BOUND_TEXT_BYTES]
    assert frow["manifest"]["output"]["text_truncated"] is True
    raw = env.store.uploads[0][2]
    assert len(raw) <= 500
    text = json.loads(raw)
    assert text["pages"][0]["text"] == "x" * 100 and text["pages"][0]["truncated"] is False
    assert text["pages"][1]["truncated"] is True
    assert 0 < len(text["pages"][1]["text"]) < 100
    assert all(p["truncated"] and p["text"] == "" for p in text["pages"][2:])


def test_a_bound_hit_by_both_the_runner_and_the_parent_is_listed_once(monkeypatch, tmp_path):
    """The runner spends the per-file text budget as it validates pages and
    the parent spends it again writing text.json, so one fat file trips
    `text_bytes` on both sides. The manifest is a list of what was hit, not a
    tally, so it carries the bound once."""
    env = _upload_run(monkeypatch, tmp_path, files=("f1",), rfp_ingest_max_text_bytes_per_file=500)
    pages = [_page_ok(i, text="x" * 100) for i in range(3)]
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _result(
        pages, bounds=[protocol.BOUND_TEXT_BYTES],
    ))
    ri.execute("r1")
    assert env.sandbox_calls[0]["max_text_bytes_per_file"] == 500   # one budget, not two
    frow = env.db.files("r1")[0]
    assert frow["status"] == protocol.STATUS_VERIFIED
    bounds = frow["manifest"]["resources"]["bounds_hit"]
    assert bounds == [protocol.BOUND_TEXT_BYTES]
    assert len(bounds) == len(set(bounds))


def test_text_json_helper_keeps_whole_pages_under_a_generous_cap():
    pages = [_page_ok(0, text="café   hidden"), _page_failed(1)]
    data, truncated, with_text = ri._text_json(SimpleNamespace(
        rfp_ingest_max_text_bytes_per_file=10_000), pages)
    assert truncated is False and with_text == 1
    parsed = json.loads(data)
    assert parsed["pages"][0]["text"] == "café   hidden"
    assert parsed["pages"][1]["text"] is None


def test_failed_self_test_refuses_the_run(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path)
    monkeypatch.setattr(ri, "_last_self_test", {"ok": False, "at": ri._now_iso(),
                        "detail": "The sandbox self-test "
                        "ended failed/spawn: The sandbox could not be started; check the "
                        "deployment."})
    ri.execute("r1")
    run = env.db.row(ri.RUNS_TABLE, "r1")
    assert run["status"] == protocol.RUN_FAILED
    assert run["error"].startswith("The sandbox self-test ended failed/spawn")
    assert env.sandbox_calls == []
    assert env.db.files("r1")[0]["status"] == protocol.STATUS_PENDING
    assert len(env.bells) == 1
    # A passing (or never run) self-test lets runs through.
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))
    monkeypatch.setattr(ri, "_last_self_test", {"ok": True, "detail": "fine"})
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(1))
    ri.execute("r1")
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_DONE


def test_a_stale_failed_self_test_is_re_run_before_a_run_is_refused(monkeypatch, tmp_path):
    """A cached failure must not pin a worker for its whole life: the only
    other self-test callers are the lifespan boot and GET /status, which the
    load balancer may never route to this worker again."""
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))
    stale = (NOW - timedelta(hours=3)).isoformat()
    monkeypatch.setattr(ri, "_now", lambda: NOW)
    monkeypatch.setattr(ri, "_last_self_test", {"ok": False, "at": stale, "detail": "broken"})
    runs = []
    monkeypatch.setattr(ri, "self_test", lambda: runs.append(1) or {
        "ok": True, "at": ri._now_iso(), "detail": "fine",
    })
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(1))
    ri.execute("r1")
    assert runs == [1]
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_DONE


def test_a_busy_self_test_slot_retries_the_run_and_is_never_cached(monkeypatch, tmp_path):
    """Contention for the dedicated uid slot (both uvicorn workers boot at
    once) says nothing about this sandbox: it must not be recorded as a
    verdict, and the run it meets is retried, never failed."""
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))
    monkeypatch.setattr(ri, "_geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(runner, "_geteuid", lambda: 0)
    monkeypatch.setattr(ri, "_self_test_slot", lambda s, root: (None, "busy"))
    monkeypatch.setattr(runner, "run_sandbox",
                        lambda *a, **k: pytest.fail("no child when the slot is busy"))
    monkeypatch.setattr(ri, "_last_self_test", {"ok": True, "at": ri._now_iso(),
                                                "detail": "fine"})
    verdict = ri.self_test()
    assert verdict["ok"] is None and verdict["detail"] == ri._MSG_SELF_TEST_SLOT_BUSY
    assert ri.last_self_test()["ok"] is True            # the good verdict still stands
    # An unknown verdict reaching execute() is a retryable fault, not a mark.
    monkeypatch.setattr(ri, "_last_self_test", {"ok": None, "at": ri._now_iso(),
                                                "detail": ri._MSG_SELF_TEST_SLOT_BUSY})
    with pytest.raises(ri.RfpIngestTransient) as exc:
        ri.execute("r1")
    assert str(exc.value) == ri._MSG_SELF_TEST_SLOT_BUSY
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_PENDING
    assert env.bells == []


def test_spawn_failure_fails_the_file_and_aborts_the_run(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path)

    def spawn(i, pdf, kw):
        raise runner.SandboxSpawnError("The sandbox scratch layout could not be prepared.")

    _script_sandbox(monkeypatch, env, spawn)
    ri.execute("r1")
    first, second = env.db.files("r1")
    assert first["status"] == protocol.STATUS_FAILED
    assert first["reject_code"] == protocol.FAIL_SPAWN
    assert first["error"] == protocol.VERDICT_MESSAGES[protocol.FAIL_SPAWN]
    assert second["status"] == protocol.STATUS_PENDING      # never attempted
    run = env.db.row(ri.RUNS_TABLE, "r1")
    assert run["status"] == protocol.RUN_FAILED
    assert run["error"] == protocol.VERDICT_MESSAGES[protocol.FAIL_SPAWN]
    assert run["files_failed"] == 1 and len(env.sandbox_calls) == 1


def test_a_result_level_spawn_failure_is_one_file_not_the_whole_run(monkeypatch, tmp_path):
    """A child that cannot start (EXIT_BAD_ARGS, no start or ready event) comes
    back as a final SandboxResult(failed, spawn) for THAT file. Only a
    parent-side SandboxSpawnError aborts the run, so the loop moves on to the
    next pending file and the run settles from the file verdicts."""
    env = _upload_run(monkeypatch, tmp_path)
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _result(
        [], status=runner.STATUS_FAILED, code=protocol.FAIL_SPAWN,
    ) if i == 0 else _complete(2))
    ri.execute("r1")
    first, second = env.db.files("r1")
    assert first["status"] == protocol.STATUS_FAILED
    assert first["reject_code"] == protocol.FAIL_SPAWN
    _assert_verdict_text(first)
    assert first["error"] == protocol.VERDICT_MESSAGES[protocol.FAIL_SPAWN]
    assert second["status"] == protocol.STATUS_VERIFIED      # the run carried on
    assert len(env.sandbox_calls) == 2                       # no respawn for file 1
    run = env.db.row(ri.RUNS_TABLE, "r1")
    assert run["status"] == protocol.RUN_DONE_WITH_ERRORS
    assert run["error"] is None                              # not the run-abort sentence
    assert (run["files_verified"], run["files_failed"]) == (1, 1)


def test_a_uid_slot_that_never_frees_requeues_the_run_instead_of_failing_the_file(
    monkeypatch, tmp_path
):
    """A SlotWaitTimeout is capacity, not a bad file: the next attempt may
    well get a slot. Letting it fall into the generic handler would burn the
    file as failed/interrupted, and raising SandboxSpawnError would abandon
    the whole run, so it is a transient fault and the queue retries."""
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))

    def wedged(i, pdf, kw):
        raise runner.SlotWaitTimeout("no sandbox uid slot came free")

    _script_sandbox(monkeypatch, env, wedged)
    with pytest.raises(ri.RfpIngestTransient) as exc:
        ri.execute("r1")
    assert str(exc.value) == ri._MSG_CAPACITY
    frow = env.db.row(ri.FILES_TABLE, "f1")
    assert frow["status"] == protocol.STATUS_RUNNING     # still claimed, retried as-is
    assert frow["reject_code"] is None and frow["error"] is None
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_RUNNING
    assert env.bells == []


def test_stale_running_files_are_reset_and_their_outputs_removed(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))
    stale = env.db.row(ri.FILES_TABLE, "f1")
    stale.update(status=protocol.STATUS_RUNNING, claim_token="old", phase="sandbox", pages_done=9)
    env.db.tables[ri.PAGES_TABLE] = [{"id": "p", "file_id": "f1", "page_index": 0,
                                      "status": "ok"}]
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(1))
    ri.execute("r1")
    frow = env.db.row(ri.FILES_TABLE, "f1")
    assert frow["status"] == protocol.STATUS_VERIFIED and frow["claim_token"] != "old"
    assert env.store.deleted[0] == (rs.DERIVED_BUCKET, "r1/f1")
    assert [r["page_index"] for r in env.db.tables[ri.PAGES_TABLE]] == [0]
    assert env.db.tables[ri.PAGES_TABLE][0]["id"] != "p"


def test_duplicate_content_inside_a_run_is_rejected_with_the_winner(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path)
    env.store.download_bytes = PDF + b"%same bytes for both\n"
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(1))
    ri.execute("r1")
    first, second = env.db.files("r1")
    assert first["status"] == protocol.STATUS_VERIFIED
    assert second["status"] == protocol.STATUS_REJECTED
    assert second["reject_code"] == protocol.REJECT_DUPLICATE
    assert second["error"] == protocol.VERDICT_MESSAGES[protocol.REJECT_DUPLICATE]
    assert second["manifest"]["identity"]["duplicate_of"] == first["id"]
    assert second["sha256"] is None                    # the CAS never applied
    assert len(env.sandbox_calls) == 1


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (rs.RfpStorageNotFound("The stored file was not found."), protocol.STATUS_REJECTED,
         protocol.REJECT_NOT_STORED),
        (rs.RfpStorageTooLarge("The stored file is larger than the cap."),
         protocol.STATUS_REJECTED, protocol.REJECT_TOO_LARGE),
        (rs.RfpStorageError("Storage returned HTTP 500."), protocol.STATUS_FAILED,
         protocol.FAIL_STORAGE),
        (httpx.ConnectError("boom"), protocol.STATUS_FAILED, protocol.FAIL_STORAGE),
    ],
)
def test_upload_materialization_verdicts(monkeypatch, tmp_path, exc, status, code):
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))
    env.store.download_exc = exc
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: pytest.fail("nothing to run"))
    ri.execute("r1")
    frow = env.db.files("r1")[0]
    assert (frow["status"], frow["reject_code"]) == (status, code)
    assert frow["error"] == protocol.VERDICT_MESSAGES[code]
    assert frow["manifest"]["verdict"]["code"] == code
    assert "sniff" not in frow["manifest"]


def test_upload_row_without_a_quarantine_object_is_failed_storage(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))
    env.db.row(ri.FILES_TABLE, "f1")["quarantine_path"] = None
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: pytest.fail("nothing to run"))
    ri.execute("r1")
    frow = env.db.files("r1")[0]
    assert (frow["status"], frow["reject_code"]) == (protocol.STATUS_FAILED, protocol.FAIL_STORAGE)


def test_low_free_disk_skips_the_file_as_failed_storage(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))
    monkeypatch.setattr(ri, "_disk_free", lambda path: 1024)
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: pytest.fail("no sandbox on a full disk"))
    ri.execute("r1")
    frow = env.db.files("r1")[0]
    assert (frow["status"], frow["reject_code"]) == (protocol.STATUS_FAILED, protocol.FAIL_STORAGE)
    assert frow["manifest"]["disk"]["free_bytes"] == 1024


def test_unexpected_exception_maps_to_a_retryable_file_failure(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))

    def broken(i, pdf, kw):
        raise TypeError("a bug in the runner")

    _script_sandbox(monkeypatch, env, broken)
    ri.execute("r1")
    frow = env.db.files("r1")[0]
    assert (frow["status"], frow["reject_code"]) == (
        protocol.STATUS_FAILED, protocol.FAIL_INTERRUPTED,
    )
    assert "bug" not in frow["error"]
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_FAILED


def test_derived_upload_failure_is_failed_storage(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(1))
    env.store.upload_exc = httpx.ReadError("dropped")
    ri.execute("r1")
    frow = env.db.files("r1")[0]
    assert (frow["status"], frow["reject_code"]) == (protocol.STATUS_FAILED, protocol.FAIL_STORAGE)
    assert frow["manifest"]["verdict"]["code"] == protocol.FAIL_STORAGE


def test_capacity_assertion_is_a_transient_fault(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path)
    monkeypatch.setattr(ri, "_active_runs", 1)
    with pytest.raises(ri.RfpIngestTransient):
        ri.execute("r1")
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_PENDING
    assert ri.active_run_count() == 1               # not double-released


# ── Cancel, lease loss, shutdown ─────────────────────────────────────────


def test_cancel_run_marks_canceled_first_then_cancels_the_queued_job(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row(status=protocol.RUN_PENDING)]})
    env.queue.active = {"id": "job7", "status": "queued"}
    order = []
    monkeypatch.setattr(
        llm_queue, "cancel",
        lambda job_id: order.append((job_id, env.db.row(ri.RUNS_TABLE, "r1")["status"])),
    )
    row = ri.cancel_run("r1")
    assert row["status"] == protocol.RUN_CANCELED
    assert order == [("job7", protocol.RUN_CANCELED)]   # canceled BEFORE the queue's mark
    assert len(env.bells) == 1
    # The queue's own failed mark (llm_queue.cancel -> mark_from_queue) loses.
    ri.mark_from_queue("r1", "failed", "Canceled from the AI monitor page.")
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_CANCELED
    for status in (protocol.RUN_DONE, protocol.RUN_FAILED, protocol.RUN_EXPIRED,
                   protocol.RUN_CANCELED):
        env.db.tables[ri.RUNS_TABLE].append(_run_row(status, status=status))
        with pytest.raises(ri.RfpIngestPermanent) as exc:
            ri.cancel_run(status)
        assert exc.value.http_status == 409
    with pytest.raises(ri.RfpIngestPermanent) as exc:
        ri.cancel_run("missing")
    assert exc.value.http_status == 404


def test_cancel_between_files_stops_the_loop_and_leaves_the_rest_pending(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path)
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(1))
    complete = ri._complete_file

    def complete_then_cancel(ctx, state, result, sink, manifest):
        # The cancel lands right after file 1 settles; the loop head sees
        # the sticky status before it claims file 2.
        complete(ctx, state, result, sink, manifest)
        ri.cancel_run("r1")

    monkeypatch.setattr(ri, "_complete_file", complete_then_cancel)
    ri.execute("r1")
    first, second = env.db.files("r1")
    assert first["status"] == protocol.STATUS_VERIFIED
    assert second["status"] == protocol.STATUS_PENDING and second["claim_token"] is None
    run = env.db.row(ri.RUNS_TABLE, "r1")
    assert run["status"] == protocol.RUN_CANCELED
    assert len(env.sandbox_calls) == 1
    assert len(env.bells) == 1 and env.bells[0]["metadata"]["status"] == protocol.RUN_CANCELED
    assert env.queue.requeued == 0


def test_cancel_during_the_upload_leaves_the_file_pending_for_a_retry(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path)

    def sandbox(i, pdf, kw):
        # The cancel arrives while the child runs but the fake child never
        # polls should_abort: _mark(canceled) hands the file back, so every
        # later fenced write of this attempt loses and nothing is marked.
        ri.cancel_run("r1")
        return _complete(1)

    _script_sandbox(monkeypatch, env, sandbox)
    ri.execute("r1")
    first, second = env.db.files("r1")
    assert first["status"] == protocol.STATUS_PENDING
    assert first["reject_code"] == protocol.FAIL_INTERRUPTED and first["claim_token"] is None
    assert second["status"] == protocol.STATUS_PENDING
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_CANCELED
    assert env.store.many == []                          # the claim re-check stopped the upload
    assert len(env.sandbox_calls) == 1


def test_cancel_mid_child_hands_the_file_back_and_removes_its_outputs(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path)
    ticks = []

    def sandbox(i, pdf, kw):
        assert kw["should_abort"]() is False
        ri.cancel_run("r1")
        monkeypatch.setattr(ri, "_CANCEL_POLL_SECONDS", 0.0)
        ticks.append(kw["should_abort"]())
        return _result([], status=runner.STATUS_FAILED, code=protocol.FAIL_INTERRUPTED)

    _script_sandbox(monkeypatch, env, sandbox)
    ri.execute("r1")
    assert ticks == [True]
    first, second = env.db.files("r1")
    assert first["status"] == protocol.STATUS_PENDING
    assert first["claim_token"] is None and first["phase"] is None
    assert first["reject_code"] == protocol.FAIL_INTERRUPTED
    _assert_verdict_text(first)
    assert (rs.DERIVED_BUCKET, f"r1/{first['id']}") in env.store.deleted
    assert second["status"] == protocol.STATUS_PENDING
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_CANCELED
    assert len(env.sandbox_calls) == 1 and env.queue.requeued == 0
    assert len(env.bells) == 1


def test_lease_lost_between_files_writes_nothing(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path)
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: False)
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: pytest.fail("no child after a lost lease"))
    ri.execute("r1")                                   # LeaseLost is swallowed at the top
    run = env.db.row(ri.RUNS_TABLE, "r1")
    assert run["status"] == protocol.RUN_RUNNING       # the new owner governs it
    assert all(f["status"] == protocol.STATUS_PENDING for f in env.db.files("r1"))
    assert env.bells == [] and env.queue.requeued == 0


def test_lease_lost_mid_child_leaves_the_claimed_file_alone(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path)
    renews = iter([True, False])
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: next(renews))

    def sandbox(i, pdf, kw):
        # The real monitor loop raises LeaseLost after renew() says no.
        assert kw["renew"]() is False
        raise runner.LeaseLost("lease lost while rendering")

    _script_sandbox(monkeypatch, env, sandbox)
    ri.execute("r1")
    first, second = env.db.files("r1")
    assert first["status"] == protocol.STATUS_RUNNING and first["claim_token"]
    assert first["phase"] == protocol.PHASE_SANDBOX
    assert second["status"] == protocol.STATUS_PENDING
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_RUNNING
    assert env.bells == [] and env.queue.requeued == 0
    assert list((tmp_path / "scratch").glob("rfp-ingest-*")) == []   # scratch still cleaned


def test_shutdown_mid_child_requeues_and_leaves_the_file_running(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path)

    def sandbox(i, pdf, kw):
        ri.SHUTTING_DOWN.set()
        assert kw["should_abort"]() is True
        return _result([], status=runner.STATUS_FAILED, code=protocol.FAIL_INTERRUPTED)

    _script_sandbox(monkeypatch, env, sandbox)
    try:
        ri.execute("r1")
    finally:
        ri.SHUTTING_DOWN.clear()
    first, second = env.db.files("r1")
    assert first["status"] == protocol.STATUS_RUNNING and first["claim_token"]
    assert second["status"] == protocol.STATUS_PENDING
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_RUNNING
    assert env.queue.requeued == 1
    assert env.bells == [] and len(env.sandbox_calls) == 1
    # The next attempt resets the stale claim and finishes the run.
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(1))
    ri.execute("r1")
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_DONE
    assert (rs.DERIVED_BUCKET, f"r1/{first['id']}") in env.store.deleted


def test_shutdown_between_files_requeues_without_touching_files(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path)
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: pytest.fail("no child during shutdown"))
    ri.SHUTTING_DOWN.set()
    try:
        ri.execute("r1")
    finally:
        ri.SHUTTING_DOWN.clear()
    assert env.queue.requeued == 1
    assert all(f["status"] == protocol.STATUS_PENDING for f in env.db.files("r1"))
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_RUNNING


def test_lost_file_claim_mid_child_abandons_the_file(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path)

    def sandbox(i, pdf, kw):
        if i == 0:
            # Someone reset the file under us (a retry from another worker
            # would look like this): the progress write finds no row.
            env.db.files("r1")[0]["claim_token"] = "someone-else"
            kw["on_progress"](protocol.PHASE_SANDBOX, 3, 4242)
            assert kw["should_abort"]() is True
            return _result([], status=runner.STATUS_FAILED, code=protocol.FAIL_INTERRUPTED)
        return _complete(1)

    _script_sandbox(monkeypatch, env, sandbox)
    ri.execute("r1")
    first, second = env.db.files("r1")
    assert first["status"] == protocol.STATUS_RUNNING and first["claim_token"] == "someone-else"
    assert first["pages_done"] == 0                     # the fenced write never landed
    assert second["status"] == protocol.STATUS_VERIFIED
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_RUNNING  # first still active


def test_progress_callback_writes_the_live_fields_fenced_on_the_claim(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))

    def sandbox(i, pdf, kw):
        kw["on_progress"](protocol.PHASE_SANDBOX, 5, 777)
        row = env.db.row(ri.FILES_TABLE, "f1")
        assert (row["phase"], row["pages_done"], row["child_pid"]) == ("sandbox", 5, 777)
        assert row["last_progress_at"]
        kw["on_progress"](protocol.PHASE_VERIFY, 9, 778)   # throttled: within 10 s
        assert env.db.row(ri.FILES_TABLE, "f1")["pages_done"] == 5
        return _complete(1)

    _script_sandbox(monkeypatch, env, sandbox)
    ri.execute("r1")
    row = env.db.row(ri.FILES_TABLE, "f1")
    assert row["status"] == protocol.STATUS_VERIFIED
    assert row["phase"] is None and row["child_pid"] is None and row["pages_done"] == 1


# ── Retry and delete ─────────────────────────────────────────────────────


def _retry_env(monkeypatch, tmp_path, status=protocol.RUN_FAILED):
    return _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row(status=status, error="old", completed_at=NOW.isoformat())],
        ri.FILES_TABLE: [
            _file_row("failed", status=protocol.STATUS_FAILED, reject_code="storage",
                      error="x", derived_prefix="r1/failed", text_path="r1/failed/text.json",
                      images_pdf_paths=["r1/failed/images-001.pdf"], page_count=3),
            _file_row("rejected", status=protocol.STATUS_REJECTED, reject_code="not_pdf",
                      error="y", seq=1),
            _file_row("running", status=protocol.STATUS_RUNNING, claim_token="tok", seq=2),
            _file_row("ok", status=protocol.STATUS_VERIFIED, page_count=4, seq=3),
        ],
        ri.PAGES_TABLE: [
            {"id": "pa", "file_id": "failed", "page_index": 0, "status": "ok"},
            {"id": "pb", "file_id": "ok", "page_index": 0, "status": "ok"},
        ],
    })


def test_retry_refuses_an_active_job_or_a_wrong_status(monkeypatch, tmp_path):
    env = _retry_env(monkeypatch, tmp_path)
    env.queue.active = {"id": "j", "status": "running"}
    with pytest.raises(ri.RfpIngestPermanent) as exc:
        ri.retry_run("r1", created_by=USER, background=BackgroundTasks())
    assert exc.value.http_status == 409 and "running" in str(exc.value)
    assert env.db.row(ri.FILES_TABLE, "failed")["status"] == protocol.STATUS_FAILED
    assert env.queue.enqueued == [] and env.store.deleted == []
    for status in (protocol.RUN_DONE, protocol.RUN_RUNNING, protocol.RUN_PENDING,
                   protocol.RUN_STAGING, protocol.RUN_EXPIRED):
        env = _retry_env(monkeypatch, tmp_path, status=status)
        with pytest.raises(ri.RfpIngestPermanent) as exc:
            ri.retry_run("r1", created_by=USER, background=BackgroundTasks())
        assert exc.value.http_status == 409


@pytest.mark.parametrize("status", sorted(ri._RUN_RETRYABLE))
def test_retry_resets_retryable_files_then_enqueues(monkeypatch, tmp_path, status):
    env = _retry_env(monkeypatch, tmp_path, status=status)
    row = ri.retry_run("r1", created_by=USER, background=BackgroundTasks())
    assert row["status"] == protocol.RUN_PENDING and row["error"] is None
    assert row["completed_at"] is None
    for fid in ("failed", "running"):
        frow = env.db.row(ri.FILES_TABLE, fid)
        assert frow["status"] == protocol.STATUS_PENDING
        assert frow["claim_token"] is None and frow["reject_code"] is None
        assert frow["error"] is None and frow["derived_prefix"] is None
        assert frow["images_pdf_paths"] is None and frow["page_count"] is None
    rejected = env.db.row(ri.FILES_TABLE, "rejected")
    assert rejected["status"] == protocol.STATUS_REJECTED and rejected["error"] == "y"
    assert env.db.row(ri.FILES_TABLE, "ok")["status"] == protocol.STATUS_VERIFIED
    assert {r["id"] for r in env.db.tables[ri.PAGES_TABLE]} == {"pb"}
    assert env.store.deleted == [(rs.DERIVED_BUCKET, "r1/failed"),
                                 (rs.DERIVED_BUCKET, "r1/running")]
    assert env.queue.enqueued == [{
        "job_type": llm_queue.JOB_RFP_INGEST, "target_id": "r1", "payload": {"run_id": "r1"},
        "created_by": USER, "priority": 200, "raise_on_active": True,
    }]


def test_retry_puts_the_run_back_when_the_enqueue_collides(monkeypatch, tmp_path):
    env = _retry_env(monkeypatch, tmp_path)

    def collide(*a, **k):
        raise llm_queue.JobAlreadyActive({"id": "j", "status": "queued"})

    monkeypatch.setattr(llm_queue, "enqueue", collide)
    with pytest.raises(ri.RfpIngestPermanent) as exc:
        ri.retry_run("r1", created_by=USER, background=BackgroundTasks())
    assert exc.value.http_status == 409
    run = env.db.row(ri.RUNS_TABLE, "r1")
    # The whole terminal shape goes back, not just the status: a run left
    # pending with completed_at NULL is terminal to every reader but can never
    # be reclaimed by the retention prune.
    assert run["status"] == protocol.RUN_FAILED
    assert run["completed_at"] == NOW.isoformat() and run["error"] == "old"
    monkeypatch.setattr(ri, "_now", lambda: NOW + timedelta(days=30))
    assert ri.prune_expired() == 1
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_EXPIRED


def test_retry_409s_when_the_run_changes_under_it(monkeypatch, tmp_path):
    """Spec 3.6: the run is CASed to pending fenced on the status just read.
    A cancel landing in that window must win, and the files of a run we did
    not get must not be touched at all."""
    env = _retry_env(monkeypatch, tmp_path)

    def cancel_mid_check(job_type, target_id):
        # The last read before the CAS: a cancel lands here.
        env.db.row(ri.RUNS_TABLE, target_id)["status"] = protocol.RUN_CANCELED
        return None

    monkeypatch.setattr(llm_queue, "active_job", cancel_mid_check)
    with pytest.raises(ri.RfpIngestPermanent) as exc:
        ri.retry_run("r1", created_by=USER, background=BackgroundTasks())
    assert exc.value.http_status == 409 and "changed while retrying" in str(exc.value)
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_CANCELED
    # The file reset runs only once the run CAS has won, so a live runner
    # keeps its claim and its outputs.
    running = env.db.row(ri.FILES_TABLE, "running")
    assert running["status"] == protocol.STATUS_RUNNING and running["claim_token"] == "tok"
    assert env.db.row(ri.FILES_TABLE, "failed")["status"] == protocol.STATUS_FAILED
    assert {r["id"] for r in env.db.tables[ri.PAGES_TABLE]} == {"pa", "pb"}
    assert env.store.deleted == [] and env.queue.enqueued == []


def test_retry_falls_back_to_background_tasks_when_the_queue_is_off(monkeypatch, tmp_path):
    env = _retry_env(monkeypatch, tmp_path)
    monkeypatch.setattr(ri, "get_settings", lambda: _settings(tmp_path, llm_queue_enabled=False))
    background = BackgroundTasks()
    ri.retry_run("r1", created_by=USER, background=background)
    assert background.tasks[0].func is ri.run_in_background
    assert env.queue.enqueued == []


def test_delete_run_fencing_then_both_prefixes(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row(s, status=s) for s in sorted(protocol.RUN_STATUSES)],
        ri.FILES_TABLE: [_file_row("f", run_id="done")],
    })
    for status in (protocol.RUN_PENDING, protocol.RUN_RUNNING):
        with pytest.raises(ri.RfpIngestPermanent) as exc:
            ri.delete_run(status)
        assert exc.value.http_status == 409
        assert env.db.row(ri.RUNS_TABLE, status)
    with pytest.raises(ri.RfpIngestPermanent) as exc:
        ri.delete_run("missing")
    assert exc.value.http_status == 404
    for status in sorted(ri._RUN_DELETABLE):
        ri.delete_run(status)
        assert all(r["id"] != status for r in env.db.tables[ri.RUNS_TABLE])
        assert (rs.DERIVED_BUCKET, status) in env.store.deleted
        assert (rs.QUARANTINE_BUCKET, status) in env.store.deleted
    assert env.store.deleted.index((rs.DERIVED_BUCKET, "done")) < (
        env.store.deleted.index((rs.QUARANTINE_BUCKET, "done"))
    )


def test_delete_refuses_while_the_queue_still_owns_the_run(monkeypatch, tmp_path):
    """Spec 3.6 tells the operator to cancel and wait for the runner's
    acknowledgement; the active job row IS that acknowledgement. Without the
    guard a DELETE a millisecond after a cancel raced the runner's upload
    phase, and every object written after delete_prefix was orphaned with no
    row and no prefix left to sweep."""
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row(status=protocol.RUN_CANCELED, completed_at=NOW.isoformat())],
        ri.FILES_TABLE: [_file_row("f", status=protocol.STATUS_RUNNING, claim_token="tok")],
    })
    env.queue.active = {"id": "j1", "status": "running"}
    with pytest.raises(ri.RfpIngestPermanent) as exc:
        ri.delete_run("r1")
    assert exc.value.http_status == 409
    assert env.db.row(ri.RUNS_TABLE, "r1") and env.store.deleted == []
    env.queue.active = None
    ri.delete_run("r1")
    assert env.db.tables[ri.RUNS_TABLE] == []


def test_delete_keeps_the_rows_when_the_objects_cannot_go(monkeypatch, tmp_path):
    """The rows are what the prune walks to find the objects, so losing them
    first turns any delete failure into a permanent orphan."""
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row(status=protocol.RUN_DONE, completed_at=NOW.isoformat())],
        ri.FILES_TABLE: [_file_row("f")],
    })

    def boom(bucket, prefix):
        raise rs.RfpStorageError("Storage returned HTTP 500.")

    monkeypatch.setattr(rs, "delete_prefix", boom)
    with pytest.raises(ri.RfpIngestTransient):
        ri.delete_run("r1")
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_DONE
    assert env.db.files("r1")                        # still deletable, so still reclaimable


# ── Email runs and materialization ───────────────────────────────────────

EMAIL = {
    "id": "e1", "mailbox": "pm@g3.test", "graph_message_id": "MSG", "subject": "RFP: Job",
    "from_name": "GC", "from_address": "gc@example.com", "message_at": NOW.isoformat(),
    "status": "processed", "has_attachments": True, "project_id": None,
    "direction": "inbound", "created_at": NOW.isoformat(),
}


def _attachment(att_id, **over):
    row = {
        "id": att_id, "email_id": "e1", "graph_attachment_id": f"G-{att_id}",
        "filename": f"{att_id}.pdf", "mime_type": "application/pdf", "size_bytes": 1234,
        "storage_path": None, "skipped_reason": None, "created_at": NOW.isoformat(),
    }
    row.update(over)
    return row


def _email_env(monkeypatch, tmp_path, attachments, **over):
    env = _env(monkeypatch, tmp_path, {
        "ingested_emails": [EMAIL], "ingested_email_attachments": attachments,
    }, **over)
    run, files = ri.create_email_run(email_id="e1", created_by=USER)
    return env, run, files


def _graph(monkeypatch, handler):
    monkeypatch.setattr(graph_email, "_acquire_token", lambda: "tok")
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        graph_email, "_graph_client",
        lambda timeout: httpx.Client(transport=transport, follow_redirects=False),
    )


def test_create_email_run_is_pending_with_one_file_per_attachment(monkeypatch, tmp_path):
    env, run, files = _email_env(monkeypatch, tmp_path, [
        _attachment("a1", filename="Plans​.pdf"),
        _attachment("a2", skipped_reason="item_attachment", graph_attachment_id=None),
    ])
    assert run["status"] == protocol.RUN_PENDING and run["source_kind"] == "email"
    assert run["email_id"] == "e1" and run["file_count"] == 2
    assert [f["source_attachment_id"] for f in files] == ["a1", "a2"]
    assert files[0]["filename"] == "Plans.pdf"       # hidden code point dropped
    assert files[0]["quarantine_path"] is None and files[0]["size_bytes"] is None
    assert all(f["status"] == protocol.STATUS_PENDING for f in files)
    assert env.queue.enqueued == []                  # the ROUTER dispatches


def test_create_email_run_refusals(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, {"ingested_emails": [EMAIL], "ingested_email_attachments": []})
    with pytest.raises(ri.RfpIngestPermanent) as exc:
        ri.create_email_run(email_id="e1", created_by=USER)
    assert exc.value.http_status == 409
    with pytest.raises(ri.RfpIngestPermanent) as exc:
        ri.create_email_run(email_id="nope", created_by=USER)
    assert exc.value.http_status == 404
    _env(monkeypatch, tmp_path, {
        "ingested_emails": [EMAIL],
        "ingested_email_attachments": [_attachment("a1"), _attachment("a2")],
    }, rfp_ingest_max_files_per_run=1)
    with pytest.raises(ri.RfpIngestPermanent) as exc:
        ri.create_email_run(email_id="e1", created_by=USER)
    assert exc.value.http_status == 413


def test_email_attachment_streams_from_graph_and_lands_in_quarantine(monkeypatch, tmp_path):
    env, run, files = _email_env(monkeypatch, tmp_path, [_attachment("a1")])
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})

    _graph(monkeypatch, handler)
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(1))
    ri.execute(run["id"])
    frow = env.db.files(run["id"])[0]
    assert frow["status"] == protocol.STATUS_VERIFIED
    assert frow["quarantine_path"] == f"{run['id']}/{frow['id']}/source.pdf"
    assert frow["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert frow["size_bytes"] == len(PDF)
    assert len(requests) == 1
    assert requests[0].url.path == "/v1.0/users/pm@g3.test/messages/MSG/attachments/G-a1/$value"
    assert requests[0].headers["authorization"] == "Bearer tok"
    assert 'IdType="ImmutableId"' in requests[0].headers["prefer"]
    assert env.store.files[0] == (rs.QUARANTINE_BUCKET, frow["quarantine_path"], PDF,
                                  "application/pdf")
    source = frow["manifest"]["identity"]["source"]
    assert source == {
        "kind": "email", "email_id": "e1", "attachment_id": "a1", "graph_attachment_id": "G-a1",
        "mailbox": "pm@g3.test", "stored_copy": False,
    }
    assert env.db.row(ri.RUNS_TABLE, run["id"])["status"] == protocol.RUN_DONE


@pytest.mark.parametrize(
    ("status_code", "code"),
    [(404, protocol.REJECT_NOT_STORED), (410, protocol.REJECT_NOT_STORED)],
)
def test_email_attachment_gone_at_graph_is_not_stored(monkeypatch, tmp_path, status_code, code):
    env, run, files = _email_env(monkeypatch, tmp_path, [_attachment("a1")])
    _graph(monkeypatch, lambda request: httpx.Response(status_code, json={"error": "gone"}))
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: pytest.fail("nothing to run"))
    ri.execute(run["id"])
    frow = env.db.files(run["id"])[0]
    assert (frow["status"], frow["reject_code"]) == (protocol.STATUS_REJECTED, code)
    assert frow["error"] == protocol.VERDICT_MESSAGES[code]
    assert env.store.files == []
    assert env.db.row(ri.RUNS_TABLE, run["id"])["status"] == protocol.RUN_DONE


def test_email_attachment_over_the_cap_is_too_large(monkeypatch, tmp_path):
    env, run, files = _email_env(monkeypatch, tmp_path, [_attachment("a1")],
                                 rfp_ingest_max_file_bytes=100)
    _graph(monkeypatch, lambda request: httpx.Response(200, content=b"%PDF-1.4" + b"x" * 500))
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: pytest.fail("nothing to run"))
    ri.execute(run["id"])
    frow = env.db.files(run["id"])[0]
    assert (frow["status"], frow["reject_code"]) == (
        protocol.STATUS_REJECTED, protocol.REJECT_TOO_LARGE,
    )


def test_email_attachment_transport_failure_is_failed_storage(monkeypatch, tmp_path):
    env, run, files = _email_env(monkeypatch, tmp_path, [_attachment("a1")])

    def handler(request):
        raise httpx.ConnectError("graph unreachable")

    _graph(monkeypatch, handler)
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: pytest.fail("nothing to run"))
    ri.execute(run["id"])
    frow = env.db.files(run["id"])[0]
    assert (frow["status"], frow["reject_code"]) == (protocol.STATUS_FAILED, protocol.FAIL_STORAGE)
    assert env.db.row(ri.RUNS_TABLE, run["id"])["status"] == protocol.RUN_FAILED


def test_item_attachments_and_missing_graph_ids_never_touch_graph(monkeypatch, tmp_path):
    env, run, files = _email_env(monkeypatch, tmp_path, [
        _attachment("item", skipped_reason="item_attachment"),
        _attachment("noid", graph_attachment_id=None),
    ])
    _graph(monkeypatch, lambda request: pytest.fail("Graph must not be called"))
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: pytest.fail("nothing to run"))
    ri.execute(run["id"])
    item, noid = env.db.files(run["id"])
    assert item["reject_code"] == protocol.REJECT_ITEM_ATTACHMENT
    assert noid["reject_code"] == protocol.REJECT_NOT_STORED
    for frow in (item, noid):
        assert frow["status"] == protocol.STATUS_REJECTED
        _assert_verdict_text(frow)
        assert frow["manifest"]["identity"]["source"]["kind"] == "email"


def test_stored_attachment_copy_is_read_from_project_files(monkeypatch, tmp_path):
    env, run, files = _email_env(monkeypatch, tmp_path, [
        _attachment("a1", storage_path="emails/e1/abc-a1.pdf"),
    ])
    _graph(monkeypatch, lambda request: pytest.fail("the stored copy wins"))
    monkeypatch.setattr(storage, "download_file", lambda path: PDF if path.endswith("a1.pdf")
                        else pytest.fail(path))
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(1))
    ri.execute(run["id"])
    frow = env.db.files(run["id"])[0]
    assert frow["status"] == protocol.STATUS_VERIFIED
    assert frow["manifest"]["identity"]["source"]["stored_copy"] is True
    assert env.store.files[0][:2] == (rs.QUARANTINE_BUCKET, frow["quarantine_path"])


def test_deleted_attachment_row_is_not_stored(monkeypatch, tmp_path):
    env, run, files = _email_env(monkeypatch, tmp_path, [_attachment("a1")])
    env.db.tables["ingested_email_attachments"] = []
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: pytest.fail("nothing to run"))
    ri.execute(run["id"])
    assert env.db.files(run["id"])[0]["reject_code"] == protocol.REJECT_NOT_STORED


# ── Prune, reads, status, self-test ──────────────────────────────────────


def test_prune_expires_old_terminal_runs_only(monkeypatch, tmp_path):
    old = (NOW - timedelta(days=20)).isoformat()
    fresh = (NOW - timedelta(days=2)).isoformat()
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [
        _run_row("old-done", status=protocol.RUN_DONE, completed_at=old),
        _run_row("old-failed", status=protocol.RUN_FAILED, completed_at=old),
        _run_row("old-canceled", status=protocol.RUN_CANCELED, completed_at=old),
        _run_row("fresh-done", status=protocol.RUN_DONE, completed_at=fresh),
        _run_row("running", status=protocol.RUN_RUNNING, completed_at=old),
        _run_row("expired", status=protocol.RUN_EXPIRED, completed_at=old),
        _run_row("never", status=protocol.RUN_DONE, completed_at=None),
    ]})
    monkeypatch.setattr(ri, "_now", lambda: NOW)
    assert ri.prune_expired() == 3
    for run_id in ("old-done", "old-failed", "old-canceled"):
        assert env.db.row(ri.RUNS_TABLE, run_id)["status"] == protocol.RUN_EXPIRED
        assert (rs.DERIVED_BUCKET, run_id) in env.store.deleted
        assert (rs.QUARANTINE_BUCKET, run_id) in env.store.deleted
    for run_id, status in (("fresh-done", protocol.RUN_DONE), ("running", protocol.RUN_RUNNING),
                           ("never", protocol.RUN_DONE)):
        assert env.db.row(ri.RUNS_TABLE, run_id)["status"] == status
    assert env.bells == []                              # expiring is silent
    assert ri.prune_expired() == 0                      # idempotent


def test_prune_expires_the_run_before_it_deletes_the_bytes(monkeypatch, tmp_path):
    """A retry landing inside the sweep must not resurrect a run whose source
    objects are already gone: the expired CAS comes first, so the retry either
    wins (and the bytes are left alone) or loses (and the run is expired,
    which /retry refuses)."""
    old = (NOW - timedelta(days=20)).isoformat()
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row("a", status=protocol.RUN_FAILED, completed_at=old)],
        ri.FILES_TABLE: [_file_row("f", run_id="a", status=protocol.STATUS_FAILED,
                                   reject_code="storage")],
    })
    monkeypatch.setattr(ri, "_now", lambda: NOW)
    order = []

    def retry_mid_sweep(bucket, prefix):
        order.append((bucket, prefix, env.db.row(ri.RUNS_TABLE, "a")["status"]))
        if bucket == rs.DERIVED_BUCKET:
            with pytest.raises(ri.RfpIngestPermanent) as exc:
                ri.retry_run("a", created_by=USER, background=BackgroundTasks())
            assert exc.value.http_status == 409

    monkeypatch.setattr(rs, "delete_prefix", retry_mid_sweep)
    assert ri.prune_expired() == 1
    assert [status for _, _, status in order] == [protocol.RUN_EXPIRED] * 2
    assert env.db.row(ri.RUNS_TABLE, "a")["status"] == protocol.RUN_EXPIRED
    assert env.queue.enqueued == []
    # A retry that wins the race instead keeps every byte it needs.
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row("b", status=protocol.RUN_FAILED, completed_at=old)],
    })
    monkeypatch.setattr(ri, "_now", lambda: NOW)
    monkeypatch.setattr(ri, "_mark", lambda run_id, **kw: None)
    assert ri.prune_expired() == 0
    assert env.store.deleted == []


def test_prune_clears_the_paths_of_an_expired_run(monkeypatch, tmp_path):
    """The objects are gone, so the columns the readers mint signed URLs from
    go too: an expired run must not offer downloads that 404."""
    old = (NOW - timedelta(days=20)).isoformat()
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row("a", status=protocol.RUN_DONE, completed_at=old)],
        ri.FILES_TABLE: [_file_row("f", run_id="a", status=protocol.STATUS_VERIFIED,
                                   derived_prefix="a/f", text_path="a/f/text.json",
                                   images_pdf_paths=["a/f/images-001.pdf"])],
        ri.PAGES_TABLE: [{"id": "p0", "file_id": "f", "page_index": 0, "status": "ok",
                          "thumb_path": "a/f/thumb/0000.jpg", "full_path": "a/f/full/0000.jpg"}],
    })
    monkeypatch.setattr(ri, "_now", lambda: NOW)
    assert ri.prune_expired() == 1
    frow = env.db.row(ri.FILES_TABLE, "f")
    assert frow["derived_prefix"] is None and frow["text_path"] is None
    assert frow["images_pdf_paths"] is None
    assert ri.file_urls("f") == {"images_pdf": [], "text": None, "manifest": None}
    assert ri.list_pages("f", offset=0, limit=10)["rows"][0]["thumb_url"] is None
    assert ri.page_urls("p0") == {"full": None}
    assert env.store.signed == []                     # nothing was even minted


def test_prune_reclaims_abandoned_staging_runs(monkeypatch, tmp_path):
    """A staging run never gets a completed_at, so the terminal pass could
    never see it: without this pass a closed tab leaked its uploads forever.

    The swept run lands on `expired`, not `canceled`: canceled is a status
    /retry accepts, and its source objects are about to be deleted. The bell
    still fires once (on the canceled edge) and the sentence stays on the
    row."""
    stale = (NOW - timedelta(hours=72)).isoformat()
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [
            _run_row("old", status=protocol.RUN_STAGING, completed_at=None, updated_at=stale),
            _run_row("fresh", status=protocol.RUN_STAGING, completed_at=None,
                     updated_at=(NOW - timedelta(hours=1)).isoformat()),
        ],
        ri.FILES_TABLE: [_file_row("f", run_id="old")],
    })
    monkeypatch.setattr(ri, "_now", lambda: NOW)
    assert ri.prune_expired() == 0                    # nothing EXPIRED by the terminal pass:
    old = env.db.row(ri.RUNS_TABLE, "old")
    assert old["status"] == protocol.RUN_EXPIRED and old["completed_at"]
    assert old["error"] == ri._MSG_STAGING_ABANDONED
    assert [bell["type"] for bell in env.bells] == [ri.RUN_FINISHED_NOTIFICATION]
    # The pointer goes with the object it named.
    assert env.db.row(ri.FILES_TABLE, "f")["quarantine_path"] is None
    assert (rs.QUARANTINE_BUCKET, "old") in env.store.deleted
    assert env.db.row(ri.RUNS_TABLE, "fresh")["status"] == protocol.RUN_STAGING
    assert all(prefix != "fresh" for _, prefix in env.store.deleted)
    # A run started in the same second as the sweep is never canceled.
    env.db.row(ri.RUNS_TABLE, "fresh")["updated_at"] = stale
    started = []
    real_mark = ri._mark

    def start_first(run_id, *, status, **kw):
        if run_id == "fresh" and not started:
            started.append(1)
            env.db.row(ri.RUNS_TABLE, "fresh")["status"] = protocol.RUN_PENDING
        return real_mark(run_id, status=status, **kw)

    monkeypatch.setattr(ri, "_mark", start_first)
    ri.prune_expired()
    assert env.db.row(ri.RUNS_TABLE, "fresh")["status"] == protocol.RUN_PENDING


def test_a_swept_staging_run_refuses_the_retry_its_bytes_can_no_longer_serve(
    monkeypatch, tmp_path
):
    """Leaving the swept run `canceled` gave it a working Retry button
    forever: every file would come back rejected/not_stored, permanently,
    because the quarantine objects the retry needs were deleted by the same
    sweep."""
    stale = (NOW - timedelta(hours=72)).isoformat()
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [
            _run_row("old", status=protocol.RUN_STAGING, completed_at=None, updated_at=stale),
        ],
        ri.FILES_TABLE: [_file_row("f", run_id="old", status=protocol.STATUS_PENDING)],
    })
    monkeypatch.setattr(ri, "_now", lambda: NOW)
    ri.prune_expired()
    assert env.db.row(ri.RUNS_TABLE, "old")["status"] == protocol.RUN_EXPIRED
    with pytest.raises(ri.RfpIngestPermanent) as exc:
        ri.retry_run("old", created_by=USER, background=BackgroundTasks())
    assert exc.value.http_status == 409
    assert env.db.row(ri.RUNS_TABLE, "old")["status"] == protocol.RUN_EXPIRED
    assert env.queue.enqueued == []


def test_expiring_a_run_clears_its_page_paths_in_one_statement_per_chunk(monkeypatch, tmp_path):
    """One round trip per file row put a page of expiring runs into the tens
    of thousands of sequential statements, on the queue's worker thread and
    inside the hourly slot."""
    old = (NOW - timedelta(days=20)).isoformat()
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row("a", status=protocol.RUN_DONE, completed_at=old)],
        ri.FILES_TABLE: [
            _file_row(f"f{i}", run_id="a", status=protocol.STATUS_VERIFIED, seq=i,
                      derived_prefix=f"a/f{i}")
            for i in range(5)
        ],
        ri.PAGES_TABLE: [
            {"id": f"p{i}", "file_id": f"f{i}", "page_index": 0, "status": "ok",
             "thumb_path": f"a/f{i}/thumb/0000.jpg", "full_path": f"a/f{i}/full/0000.jpg"}
            for i in range(5)
        ],
    })
    monkeypatch.setattr(ri, "_now", lambda: NOW)
    page_updates = []
    real_table = env.db.table

    def counting_table(name):
        query = real_table(name)
        if name == ri.PAGES_TABLE:
            real_update = query.update

            def update(payload):
                page_updates.append(payload)
                return real_update(payload)

            query.update = update
        return query

    monkeypatch.setattr(env.db, "table", counting_table)
    assert ri.prune_expired() == 1
    assert len(page_updates) == 1                       # five files, one statement
    assert all(
        row["thumb_path"] is None and row["full_path"] is None
        for row in env.db.tables[ri.PAGES_TABLE]
    )
    # A run wider than one chunk pays one statement per chunk, never per file.
    page_updates.clear()
    env.db.row(ri.RUNS_TABLE, "a")["status"] = protocol.RUN_DONE
    monkeypatch.setattr(ri, "_PATH_CLEAR_BATCH", 2)
    assert ri.prune_expired() == 1
    assert len(page_updates) == 3


def test_the_scratch_sweep_refuses_a_tree_with_more_entries_than_it_will_walk(
    monkeypatch, tmp_path, caplog
):
    """An orphaned tree is by definition one whose quota mirror died with the
    parent that owned it, so nothing bounded its entry count. Walking it (or
    handing it to shutil.rmtree, which lists each directory in full) is the
    memory peak the sweep exists to avoid, so it is logged and left."""
    env = _env(monkeypatch, tmp_path)
    root = ri._scratch_root(env.settings)
    old = time.time() - 12 * 3600
    bomb = root / "rfp-sandbox-bomb"
    (bomb / "out").mkdir(parents=True)
    for i in range(8):
        (bomb / "out" / f"{i:04d}.jpg").write_bytes(b"\xff\xd8x\xff\xd9")
    ordinary = root / "rfp-ingest-dead"
    ordinary.mkdir()
    (ordinary / "source.pdf").write_bytes(b"%PDF-1.4 orphan")
    for path in (bomb, ordinary):
        for entry in [path] + list(path.rglob("*")):
            os.utime(entry, (old, old))
    monkeypatch.setattr(ri, "_sweep_entry_budget", lambda s: 3)
    with caplog.at_level(logging.ERROR, logger="app.services.rfp_ingest"):
        assert ri._sweep_scratch(env.settings) == 1     # only the ordinary one
    assert bomb.exists() and not ordinary.exists()
    assert "rfp-sandbox-bomb" in caplog.text


def test_tree_stats_stops_descending_at_the_depth_bound(monkeypatch, tmp_path):
    """The depth bound mirrors the runner's out-dir walk: a deep tree is
    measured as far as the bound and never recursed into forever."""
    old = time.time() - 12 * 3600
    deep = tmp_path / "rfp-sandbox-deep"
    current = deep
    for level in range(ri._MAX_TREE_DEPTH + 4):
        current = current / f"d{level}"
    current.mkdir(parents=True)
    (current / "buried.bin").write_bytes(b"x" * 5000)
    for entry in [deep] + list(deep.rglob("*")):
        os.utime(entry, (old, old))
    newest, total, overflowed = ri._tree_stats(deep, 10_000)
    assert not overflowed
    assert total < 5000                                 # the buried file is out of reach
    assert abs(newest - old) < 5                        # the mtime is still plausible


def test_prune_sweeps_orphaned_scratch_directories(monkeypatch, tmp_path):
    """One hard worker death (an OOM kill skips every finally) used to wedge
    the feature until the next redeploy: uvicorn respawns inside the SAME
    container, so the orphan stays on the disk _check_disk measures."""
    env = _env(monkeypatch, tmp_path)
    root = ri._scratch_root(env.settings)
    old = time.time() - 12 * 3600
    orphan = root / "rfp-ingest-dead"
    (orphan / "images").mkdir(parents=True)
    (orphan / "source.pdf").write_bytes(b"%PDF-1.4 orphan")
    sandbox = root / "rfp-sandbox-dead"
    (sandbox / "out").mkdir(parents=True)
    (sandbox / "out" / "0000.jpg").write_bytes(b"\xff\xd8orphan\xff\xd9")
    live = root / "rfp-ingest-live"
    (live / "out").mkdir(parents=True)
    (live / "out" / "0000.jpg").write_bytes(b"\xff\xd8live\xff\xd9")
    lock = root / runner.SLOT_LOCK_TEMPLATE.format(slot=0)
    lock.write_bytes(b"")
    keep = root / "not-ours"
    keep.mkdir()
    for path in (orphan, sandbox, keep, lock):
        for entry in ([path] + list(path.rglob("*")) if path.is_dir() else [path]):
            os.utime(entry, (old, old))
    os.utime(live, (old, old))                        # the work dir root goes stale...
    assert ri._sweep_scratch(env.settings) == 2
    assert not orphan.exists() and not sandbox.exists()
    assert live.exists()                              # ...but out/ is fresh, so it stays
    assert lock.exists()                              # the uid lock is a FILE, never swept
    assert keep.exists()                              # foreign names are left alone
    # The hourly prune slot is what drives it.
    os.utime(live / "out" / "0000.jpg", (old, old))
    os.utime(live / "out", (old, old))
    monkeypatch.setattr(ri, "_now", lambda: NOW)
    ri.prune_expired()
    assert not live.exists()


def test_reads_for_the_router(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row("r1", status=protocol.RUN_DONE),
                        _run_row("r2", status=protocol.RUN_PENDING,
                                 created_at=(NOW + timedelta(minutes=1)).isoformat())],
        ri.FILES_TABLE: [_file_row("f1", status=protocol.STATUS_VERIFIED, manifest={"big": 1},
                                   hazards={"uri_links": 2}, derived_prefix="r1/f1",
                                   text_path="r1/f1/text.json",
                                   images_pdf_paths=["r1/f1/images-001.pdf"],
                                   filename="Spec Book (Rev 2).pdf")],
        ri.PAGES_TABLE: [
            {"id": "p0", "file_id": "f1", "page_index": 0, "status": "ok",
             "thumb_path": "r1/f1/thumb/0000.jpg", "full_path": "r1/f1/full/0000.jpg"},
            {"id": "p1", "file_id": "f1", "page_index": 1, "status": "failed",
             "thumb_path": None, "full_path": None, "code": "verify"},
        ],
    })
    monkeypatch.setattr(llm_queue, "poll_info", lambda jt, t: {"state": "queued"})
    run = ri.get_run("r1")
    assert run["queue"] == {"state": "queued"}
    assert [f["id"] for f in run["files"]] == ["f1"]
    assert "manifest" not in run["files"][0] or run["files"][0].get("manifest") == {"big": 1}
    assert ri.get_run("missing") is None
    page = ri.list_runs(status=None, limit=1, offset=0)
    assert page["total"] == 2 and [r["id"] for r in page["rows"]] == ["r2"]
    assert ri.list_runs(status=protocol.RUN_DONE, limit=10, offset=0)["rows"][0]["id"] == "r1"
    with pytest.raises(ri.RfpIngestPermanent) as exc:
        ri.list_runs(status="bogus", limit=10, offset=0)
    assert exc.value.http_status == 400
    assert ri.get_file("f1")["manifest"] == {"big": 1}
    pages = ri.list_pages("f1", offset=0, limit=10)
    assert pages["total"] == 2
    assert pages["rows"][0]["thumb_url"] == "https://signed.test/rfp-derived/r1/f1/thumb/0000.jpg"
    assert pages["rows"][1]["thumb_url"] is None
    assert ri.page_urls("p0") == {"full": "https://signed.test/rfp-derived/r1/f1/full/0000.jpg"}
    assert ri.page_urls("p1") == {"full": None}
    assert ri.page_urls("nope") is None
    urls = ri.file_urls("f1")
    assert urls["images_pdf"] == ["https://signed.test/rfp-derived/r1/f1/images-001.pdf"]
    assert urls["text"].endswith("/r1/f1/text.json")
    assert urls["manifest"].endswith("/r1/f1/manifest.json")
    downloads = {d for _, _, d in env.store.signed if d}
    assert downloads == {"Spec_Book_Rev_2-images-001.pdf", "Spec_Book_Rev_2-text.json",
                         "Spec_Book_Rev_2-manifest.json"}
    assert ri.file_urls("nope") is None


def test_images_pdf_urls_stay_positional_when_one_part_cannot_be_signed(monkeypatch, tmp_path):
    """`images_pdf` is read by index against `images_pdf_paths`, and the page
    numbers its download buttons from that list. Dropping a part that could
    not be minted would shift every later part under the wrong number and hand
    the operator a different document than the one they clicked, so the entry
    stays in place as null."""
    env = _env(monkeypatch, tmp_path, {
        ri.FILES_TABLE: [_file_row("f1", status=protocol.STATUS_VERIFIED,
                                   derived_prefix="r1/f1",
                                   images_pdf_paths=["r1/f1/images-001.pdf",
                                                     "r1/f1/images-002.pdf",
                                                     "r1/f1/images-003.pdf"],
                                   filename="Drawings.pdf")],
    })
    real_signed = env.store.signed_url

    def flaky(bucket, path, *, download=None):
        if path.endswith("images-002.pdf"):
            raise rs.RfpStorageError("mint failed")
        return real_signed(bucket, path, download=download)

    monkeypatch.setattr(rs, "signed_url", flaky)
    urls = ri.file_urls("f1")
    assert urls["images_pdf"] == [
        "https://signed.test/rfp-derived/r1/f1/images-001.pdf",
        None,
        "https://signed.test/rfp-derived/r1/f1/images-003.pdf",
    ]


def test_recent_emails_lists_inbound_with_attachment_counts(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, {
        "ingested_emails": [
            EMAIL,
            {**EMAIL, "id": "e2", "subject": "Addendum 3", "direction": "inbound",
             "message_at": (NOW + timedelta(hours=1)).isoformat()},
            {**EMAIL, "id": "out", "subject": "RFP reply", "direction": "outbound"},
        ],
        "ingested_email_attachments": [
            _attachment("a1"), _attachment("a2", storage_path="emails/e1/x.pdf"),
            _attachment("a3", email_id="e2", skipped_reason="too_large"),
        ],
    })
    rows = ri.recent_emails(q=None, limit=10)
    assert [r["id"] for r in rows] == ["e2", "e1"]
    assert (rows[1]["attachment_count"], rows[1]["stored_count"]) == (2, 1)
    assert (rows[0]["attachment_count"], rows[0]["stored_count"]) == (1, 0)
    assert [r["id"] for r in ri.recent_emails(q="ddend", limit=10)] == ["e2"]
    assert ri.recent_emails(q="(%)", limit=10) == rows          # sanitized to nothing: no filter
    assert ri.recent_emails(q="RFP", limit=10)[0]["id"] == "e1"
    assert ri._sanitize_query("a,b(c)%d_e*f\\g") == re.sub(r"[,()%_*\\]", " ", "a,b(c)%d_e*f\\g")


def test_self_test_caches_its_verdict_for_execute(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path)
    page = _page_ok(0, text="RFP ingestion sandbox self-test")
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _result([page]))
    out = ri.self_test()
    assert out["ok"] is True and out["at"] and out["uid_switch_applied"] is False
    assert ri.last_self_test()["ok"] is True
    call = env.sandbox_calls[0]
    assert call["pdf_bytes"] == PDF and call["max_pages_remaining"] == 1
    assert call["uid_slot"] is None and call["file_timeout_seconds"] == 120
    assert list((tmp_path / "scratch").glob("rfp-selftest-*")) == []
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _result(
        [], status=runner.STATUS_FAILED, code=protocol.FAIL_SPAWN))
    out = ri.self_test()
    assert out["ok"] is False
    assert out["detail"] == (
        "The sandbox self-test ended failed/spawn: "
        + protocol.VERDICT_MESSAGES[protocol.FAIL_SPAWN]
    )
    assert ri._last_self_test["ok"] is False

    def boom(i, pdf, kw):
        raise OSError("no fork")

    _script_sandbox(monkeypatch, env, boom)
    out = ri.self_test()
    assert out["ok"] is False and out["detail"] == ri._MSG_SELF_TEST_ERROR


def test_self_test_uses_the_dedicated_slot_when_switching(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "_geteuid", lambda: 0)
    page = _page_ok(0, text="self-test")
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _result([page], uid=kw["uid_slot"].uid))
    out = ri.self_test()
    assert out["ok"] is True and out["uid"] == 60100 + 4
    slot = env.sandbox_calls[0]["uid_slot"]
    assert (slot.uid, slot.gid, slot.slot) == (60104, 60104, 4)
    assert (tmp_path / "scratch" / "rfp-ingest-slot-4.lock").exists()
    # The lock was released: the slot reads free afterwards.
    occupancy = runner.slot_occupancy(tmp_path / "scratch", pool_base=60100, pool_size=5)
    assert occupancy[4] == {"slot": 4, "uid": 60104, "busy": False}


def test_status_report_runs_the_self_test_and_reports_slots(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path)
    page = _page_ok(0, text="self-test")
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _result([page]))
    report = ri.status_report()
    assert report["enabled"] is True
    assert report["limits"]["thumb_long_side"] == 1568 and report["limits_hash"]
    assert report["versions"]["sandbox_version"] == protocol.SANDBOX_VERSION
    assert report["versions"]["pypdfium2"]
    assert report["platform"]["uid_switch"] is False and report["platform"]["notes"]
    assert report["platform"]["self_test_uid"] == 60104
    assert report["scratch"]["free_bytes"] > 0 and report["scratch"]["root"]
    assert [s["slot"] for s in report["slots"]] == [0, 1, 2, 3]
    # The self-test's own slot is reported apart from the pool: a busy one is
    # why a self-test can come back unknown, and the pid says which worker
    # answered (each caches its own verdict).
    assert report["platform"]["self_test_slot"] == {"slot": 4, "uid": 60104, "busy": False}
    assert report["platform"]["pid"] == os.getpid()
    assert report["concurrency"] == {"sandbox_concurrency": 1, "active_runs": 0}
    assert report["caps"]["file_timeout_seconds"] == 4800
    assert report["self_test"]["ok"] is True


def test_stale_running_file_without_a_token_is_reset_too(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))
    env.db.row(ri.FILES_TABLE, "f1").update(status=protocol.STATUS_RUNNING, claim_token=None)
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(1))
    ri.execute("r1")
    assert env.db.row(ri.FILES_TABLE, "f1")["status"] == protocol.STATUS_VERIFIED
    assert env.store.deleted[0] == (rs.DERIVED_BUCKET, "r1/f1")


# ── Office files (spec 2.1) ──────────────────────────────────────────────

def _tiny_ooxml() -> bytes:
    """A real (minimal) OOXML container: the converter's pre-conversion
    scan opens the zip, so the bytes have to be one."""
    import io as _io
    import zipfile as _zipfile

    buf = _io.BytesIO()
    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("_rels/.rels", "<Relationships/>")
        zf.writestr("word/document.xml", "<w:document/>" + " " * 200)
    return buf.getvalue()


OOXML = _tiny_ooxml()
OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _gotenberg(monkeypatch, env: _Env, handler):
    """Put the converter on a MockTransport and record every request's
    part name and body on `env.conversions`."""
    env.conversions = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        ctype = request.headers.get("content-type", "")
        boundary = ctype.split("boundary=", 1)[1].encode() if "boundary=" in ctype else b""
        body = request.read()
        env.conversions.append({"url": str(request.url), "body": body, "boundary": boundary})
        return handler(request)

    monkeypatch.setattr(office, "_transport", httpx.MockTransport(wrapped))


def _office_run(monkeypatch, tmp_path, *, fmt="docx", raw=OOXML, files=("f1",), **over) -> _Env:
    """An upload run whose files are office documents already in quarantine
    (as add_upload_file leaves them): the row carries the format, the
    quarantine object has the format's extension, and the fake bucket
    answers the raw bytes for it."""
    rows = [
        _file_row(
            fid, seq=i, filename=f"{fid}.{fmt}", declared_mime=None, source_format=fmt,
            converted_path=None, size_bytes=len(raw),
            quarantine_path=f"r1/{fid}/source.{fmt}",
        )
        for i, fid in enumerate(files)
    ]
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row(file_count=len(files))], ri.FILES_TABLE: rows,
    }, **over)
    for row in rows:
        env.store.objects[row["quarantine_path"]] = raw
    return env


def test_add_upload_file_accepts_a_docx_and_records_its_format(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row(status=protocol.RUN_STAGING)]})
    row = ri.add_upload_file("r1", filename="Bid Form.docx", declared_mime=DOCX_MIME, data=OOXML)
    assert row["status"] == protocol.STATUS_PENDING
    assert row["source_format"] == protocol.SOURCE_FORMAT_DOCX
    assert row.get("converted_path") is None
    assert row["sha256"] == hashlib.sha256(OOXML).hexdigest()
    # The quarantine object takes the sniffed format's extension and type.
    assert row["quarantine_path"] == f"r1/{row['id']}/source.docx"
    assert env.store.uploads == [(rs.QUARANTINE_BUCKET, row["quarantine_path"], OOXML, DOCX_MIME)]
    assert row["manifest"]["identity"]["source_format"] == "docx"
    assert row["manifest"]["sniff"]["verdict"] is None
    assert row["manifest"]["sniff"]["source_format"] == "docx"
    assert row["manifest"]["sniff"]["pdf_header_offset"] is None
    # The harvest path is the same function with a source pointer.
    xls = ri.add_upload_file(
        "r1", filename="takeoff.xls", declared_mime=None, data=OLE2,
        source={"kind": "procore", "file_path": "Bid Docs/takeoff.xls"},
    )
    assert xls["source_format"] == protocol.SOURCE_FORMAT_XLS
    assert xls["quarantine_path"].endswith("/source.xls")
    assert env.store.uploads[1][3] == "application/vnd.ms-excel"
    assert xls["manifest"]["identity"]["source"]["kind"] == "procore"
    # A PDF is exactly what it was.
    pdf = ri.add_upload_file("r1", filename="spec.pdf", declared_mime=None, data=PDF)
    assert pdf["source_format"] == "pdf" and pdf["quarantine_path"].endswith("/source.pdf")
    assert env.store.uploads[2][3] == "application/pdf"


def test_add_upload_file_office_extension_over_pdf_bytes_is_a_pdf(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row(status=protocol.RUN_STAGING)]})
    row = ri.add_upload_file("r1", filename="renamed.docx", declared_mime=DOCX_MIME, data=PDF)
    assert row["status"] == protocol.STATUS_PENDING and row["source_format"] == "pdf"
    assert row["quarantine_path"].endswith("/source.pdf")
    assert env.store.uploads[0][3] == "application/pdf"


def test_add_upload_file_zip_named_pdf_and_unknown_office_names_stay_not_pdf(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row(status=protocol.RUN_STAGING)]})
    for name, data in (("Bid Form.pdf", OOXML), ("deck.pptx", OOXML), ("old.doc", OOXML)):
        row = ri.add_upload_file("r1", filename=name, declared_mime=None, data=data + name.encode())
        assert row["status"] == protocol.STATUS_REJECTED, name
        assert row["reject_code"] == protocol.REJECT_NOT_PDF
        assert row["error"] == "The file is not a PDF, Word or Excel file."
        assert row["source_format"] == "pdf"        # the column default: no format was established
        assert row["quarantine_path"] is None
    assert env.store.uploads == []


def test_add_upload_file_with_office_files_off_rejects_them_as_before(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {ri.RUNS_TABLE: [_run_row(status=protocol.RUN_STAGING)]},
               rfp_ingest_office_files_enabled=False)
    row = ri.add_upload_file("r1", filename="Bid Form.docx", declared_mime=DOCX_MIME, data=OOXML)
    assert row["status"] == protocol.STATUS_REJECTED
    assert row["reject_code"] == protocol.REJECT_NOT_PDF
    assert row["error"] == protocol.VERDICT_MESSAGES[protocol.REJECT_NOT_PDF]
    assert row["manifest"]["sniff"]["source_format"] is None
    assert env.store.uploads == []


def test_an_office_file_is_converted_uploaded_and_verified_through_the_pdf(monkeypatch, tmp_path):
    env = _office_run(monkeypatch, tmp_path)
    _gotenberg(monkeypatch, env, lambda r: httpx.Response(200, content=PDF))
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(2))
    ri.execute("r1")
    frow = env.db.files("r1")[0]
    assert frow["status"] == protocol.STATUS_VERIFIED
    assert frow["source_format"] == "docx"
    assert frow["converted_path"] == f"r1/{frow['id']}/converted.pdf"
    # Identity is the RAW office file (what sits in quarantine).
    assert frow["sha256"] == hashlib.sha256(OOXML).hexdigest()
    assert frow["size_bytes"] == len(OOXML)
    manifest = frow["manifest"]
    assert manifest["identity"]["source_format"] == "docx"
    assert manifest["sniff"]["source_format"] == "docx" and manifest["sniff"]["verdict"] is None
    conv = manifest["conversion"]
    assert conv["engine"] == "gotenberg" and conv["route"] == "/forms/libreoffice/convert"
    assert conv["source_format"] == "docx" and conv["reused"] is False
    assert conv["pdf_sha256"] == hashlib.sha256(PDF).hexdigest()
    assert conv["pdf_bytes"] == len(PDF) and conv["http_status"] == 200
    assert isinstance(conv["duration_ms"], int)
    assert conv["converted_path"] == frow["converted_path"]
    # The converter's answer got the parent's own PDF sniff before the child.
    assert conv["pdf_sniff"]["verdict"] is None and conv["pdf_sniff"]["source_format"] == "pdf"
    assert conv["pdf_sniff"]["pdf_header_offset"] == 0
    # One request, the part named by the format, the raw bytes inside it.
    assert len(env.conversions) == 1
    assert env.conversions[0]["url"] == f"{env.settings.gotenberg_base_url}/forms/libreoffice/convert"
    assert b'filename="source.docx"' in env.conversions[0]["body"]
    assert OOXML in env.conversions[0]["body"]
    # converted.pdf went to the derived bucket, and the child got the PDF.
    assert (rs.DERIVED_BUCKET, frow["converted_path"], PDF, "application/pdf") in env.store.files
    assert env.sandbox_calls[0]["pdf_bytes"] == PDF
    assert env.sandbox_calls[0]["pdf_path"].name == "source.pdf"
    # The raw file was never uploaded anywhere else and nothing else changed
    # about the outputs of a verified file.
    assert frow["derived_prefix"] == f"r1/{frow['id']}"
    assert frow["text_path"] == f"r1/{frow['id']}/text.json"
    assert env.db.row(ri.RUNS_TABLE, "r1")["status"] == protocol.RUN_DONE
    assert frow["phase"] is None


def test_a_pdf_file_never_touches_the_converter(monkeypatch, tmp_path):
    env = _upload_run(monkeypatch, tmp_path, files=("f1",))
    _gotenberg(monkeypatch, env, lambda r: pytest.fail("a PDF must not be converted"))
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(1))
    ri.execute("r1")
    frow = env.db.files("r1")[0]
    assert frow["status"] == protocol.STATUS_VERIFIED and frow["source_format"] == "pdf"
    assert "conversion" not in frow["manifest"] and frow["converted_path"] is None
    assert not any(p.endswith("converted.pdf") for p in env.store.all_paths())


@pytest.mark.parametrize(
    "answer, reason",
    [
        (lambda r: httpx.Response(503, content=b"busy"), "server_error"),
        (lambda r: httpx.Response(500, content=b"boom"), "server_error"),
        (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r)), "timeout"),
        (lambda r: (_ for _ in ()).throw(httpx.ConnectError("down", request=r)), "unreachable"),
    ],
)
def test_converter_5xx_timeout_or_outage_is_failed_conversion_unavailable(
    monkeypatch, tmp_path, answer, reason
):
    env = _office_run(monkeypatch, tmp_path)
    _gotenberg(monkeypatch, env, answer)
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: pytest.fail("no PDF, no child"))
    ri.execute("r1")
    frow = env.db.files("r1")[0]
    assert frow["status"] == protocol.STATUS_FAILED
    assert frow["reject_code"] == protocol.FAIL_CONVERSION_UNAVAILABLE
    assert frow["error"] == "The PDF converter was unavailable; retry the run."
    _assert_verdict_text(frow)
    assert frow["converted_path"] is None
    conv = frow["manifest"]["conversion"]
    assert conv["reason"] == reason and conv["engine"] == "gotenberg"
    assert "pdf_sha256" not in conv
    assert env.store.files == [] and env.store.many == []
    run = env.db.row(ri.RUNS_TABLE, "r1")
    # A retryable failure: the run is failed (its only file failed) and
    # /retry will bring the file back as pending.
    assert run["status"] == protocol.RUN_FAILED and run["files_failed"] == 1
    assert protocol.FAIL_CONVERSION_UNAVAILABLE in protocol.FAIL_CODES


@pytest.mark.parametrize("status_code", [400, 415, 422])
def test_converter_4xx_is_rejected_conversion_rejected(monkeypatch, tmp_path, status_code):
    env = _office_run(monkeypatch, tmp_path, fmt="xls", raw=OLE2)
    _gotenberg(monkeypatch, env, lambda r: httpx.Response(status_code, content=b"no"))
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: pytest.fail("no PDF, no child"))
    ri.execute("r1")
    frow = env.db.files("r1")[0]
    assert frow["status"] == protocol.STATUS_REJECTED
    assert frow["reject_code"] == protocol.REJECT_CONVERSION_REJECTED
    assert frow["error"] == "The file could not be converted to PDF."
    assert frow["manifest"]["conversion"]["http_status"] == status_code
    assert frow["manifest"]["conversion"]["reason"] == "refused"
    assert b'filename="source.xls"' in env.conversions[0]["body"]
    assert frow["converted_path"] is None and env.store.files == []
    run = env.db.row(ri.RUNS_TABLE, "r1")
    assert run["status"] == protocol.RUN_DONE and run["files_rejected"] == 1
    assert protocol.REJECT_CONVERSION_REJECTED in protocol.REJECT_CODES


def test_a_converter_answer_that_is_not_a_pdf_is_rejected_by_the_parent_sniff(monkeypatch, tmp_path):
    env = _office_run(monkeypatch, tmp_path)
    _gotenberg(monkeypatch, env, lambda r: httpx.Response(200, content=b"<html>error page</html>"))
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: pytest.fail("no PDF, no child"))
    ri.execute("r1")
    frow = env.db.files("r1")[0]
    assert frow["status"] == protocol.STATUS_REJECTED
    assert frow["reject_code"] == protocol.REJECT_CONVERSION_REJECTED
    conv = frow["manifest"]["conversion"]
    assert conv["http_status"] == 200 and conv["pdf_sniff"]["verdict"] == protocol.REJECT_NOT_PDF
    # Nothing that failed the sniff is ever uploaded as converted.pdf.
    assert env.store.files == [] and frow["converted_path"] is None


def test_a_converted_pdf_over_the_cap_is_rejected_too_large(monkeypatch, tmp_path):
    env = _office_run(monkeypatch, tmp_path, rfp_ingest_max_file_bytes=len(OOXML) + 64)
    big = b"%PDF-1.4\n" + b"x" * (len(OOXML) + 64) + b"\n%%EOF\n"
    _gotenberg(monkeypatch, env, lambda r: httpx.Response(200, content=big))
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: pytest.fail("no PDF, no child"))
    ri.execute("r1")
    frow = env.db.files("r1")[0]
    assert frow["status"] == protocol.STATUS_REJECTED
    assert frow["reject_code"] == protocol.REJECT_TOO_LARGE
    assert frow["error"] == protocol.VERDICT_MESSAGES[protocol.REJECT_TOO_LARGE]
    assert frow["manifest"]["conversion"]["reason"] == "too_large"
    assert env.store.files == [] and frow["converted_path"] is None


def _prior_office_row(fid, sha, nbytes, **over):
    return _file_row(
        fid, filename=f"{fid}.docx", declared_mime=None, source_format="docx",
        size_bytes=len(OOXML), quarantine_path=f"r1/{fid}/source.docx",
        converted_path=f"r1/{fid}/converted.pdf",
        manifest={"identity": {"source_format": "docx"},
                  "conversion": {"engine": "gotenberg", "route": "/forms/libreoffice/convert",
                                 "source_format": "docx", "duration_ms": 1234,
                                 "http_status": 200, "pdf_sha256": sha, "pdf_bytes": nbytes,
                                 "reused": False}},
        **over,
    )


def test_a_rerun_reuses_converted_pdf_when_its_digest_matches_the_manifest(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row(file_count=1)],
        ri.FILES_TABLE: [_prior_office_row("f1", hashlib.sha256(PDF).hexdigest(), len(PDF))],
    })
    env.store.objects["r1/f1/source.docx"] = OOXML
    env.store.objects["r1/f1/converted.pdf"] = PDF
    _gotenberg(monkeypatch, env, lambda r: pytest.fail("the earlier conversion must be reused"))
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(1))
    ri.execute("r1")
    frow = env.db.row(ri.FILES_TABLE, "f1")
    assert frow["status"] == protocol.STATUS_VERIFIED
    conv = frow["manifest"]["conversion"]
    assert conv["reused"] is True and conv["pdf_sha256"] == hashlib.sha256(PDF).hexdigest()
    assert conv["duration_ms"] == 1234 and conv["converted_path"] == "r1/f1/converted.pdf"
    assert conv["pdf_sniff"]["verdict"] is None
    assert env.conversions == []
    # Nothing re-uploaded converted.pdf; the child got the reused PDF.
    assert not any(path.endswith("converted.pdf") for _, path, *_ in env.store.files)
    assert env.sandbox_calls[0]["pdf_bytes"] == PDF
    assert frow["converted_path"] == "r1/f1/converted.pdf"


@pytest.mark.parametrize("what", ["digest_mismatch", "object_gone", "no_prior_manifest"])
def test_a_rerun_reconverts_when_converted_pdf_cannot_be_trusted(monkeypatch, tmp_path, what):
    sha = hashlib.sha256(PDF).hexdigest()
    row = _prior_office_row("f1", sha if what != "digest_mismatch" else "0" * 64, len(PDF))
    if what == "no_prior_manifest":
        row["manifest"] = None
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row(file_count=1)], ri.FILES_TABLE: [row],
    })
    env.store.objects["r1/f1/source.docx"] = OOXML
    env.store.objects["r1/f1/converted.pdf"] = (
        rs.RfpStorageNotFound("gone") if what == "object_gone" else PDF
    )
    fresh = PDF + b"%fresh\n"
    _gotenberg(monkeypatch, env, lambda r: httpx.Response(200, content=fresh))
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(1))
    ri.execute("r1")
    frow = env.db.row(ri.FILES_TABLE, "f1")
    assert frow["status"] == protocol.STATUS_VERIFIED
    conv = frow["manifest"]["conversion"]
    assert conv["reused"] is False and conv["pdf_sha256"] == hashlib.sha256(fresh).hexdigest()
    assert len(env.conversions) == 1
    assert (rs.DERIVED_BUCKET, "r1/f1/converted.pdf", fresh, "application/pdf") in env.store.files
    assert env.sandbox_calls[0]["pdf_bytes"] == fresh


def test_the_rerun_cleanups_spare_converted_pdf_and_the_prune_does_not(monkeypatch, tmp_path):
    # retry_run and the stale-running reset call the per-file cleanup, which
    # keeps converted.pdf; the retention prune sweeps the run prefix whole
    # and clears converted_path with the other path columns.
    old = (NOW - timedelta(days=20)).isoformat()
    env = _env(monkeypatch, tmp_path, {
        ri.RUNS_TABLE: [_run_row("r1", status=protocol.RUN_FAILED, completed_at=NOW.isoformat()),
                        _run_row("a", status=protocol.RUN_DONE, completed_at=old)],
        ri.FILES_TABLE: [
            _prior_office_row("f1", "x" * 64, 10, status=protocol.STATUS_FAILED,
                              reject_code=protocol.FAIL_STORAGE, error="y"),
            _file_row("g", run_id="a", status=protocol.STATUS_VERIFIED, derived_prefix="a/g",
                      converted_path="a/g/converted.pdf", source_format="docx"),
        ],
    })
    monkeypatch.setattr(ri, "_now", lambda: NOW)
    ri.retry_run("r1", created_by=USER, background=BackgroundTasks())
    frow = env.db.row(ri.FILES_TABLE, "f1")
    assert frow["status"] == protocol.STATUS_PENDING
    assert frow["converted_path"] == "r1/f1/converted.pdf"     # survives the reset
    assert env.store.deleted == [(rs.DERIVED_BUCKET, "r1/f1")]
    assert env.store.kept == [(rs.CONVERTED_OBJECT,)]
    assert ri.prune_expired() == 1
    g = env.db.row(ri.FILES_TABLE, "g")
    assert g["converted_path"] is None and g["derived_prefix"] is None
    assert (rs.DERIVED_BUCKET, "a") in env.store.deleted
    assert env.store.kept[-1] == ()                               # the run prefix, nothing spared


def test_a_verdict_after_a_delivery_spares_converted_pdf_too(monkeypatch, tmp_path):
    env = _office_run(monkeypatch, tmp_path)
    _gotenberg(monkeypatch, env, lambda r: httpx.Response(200, content=PDF))
    # The runner refuses the file after delivering pages (a digest that
    # changed between validation and delivery): the derived prefix is
    # discarded, converted.pdf excepted.
    def deliver_then_refuse(i, pdf, kw):
        result = _complete(40)
        for page in sorted(result.pages, key=lambda p: p.index):
            thumb, full = page.thumb, page.full
            page.thumb, page.full = None, None
            kw["page_sink"](page, thumb, full)       # one full batch goes up
        return _result(result.pages, status=runner.STATUS_FAILED,
                       code=protocol.FAIL_INVALID_OUTPUT)

    _script_sandbox(monkeypatch, env, deliver_then_refuse)
    ri.execute("r1")
    frow = env.db.files("r1")[0]
    assert len(env.store.many) == 1
    assert frow["reject_code"] == protocol.FAIL_INVALID_OUTPUT
    assert frow["converted_path"] == f"r1/{frow['id']}/converted.pdf"
    assert env.store.deleted == [(rs.DERIVED_BUCKET, f"r1/{frow['id']}")]
    assert env.store.kept == [(rs.CONVERTED_OBJECT,)]


def test_an_office_email_attachment_is_quarantined_under_its_extension_and_converted(
    monkeypatch, tmp_path
):
    env, run, files = _email_env(monkeypatch, tmp_path, [
        _attachment("a1", filename="Bid Form.docx", mime_type=DOCX_MIME,
                    storage_path="emails/e1/abc-a1.docx"),
    ])
    _graph(monkeypatch, lambda request: pytest.fail("the stored copy wins"))
    monkeypatch.setattr(storage, "download_file", lambda path: OOXML)
    _gotenberg(monkeypatch, env, lambda r: httpx.Response(200, content=PDF))
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: _complete(1))
    ri.execute(run["id"])
    frow = env.db.files(run["id"])[0]
    assert frow["status"] == protocol.STATUS_VERIFIED
    assert frow["source_format"] == "docx"
    assert frow["quarantine_path"] == f"{run['id']}/{frow['id']}/source.docx"
    assert frow["sha256"] == hashlib.sha256(OOXML).hexdigest()
    quarantine = [f for f in env.store.files if f[0] == rs.QUARANTINE_BUCKET]
    assert quarantine == [(rs.QUARANTINE_BUCKET, frow["quarantine_path"], OOXML, DOCX_MIME)]
    assert frow["manifest"]["conversion"]["reused"] is False
    assert frow["converted_path"] == f"{run['id']}/{frow['id']}/converted.pdf"
    assert env.sandbox_calls[0]["pdf_bytes"] == PDF


def test_an_office_attachment_with_office_files_off_is_not_pdf(monkeypatch, tmp_path):
    env, run, files = _email_env(monkeypatch, tmp_path, [
        _attachment("a1", filename="Bid Form.docx", storage_path="emails/e1/abc-a1.docx"),
    ], rfp_ingest_office_files_enabled=False)
    monkeypatch.setattr(storage, "download_file", lambda path: OOXML)
    _gotenberg(monkeypatch, env, lambda r: pytest.fail("office files are off"))
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: pytest.fail("nothing to run"))
    ri.execute(run["id"])
    frow = env.db.files(run["id"])[0]
    assert frow["status"] == protocol.STATUS_REJECTED
    assert frow["reject_code"] == protocol.REJECT_NOT_PDF
    assert frow["manifest"]["sniff"]["source_format"] is None
    assert env.store.files == []


def test_the_run_page_budget_is_checked_before_any_conversion(monkeypatch, tmp_path):
    env = _office_run(monkeypatch, tmp_path, rfp_ingest_max_pages_per_run=0)
    _gotenberg(monkeypatch, env, lambda r: pytest.fail("no budget, no conversion"))
    _script_sandbox(monkeypatch, env, lambda i, pdf, kw: pytest.fail("no budget, no child"))
    ri.execute("r1")
    frow = env.db.files("r1")[0]
    assert frow["reject_code"] == protocol.REJECT_RUN_PAGE_BUDGET
    assert "conversion" not in frow["manifest"]


def test_the_status_report_says_whether_office_files_are_on(monkeypatch, tmp_path):
    env = _env(monkeypatch, tmp_path, rfp_ingest_office_convert_timeout_seconds=90)
    monkeypatch.setattr(ri, "self_test", lambda: {"ok": True})
    report = ri.status_report(self_test_fresh=False)
    assert report["office_files"] == {
        "enabled": True, "engine": "gotenberg", "formats": ["doc", "docx", "xls", "xlsx"],
        "convert_timeout_seconds": 90,
    }
    assert env.settings.rfp_ingest_office_files_enabled is True


def test_office_convert_timeout_must_stay_under_half_the_queue_lease(tmp_path):
    with pytest.raises(ValueError, match="RFP_INGEST_OFFICE_CONVERT_TIMEOUT_SECONDS"):
        _settings(tmp_path, rfp_ingest_office_convert_timeout_seconds=450,
                  llm_queue_lease_seconds=900)
    with pytest.raises(ValueError, match="at least 1"):
        _settings(tmp_path, rfp_ingest_office_convert_timeout_seconds=0)
    assert _settings(tmp_path).rfp_ingest_office_convert_timeout_seconds == 180

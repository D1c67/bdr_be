"""A split that cannot finish no longer blocks the project (docs/RFP_SPLIT.md
section 10) and the storage upload retry that stops most of those failures.

Pinned:

- `storage.upload_file` retries a dropped connection (success on the second
  try; every retry upserts while the first keeps the caller's flag; the
  last error is raised after `_TRANSFER_ATTEMPTS`); `copy_file` retries too
  and reads a "Duplicate" on a retry as the landed copy;
- `rfp_split._start` queues nothing until the whole staging loop is done
  (a staging exception after N files enqueues nothing);
- the ingest ladders: at the attempt cap the split step marks the harvest
  split failed with the reason (the error text included) and moves the row
  `split -> create` (email and portal, the harvest-lookup failure too), with
  the `split.gave_up` bench event; below the cap the ladder is unchanged;
- a job whose every file failed names every reason; the flag
  (`split_issue`): failed, partial (the failed files listed), running,
  nothing for complete / skipped;
- the settle of a job serving an existing project writes the harvest back;
- the re-file of a project promoted whole BEFORE any job existed: the whole
  row becomes the source set in place and the segments are added;
- the router: "Run the splitter" (no job: stages in the background; failed
  files: re-queued on the job, linked to the project; package sent, running,
  marked outside, model away: 409), "split outside the app" and its Undo,
  the Created from RFP Ingestion flags;
- the creation bells carry the splitter sentence.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException
from storage3.exceptions import StorageApiError

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.routers import files as files_router
from app.routers import rfp_created as rr
from app.services import bid_split, office_preview, rfp_create, rfp_split, storage
from app.services import rfp_create_files as rcf
from app.services import rfp_email_ingest as ingest
from app.services import rfp_portal_ingest as portal
from tests.test_rfp_email_ingest import FakeDB
from tests.test_rfp_split import HV, JOB, _harvest, _settings

P1 = "5a000000-0000-4000-8000-000000000001"
SSL = "[SSL: SSLV3_ALERT_BAD_RECORD_MAC] ssl/tls alert bad record mac (_ssl.c:2580)"
# Captured at import: the autouse fixture stubs these on the storage module
# itself (rfp_split.storage IS that module).
REAL_UPLOAD = storage.upload_file
REAL_COPY = storage.copy_file


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(rfp_split, "audit", lambda *a, **k: None)
    monkeypatch.setattr(rfp_split, "model_away", lambda *a, **k: None)
    monkeypatch.setattr(rfp_split, "sandbox_busy", lambda *a, **k: None)
    monkeypatch.setattr(office_preview, "is_convertible", lambda *a: False)
    monkeypatch.setattr(rfp_split.storage, "upload_file", lambda *a, **k: None)
    monkeypatch.setattr(rfp_split.storage, "copy_file", lambda *a, **k: None)
    monkeypatch.setattr(rfp_split.storage, "delete_file", lambda *a, **k: None)


@pytest.fixture
def events(monkeypatch):
    out = []
    monkeypatch.setattr(rfp_split.rfp_test, "record", lambda sb, **kw: out.append(kw))
    return out


# ── A. The storage retry ─────────────────────────────────────────────────────


class _Bucket:
    """storage3's bucket proxy: `script` is one outcome per call (an
    exception to raise, or None for success); every call is recorded with a
    copy of the options (the SDK pops keys out of the dict it is given)."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def _next(self):
        outcome = self.script.pop(0) if self.script else None
        if isinstance(outcome, BaseException):
            raise outcome

    def upload(self, path, content, options):
        self.calls.append(("upload", path, dict(options)))
        options.pop("upsert", None)   # the SDK mutates what it is given
        self._next()

    def copy(self, src, dst):
        self.calls.append(("copy", src, dst))
        self._next()


def _install_bucket(monkeypatch, bucket):
    client = SimpleNamespace(storage=SimpleNamespace(from_=lambda name: bucket))
    monkeypatch.setattr(storage, "get_supabase", lambda: client)
    sleeps = []
    monkeypatch.setattr(storage.time, "sleep", sleeps.append)
    return sleeps


def test_upload_retries_a_dropped_connection_and_upserts_on_the_retry(monkeypatch):
    bucket = _Bucket([httpx.ReadError(SSL), None])
    sleeps = _install_bucket(monkeypatch, bucket)
    REAL_UPLOAD("bid-splits/j/source/a.pdf", b"%PDF", "application/pdf")
    assert [c[2]["upsert"] for c in bucket.calls] == ["false", "true"]
    assert all(c[2]["content-type"] == "application/pdf" for c in bucket.calls)
    assert sleeps == [storage._TRANSFER_BACKOFF_S]
    # A caller's upsert stays on the first attempt.
    bucket = _Bucket([None])
    _install_bucket(monkeypatch, bucket)
    REAL_UPLOAD("p", b"x", "text/plain", upsert=True)
    assert [c[2]["upsert"] for c in bucket.calls] == ["true"]


def test_upload_gives_up_after_the_attempts_and_raises_the_last_error(monkeypatch):
    bucket = _Bucket([httpx.WriteError("one"), httpx.ReadError("two"), httpx.ReadError(SSL)])
    sleeps = _install_bucket(monkeypatch, bucket)
    with pytest.raises(httpx.ReadError, match="BAD_RECORD_MAC"):
        REAL_UPLOAD("p", b"x", "application/pdf")
    assert len(bucket.calls) == storage._TRANSFER_ATTEMPTS == 3
    assert sleeps == [storage._TRANSFER_BACKOFF_S, storage._TRANSFER_BACKOFF_S * 2]
    # An HTTP-level error is not transient: raised on the first attempt.
    bucket = _Bucket([StorageApiError("bad key", "InvalidKey", 400)])
    _install_bucket(monkeypatch, bucket)
    with pytest.raises(StorageApiError):
        REAL_UPLOAD("p", b"x", "application/pdf")
    assert len(bucket.calls) == 1


def test_copy_retries_and_a_duplicate_after_a_drop_is_the_landed_copy(monkeypatch):
    bucket = _Bucket([httpx.ReadError(SSL), StorageApiError("The resource already exists", "Duplicate", 409)])
    _install_bucket(monkeypatch, bucket)
    REAL_COPY("bid-splits/j/output/e.pdf", "p1/electrical_drawing/x.pdf")
    assert [c[0] for c in bucket.calls] == ["copy", "copy"]
    # A duplicate on the FIRST attempt is a real conflict.
    bucket = _Bucket([StorageApiError("The resource already exists", "Duplicate", 409)])
    _install_bucket(monkeypatch, bucket)
    with pytest.raises(StorageApiError):
        REAL_COPY("a", "b")


# ── B. Staging queues nothing until every file is staged ─────────────────────


def test_a_staging_exception_after_n_files_enqueues_nothing(monkeypatch):
    db = FakeDB({
        "rfp_harvests": [_harvest(split_status="pending", files=[
            {"file_path": f"S{i}.pdf", "status": "accepted", "sandbox_file_id": f"sf-{i}"} for i in range(4)
        ])],
        "rfp_ingest_files": [{"id": f"sf-{i}", "source_format": "pdf"} for i in range(4)],
        "bid_split_jobs": [], "bid_split_files": [],
    })
    monkeypatch.setattr(rfp_split, "_model_label", lambda s: "m")
    monkeypatch.setattr(rfp_split, "_scratch_dir", lambda s: Path("/tmp"))
    monkeypatch.setattr(rfp_split, "_clean_scratch", lambda p: None)
    monkeypatch.setattr(rcf, "promotion_for", lambda e, fr: rcf.Promote("q", "p", e["file_path"], "application/pdf", True))
    monkeypatch.setattr(rcf, "fetch_entry", lambda d, fr, n, dest, mx: (d, b"not a pdf"))
    monkeypatch.setattr(rfp_split, "_stage_row", lambda *a, **k: _stage_or_die(db, a))
    queued = []
    monkeypatch.setattr(rfp_split.llm_queue, "enqueue", lambda jt, **kw: queued.append(kw["target_id"]))
    monkeypatch.setattr(rfp_split.storage, "delete_bid_split_prefix", lambda job_id: None)
    with pytest.raises(httpx.ReadError):
        # One at a time, so "the fourth file" is deterministic (section E
        # covers the concurrent staging).
        rfp_split._start(db, db.tables["rfp_harvests"][0], rfp_split._entries(db.tables["rfp_harvests"][0]),
                         _settings(rfp_split_stage_concurrency=1), {}, None, None)
    assert queued == []                            # three rows staged, none queued
    assert db.tables["bid_split_jobs"] == []       # the job was discarded


def _stage_or_die(db, args):
    staged = [r for r in db.tables["bid_split_files"]]
    if len(staged) == 3:
        raise httpx.ReadError(SSL)
    return db.table("bid_split_files").insert(
        {"job_id": args[1], "filename": f"S{len(staged)}.pdf", "status": "pending"}).execute().data[0]


def test_start_queues_every_pending_row_after_the_loop_with_the_manual_link(monkeypatch):
    db = FakeDB({
        "rfp_harvests": [_harvest(split_status="pending")],
        "rfp_ingest_files": [{"id": "sf-1", "source_format": "pdf"}, {"id": "sf-2", "source_format": "pdf"}],
        "bid_split_jobs": [], "bid_split_files": [],
    })
    monkeypatch.setattr(rfp_split, "_model_label", lambda s: "m")
    monkeypatch.setattr(rfp_split, "_scratch_dir", lambda s: Path("/tmp"))
    monkeypatch.setattr(rfp_split, "_clean_scratch", lambda p: None)
    monkeypatch.setattr(rcf, "promotion_for", lambda e, fr: rcf.Promote("q", "p", e["file_path"], "application/pdf", True))
    monkeypatch.setattr(rcf, "fetch_entry", lambda d, fr, n, dest, mx: (d, b"%PDF"))
    order = []
    monkeypatch.setattr(rfp_split, "_stage_row", lambda sb, job_id, *a, **k: (
        order.append("stage"), sb.table("bid_split_files").insert({"job_id": job_id, "status": "pending"}).execute().data[0])[1])
    monkeypatch.setattr(rfp_split.llm_queue, "enqueue", lambda jt, **kw: order.append(("queue", kw["created_by"])))
    monkeypatch.setattr(bid_split, "refresh_job", lambda job_id: None)
    job_id, staged = rfp_split._start(db, db.tables["rfp_harvests"][0], rfp_split._entries(db.tables["rfp_harvests"][0]),
                                      _settings(), {}, None, None, project_id=P1, created_by="u1")
    assert staged == 2 and order == ["stage", "stage", ("queue", "u1"), ("queue", "u1")]
    job = db.tables["bid_split_jobs"][0]
    assert job["project_id"] == P1 and job["created_by"] == "u1"
    assert db.tables["rfp_harvests"][0]["split_status"] == "running"


# ── B. The ladders: at the cap the split step falls through to create ───────


def _email_db(**over):
    row = {"id": "e1", "status": "split", "harvest_id": HV, "flag_reason": "no_candidate", "attempts": 3,
           "last_error": "earlier", "next_attempt_at": None, "subject": "ITB", "invitation_method": "organic",
           "extracted_project_name": "Warehouse", "test_session_id": "s1", "mailboxes": []}
    row.update(over)
    return FakeDB({"rfp_emails": [row], "rfp_harvests": [_harvest()]})


def _boom(*a, **k):
    raise httpx.ReadError(SSL)


def test_email_split_at_the_cap_marks_the_harvest_failed_and_moves_to_create(monkeypatch, events):
    monkeypatch.setattr(ingest, "get_settings", lambda: _settings(rfp_email_ingestion_classify_max_attempts=4))
    monkeypatch.setattr(ingest.rfp_split, "advance", _boom)
    alerts = []
    monkeypatch.setattr(ingest, "_alert_failed", lambda *a: alerts.append(a))
    db = _email_db()
    assert ingest._step_split(db, dict(db.tables["rfp_emails"][0])) == "create"
    row = db.tables["rfp_emails"][0]
    hv = db.tables["rfp_harvests"][0]
    assert row["status"] == "create" and row["attempts"] == 0 and row["decided_at_step"] == "split"
    assert row["flag_reason"] == "no_candidate"
    assert hv["split_status"] == "failed" and hv["split_finished_at"]
    assert hv["split_error"] == f"The documents could not be staged for splitting after 4 attempts: {SSL}"
    assert row["last_error"] == hv["split_error"]
    gave_up = [e for e in events if e["kind"] == "gave_up"]
    assert len(gave_up) == 1 and gave_up[0]["session_id"] == "s1" and gave_up[0]["rfp_email_id"] == "e1"
    assert gave_up[0]["detail"]["marked_failed"] is True and gave_up[0]["detail"]["next"] == "create"
    assert alerts == []   # nothing failed: the project carries the flag
    # Below the cap the ladder is unchanged: an attempt spent, the row waits at split.
    db = _email_db(attempts=1)
    assert ingest._step_split(db, dict(db.tables["rfp_emails"][0])) is None
    row = db.tables["rfp_emails"][0]
    assert row["status"] == "split" and row["attempts"] == 2 and row["last_error"] == SSL
    assert db.tables["rfp_harvests"][0]["split_status"] == "none"


def test_email_split_harvest_lookup_failure_at_the_cap_also_falls_through(monkeypatch, events):
    monkeypatch.setattr(ingest, "get_settings", lambda: _settings(rfp_email_ingestion_classify_max_attempts=4))
    db = _email_db()
    real_table = db.table

    def table(name):
        if name == "rfp_harvests":
            raise httpx.ConnectError("database down")
        return real_table(name)

    db.table = table
    assert ingest._step_split(db, dict(db.tables["rfp_emails"][0])) == "create"
    row = db.tables["rfp_emails"][0]
    assert row["status"] == "create" and "database down" in row["last_error"]
    # The harvest could not be written either: the reason says the step gave up.
    assert db.tables["rfp_harvests"][0]["split_status"] == "none"
    assert row["last_error"].startswith("The split step gave up after 4 attempts")


def test_email_split_at_the_cap_leaves_a_running_job_alone(monkeypatch, events):
    """The check failed but the job exists: the harvest stays `running` so
    creation links the job and its files re-file the project as they end."""
    monkeypatch.setattr(ingest, "get_settings", lambda: _settings(rfp_email_ingestion_classify_max_attempts=4))
    monkeypatch.setattr(ingest.rfp_split, "advance", _boom)
    db = _email_db()
    db.tables["rfp_harvests"][0].update(split_status="running", split_job_id=JOB)
    assert ingest._step_split(db, dict(db.tables["rfp_emails"][0])) == "create"
    assert db.tables["rfp_harvests"][0]["split_status"] == "running"
    assert events[-1]["detail"]["marked_failed"] is False


def test_portal_split_at_the_cap_falls_through_instead_of_holding(monkeypatch, events):
    settings = _settings(rfp_email_ingestion_classify_max_attempts=4)
    monkeypatch.setattr(portal.rfp_split, "advance", _boom)
    held = []
    monkeypatch.setattr(portal, "_hold_at_cap", lambda *a: held.append(a))
    db = FakeDB({"rfp_portal_invitations": [{"id": "inv-1", "portal": "ngem", "status": "split", "harvest_id": HV,
                                             "title": "Plumas St", "attempts": 3, "last_error": None,
                                             "next_attempt_at": None, "flag_reason": None}],
                 "rfp_harvests": [_harvest()]})
    assert portal._step_split(db, dict(db.tables["rfp_portal_invitations"][0]), settings) is True
    row = db.tables["rfp_portal_invitations"][0]
    assert row["status"] == "create" and row["attempts"] == 0 and row["decided_at_step"] == "split"
    assert SSL in row["last_error"] and held == []
    assert db.tables["rfp_harvests"][0]["split_status"] == "failed"
    # A row an older build held at the cap (attempts pinned there) falls through on its next failure.
    db.tables["rfp_portal_invitations"][0].update(status="split", attempts=4)
    db.tables["rfp_harvests"][0].update(split_status="none")
    assert portal._step_split(db, dict(db.tables["rfp_portal_invitations"][0]), settings) is True
    # Below the cap: the ladder.
    db.tables["rfp_portal_invitations"][0].update(status="split", attempts=0)
    db.tables["rfp_harvests"][0].update(split_status="none")
    assert portal._step_split(db, dict(db.tables["rfp_portal_invitations"][0]), settings) is False
    assert db.tables["rfp_portal_invitations"][0]["attempts"] == 1


# ── B4 / B5: the reasons and the flag ────────────────────────────────────────


def test_failure_reason_names_every_file_or_the_shared_reason():
    same = [{"filename": "a.pdf", "status": "failed", "error": "The model timed out."},
            {"filename": "b.pdf", "status": "failed", "error": "The model timed out."}]
    assert rfp_split.failure_reason(same) == "Every document failed to split (The model timed out)."
    mixed = [{"filename": "a.pdf", "status": "failed", "error": "The PDF has 674 pages; the splitter limit is 600 pages per file."},
             {"filename": "b.docx", "status": "failed", "error": None}]
    assert rfp_split.failure_reason(mixed) == (
        "Every document failed to split: a.pdf: The PDF has 674 pages; the splitter limit is 600 pages per file; "
        "b.docx: no reason recorded."
    )
    assert rfp_split.failure_reason([]) == rfp_split._MSG_ALL_FAILED


def test_a_job_that_failed_outright_carries_its_file_reasons_to_the_harvest(events):
    db = FakeDB({"rfp_harvests": [_harvest(split_status="running", split_job_id=JOB)],
                 "bid_split_jobs": [{"id": JOB, "status": "failed"}],
                 "bid_split_files": [{"id": "f1", "job_id": JOB, "filename": "SET.pdf", "status": "failed",
                                      "error": "Unreadable PDF.", "created_at": "1"}],
                 "bid_split_segments": [], "llm_jobs": []})
    out = rfp_split.advance(db, db.tables["rfp_harvests"][0], settings=_settings())
    assert out.state == "failed" and out.reason == "Every document failed to split (Unreadable PDF)."
    assert db.tables["rfp_harvests"][0]["split_error"] == out.reason


def _files(*states):
    return [{"id": f"f{i}", "job_id": JOB, "filename": f"F{i}.pdf", "status": s,
             "error": "The PDF has 674 pages; the splitter limit is 600 pages per file." if s == "failed" else None,
             "page_count": 674 if s == "failed" else 10}
            for i, s in enumerate(states)]


def test_split_issue_states():
    hv = _harvest(split_status="complete", split_job_id=JOB)
    job = {"id": JOB, "status": "done_with_errors"}
    issue = rfp_split.split_issue(hv, job, _files("done", "failed"))
    assert issue["state"] == "partial" and issue["files_total"] == 2 and issue["files_failed_count"] == 1
    assert issue["files_failed"] == [{"file_id": "f1", "filename": "F1.pdf",
                                      "error": "The PDF has 674 pages; the splitter limit is 600 pages per file."}]
    assert issue["reason"] is None and issue["job_id"] == JOB and issue["resolution"] is None
    # Complete with every file done, or skipped: nothing to say.
    assert rfp_split.split_issue(hv, {"id": JOB, "status": "done"}, _files("done")) is None
    assert rfp_split.split_issue(_harvest(split_status="skipped", split_error="no_files"), None, []) is None
    assert rfp_split.split_issue(_harvest(split_status="none"), None, []) is None
    # Staging gave up: failed with the harvest's reason, no job.
    failed = rfp_split.split_issue(_harvest(split_status="failed", split_error="The documents could not be staged."), None, [])
    assert failed["state"] == "failed" and failed["reason"] == "The documents could not be staged." and failed["job_id"] is None
    # A manual run: pending (fresh) and a processing job are running; a stale claim is failed.
    fresh = rfp_split._iso(rfp_split._now())
    assert rfp_split.split_issue(_harvest(split_status="pending", split_started_at=fresh), None, [])["state"] == "running"
    old = rfp_split._iso(rfp_split._now() - timedelta(hours=2))
    assert rfp_split.split_issue(_harvest(split_status="pending", split_started_at=old), None, [])["state"] == "failed"
    running = rfp_split.split_issue(_harvest(split_status="running", split_job_id=JOB), {"id": JOB, "status": "processing"},
                                    _files("done", "pending"))
    assert running["state"] == "running" and running["files_done"] == 1
    # Settled but not written back yet: graded like _check.
    assert rfp_split.split_issue(_harvest(split_status="running", split_job_id=JOB), {"id": JOB, "status": "failed"},
                                 _files("failed"))["state"] == "failed"
    # The resolution rides along (not on a running flag).
    rec = {"split_resolution": "outside", "split_resolved_by": "u1", "split_resolved_at": "2026-09-30T18:00:00+00:00"}
    marked = rfp_split.split_issue(hv, job, _files("failed"), record=rec, names={"u1": "Tom Moore"})
    assert marked["resolution"] == {"kind": "outside", "by": {"id": "u1", "full_name": "Tom Moore"},
                                    "at": "2026-09-30T18:00:00+00:00"}


def test_settle_linked_job_writes_the_outcome_back_for_an_existing_project(monkeypatch):
    db = FakeDB({"rfp_harvests": [_harvest(split_status="running", split_job_id=JOB)],
                 "rfp_created_projects": [{"project_id": P1, "split_status": "running", "split_job_id": JOB}],
                 "bid_split_files": _files("done", "failed")})
    rfp_split.settle_linked_job(db, {"id": JOB, "status": "done_with_errors", "project_id": P1, "source": "rfp"})
    assert db.tables["rfp_harvests"][0]["split_status"] == "complete"
    assert db.tables["rfp_created_projects"][0]["split_status"] == "complete"
    db.tables["rfp_harvests"][0]["split_status"] = "running"
    rfp_split.settle_linked_job(db, {"id": JOB, "status": "failed", "project_id": P1, "source": "rfp"})
    assert db.tables["rfp_harvests"][0]["split_status"] == "failed"
    assert db.tables["rfp_harvests"][0]["split_error"].startswith("Every document failed to split")
    # No project yet (the pipeline's own poll grades it): untouched.
    db.tables["rfp_harvests"][0]["split_status"] = "running"
    rfp_split.settle_linked_job(db, {"id": JOB, "status": "done", "project_id": None, "source": "rfp"})
    assert db.tables["rfp_harvests"][0]["split_status"] == "running"


def test_refresh_job_settles_the_harvest_when_the_job_serves_a_project(monkeypatch):
    db = FakeDB({"rfp_harvests": [_harvest(split_status="running", split_job_id=JOB)],
                 "rfp_created_projects": [{"project_id": P1}],
                 "bid_split_jobs": [{"id": JOB, "status": "processing", "project_id": P1, "source": "rfp",
                                     "created_by": None}],
                 "bid_split_files": _files("done")})
    monkeypatch.setattr(bid_split, "get_supabase", lambda: db)
    bid_split.refresh_job(JOB)
    assert db.tables["bid_split_jobs"][0]["status"] == "done"
    assert db.tables["rfp_harvests"][0]["split_status"] == "complete"


# ── C3a: the re-file of a project promoted whole before any job existed ─────


def test_a_run_after_creation_re_files_the_whole_promoted_rows(monkeypatch):
    """The fallback promoted SET.pdf whole (pre-split mapping: a
    `project_files` row carrying only the sandbox id). A manual run's job
    finishes it cut in two: that row becomes the source set in place (no
    second upload) and the two segments are added."""
    db = FakeDB({
        "project_files": [{"id": "pf-1", "project_id": P1, "category": "drawing", "filename": "SET.pdf",
                           "storage_path": f"{P1}/drawing/x-SET.pdf", "note": None, "rfp_harvest_id": HV,
                           "rfp_sandbox_file_id": "sf-1", "bid_split_segment_id": None, "bid_split_file_id": None,
                           "is_source_set": False, "preview_path": None}],
        "bid_split_jobs": [{"id": JOB, "status": "processing", "project_id": P1, "rfp_harvest_id": HV, "source": "rfp"}],
        "bid_split_files": [{"id": "f1", "job_id": JOB, "filename": "SET.pdf", "status": "done",
                             "rfp_sandbox_file_id": "sf-1", "created_at": "1"}],
        "bid_split_segments": [
            {"id": "s-g", "file_id": "f1", "sort_order": 0, "category": "general_drawings", "name": "Covers",
             "storage_path": "bid-splits/j/output/g.pdf", "size_bytes": 1, "is_original": False},
            {"id": "s-e", "file_id": "f1", "sort_order": 1, "category": "electrical_drawings", "name": "E-Sheets",
             "storage_path": "bid-splits/j/output/e.pdf", "size_bytes": 2, "is_original": False},
        ],
        "rfp_created_projects": [{"project_id": P1, "harvest_id": HV}],
        "rfp_harvests": [_harvest()],
        "rfp_ingest_files": [{"id": "sf-1", "source_format": "pdf"}],
    })
    monkeypatch.setattr(rfp_split, "_scratch_dir", lambda s: Path("/tmp"))
    monkeypatch.setattr(rfp_split, "_clean_scratch", lambda p: None)
    uploads = []
    monkeypatch.setattr(rfp_split.storage, "upload_file", lambda *a, **k: uploads.append(a))
    result = rfp_split.resync_project_files(db, "f1")
    assert result.used_split and result.inserted == 2 and uploads == []
    src = next(r for r in db.tables["project_files"] if r["id"] == "pf-1")
    assert src["is_source_set"] is True and src["category"] == "other" and src["bid_split_file_id"] == "f1"
    assert src["note"] == "Source set: split into 2 documents"
    cats = sorted(r["category"] for r in db.tables["project_files"] if not r["is_source_set"])
    assert cats == ["drawing", "electrical_drawing"]


# ── C: the router ────────────────────────────────────────────────────────────


def _user(role=Role.ESTIMATING_ADMIN, uid="u1"):
    return CurrentUser(id=uid, email="e@g3electrical.com", role=role, is_active=True)


def _rec(**over):
    row = {"project_id": P1, "source_kind": "rfp_email", "rfp_email_id": "e1", "portal_invitation_id": None,
           "harvest_id": HV, "split_status": "failed", "split_job_id": None, "split_resolution": None,
           "split_resolved_by": None, "split_resolved_at": None, "created_at": "2026-09-30T17:00:00+00:00",
           "cleared_at": None, "files_status": "complete", "files_promoted": 2, "files_skipped": []}
    row.update(over)
    return row


@pytest.fixture
def env(monkeypatch):
    db = FakeDB({
        "rfp_created_projects": [_rec()],
        "rfp_harvests": [_harvest(split_status="failed", project_id=P1,
                                  split_error=f"The documents could not be staged for splitting after 4 attempts: {SSL}")],
        "bid_split_jobs": [], "bid_split_files": [], "profiles": [{"id": "u1", "full_name": "Tom Moore"}],
        "projects": [{"id": P1, "number": "26.9.7300", "name": "Government Center AHU"}], "project_gcs": [],
        "general_contractors": [], "rfp_emails": [{"id": "e1", "subject": "ITB", "invitation_method": "organic"}],
        "rfp_portal_invitations": [], "llm_jobs": [],
    })
    monkeypatch.setattr(rr, "get_supabase", lambda: db)
    monkeypatch.setattr(bid_split, "get_supabase", lambda: db)
    monkeypatch.setattr(rfp_split, "get_settings", lambda: _settings())
    monkeypatch.setattr(files_router, "handoff_locked", lambda pid: False)
    audits = []
    monkeypatch.setattr(rr, "audit", lambda *a: audits.append(a))
    queued = []
    monkeypatch.setattr(rfp_split.llm_queue, "enqueue", lambda jt, **kw: queued.append((kw["target_id"], kw["created_by"])))
    return SimpleNamespace(db=db, audits=audits, queued=queued)


def _hv(env):
    return env.db.tables["rfp_harvests"][0]


def test_run_without_a_job_stages_in_the_background(env, monkeypatch):
    background = BackgroundTasks()
    out = rr.run_rfp_created_split(P1, background, user=_user())
    assert out["mode"] == "stage" and out["split"]["state"] == "running"
    assert _hv(env)["split_status"] == "pending" and _hv(env)["split_error"] is None
    assert env.db.tables["rfp_created_projects"][0]["split_status"] == "pending"
    assert [t.func for t in background.tasks] == [rfp_split.stage_for_project]
    assert background.tasks[0].args == (P1, "u1")
    assert env.audits[-1][1] == "rfp_created.split_run" and env.audits[-1][4]["mode"] == "stage"
    # A second press while it stages: 409.
    with pytest.raises(HTTPException) as exc:
        rr.run_rfp_created_split(P1, BackgroundTasks(), user=_user())
    assert exc.value.status_code == 409 and exc.value.detail == rfp_split.MSG_SPLIT_RUNNING
    # The background half: `_start` staged a job linked to the project.
    started = []

    def fake_start(sb, harvest, entries, settings, context, session_id, rfp_email_id, *, project_id, created_by, **_kw):
        started.append((project_id, created_by, context))
        sb.table("bid_split_jobs").insert({"id": JOB, "status": "processing", "project_id": project_id,
                                           "source": "rfp"}).execute()
        rfp_split._cas_split(sb, harvest["id"], "pending", {"split_status": "running", "split_job_id": JOB})
        return JOB, 2

    monkeypatch.setattr(rfp_split, "_start", fake_start)
    monkeypatch.setattr("app.core.supabase_client.get_supabase", lambda: env.db)
    rfp_split.stage_for_project(P1, "u1")
    assert started and started[0][:2] == (P1, "u1") and started[0][2]["subject"] == "ITB"
    assert _hv(env)["split_status"] == "running" and _hv(env)["split_job_id"] == JOB
    assert env.db.tables["rfp_created_projects"][0]["split_job_id"] == JOB


def test_background_staging_failure_marks_the_harvest_failed_with_the_reason(env, monkeypatch):
    rr.run_rfp_created_split(P1, BackgroundTasks(), user=_user())
    monkeypatch.setattr(rfp_split, "_start", _boom)
    monkeypatch.setattr("app.core.supabase_client.get_supabase", lambda: env.db)
    rfp_split.stage_for_project(P1, "u1")
    assert _hv(env)["split_status"] == "failed"
    assert _hv(env)["split_error"] == f"The documents could not be staged for splitting: {SSL}"


def test_run_with_failed_files_requeues_them_on_the_linked_job(env):
    env.db.tables["bid_split_jobs"] = [{"id": JOB, "status": "done_with_errors", "project_id": None,
                                        "source": "rfp", "created_by": None}]
    env.db.tables["bid_split_files"] = [
        {"id": "f0", "job_id": JOB, "filename": "A.pdf", "status": "done", "error": None, "page_count": 10, "created_at": "1"},
        {"id": "f1", "job_id": JOB, "filename": "B.pdf", "status": "failed", "error": "Timed out.", "page_count": 40, "created_at": "2"},
        {"id": "f2", "job_id": JOB, "filename": "C.pdf", "status": "failed",
         "error": "The PDF has 674 pages; the splitter limit is 600 pages per file.", "page_count": 674, "created_at": "3"},
    ]
    _hv(env).update(split_status="complete", split_error=None, split_job_id=JOB)
    out = rr.run_rfp_created_split(P1, BackgroundTasks(), user=_user())
    assert out["mode"] == "requeue" and out["files"] == 1 and out["job_id"] == JOB
    assert env.queued == [("f1", "u1")]                       # the page-cap file is not re-run
    files = {f["id"]: f for f in env.db.tables["bid_split_files"]}
    assert files["f1"]["status"] == "pending" and files["f2"]["status"] == "failed"
    assert env.db.tables["bid_split_jobs"][0]["project_id"] == P1          # linked: its runs re-file the project
    assert env.db.tables["bid_split_jobs"][0]["status"] == "processing"
    assert _hv(env)["split_status"] == "running" and out["split"]["state"] == "running"
    # The re-run fails again: the job settles, the harvest follows, the flag is back with the reason.
    bid_split._mark("f1", status="failed", error="Timed out again.")
    assert env.db.tables["bid_split_jobs"][0]["status"] == "done_with_errors"
    assert _hv(env)["split_status"] == "complete"
    issue = rr.issue_for_project(env.db, env.db.tables["rfp_created_projects"][0])
    assert issue["state"] == "partial" and issue["files_failed_count"] == 2
    # Only page-cap failures left: nothing the splitter can do.
    files["f1"]["page_count"] = 900
    with pytest.raises(HTTPException) as exc:
        rr.run_rfp_created_split(P1, BackgroundTasks(), user=_user())
    assert exc.value.status_code == 409 and "page limit (600 pages per file)" in exc.value.detail
    # ...and the flag says so, so the page hides "Run the splitter".
    issue = rr.issue_for_project(env.db, env.db.tables["rfp_created_projects"][0])
    assert issue["over_page_cap"] is True and issue["page_cap"] == 600


def test_run_refusals(env, monkeypatch):
    def refused(message):
        with pytest.raises(HTTPException) as exc:
            rr.run_rfp_created_split(P1, BackgroundTasks(), user=_user())
        assert exc.value.status_code == 409 and exc.value.detail == message

    monkeypatch.setattr(files_router, "handoff_locked", lambda pid: True)
    refused(rfp_split.MSG_PACKAGE_SENT)
    monkeypatch.setattr(files_router, "handoff_locked", lambda pid: False)
    monkeypatch.setattr(rfp_split, "model_away", lambda *a, **k: ("provider_down", "Could not connect — raw probe."))
    refused(rfp_split.model_away_sentence("provider_down"))
    assert "not responding" in rfp_split.model_away_sentence("provider_down")
    assert "\u2014" not in rfp_split.model_away_sentence("provider_down")
    monkeypatch.setattr(rfp_split, "model_away", lambda *a, **k: None)
    monkeypatch.setattr(rfp_split, "get_settings", lambda: _settings(rfp_split_enabled=False))
    refused(rfp_split.MSG_SPLIT_OFF)
    monkeypatch.setattr(rfp_split, "get_settings", lambda: _settings())
    env.db.tables["rfp_created_projects"][0]["split_resolution"] = "outside"
    refused(rfp_split.MSG_MARKED_OUTSIDE)
    env.db.tables["rfp_created_projects"][0]["split_resolution"] = None
    env.db.tables["bid_split_jobs"] = [{"id": JOB, "status": "processing", "project_id": P1, "source": "rfp"}]
    _hv(env).update(split_status="running", split_job_id=JOB)
    refused(rfp_split.MSG_SPLIT_RUNNING)
    _hv(env).update(split_status="skipped", split_error="no_files", split_job_id=None)
    refused(rfp_split.MSG_NOTHING_TO_RUN)
    assert env.queued == []


def test_outside_the_app_and_undo(env):
    out = rr.mark_rfp_created_split_outside(P1, user=_user())
    rec = env.db.tables["rfp_created_projects"][0]
    assert rec["split_resolution"] == "outside" and rec["split_resolved_by"] == "u1" and rec["split_resolved_at"]
    assert out["split"]["state"] == "failed"
    assert out["split"]["resolution"]["by"] == {"id": "u1", "full_name": "Tom Moore"}
    assert env.audits[-1][1] == "rfp_created.split_outside"
    with pytest.raises(HTTPException) as exc:
        rr.mark_rfp_created_split_outside(P1, user=_user())
    assert exc.value.status_code == 409
    out = rr.undo_rfp_created_split_outside(P1, user=_user())
    assert rec["split_resolution"] is None and rec["split_resolved_by"] is None and out["split"]["resolution"] is None
    assert env.audits[-1][1] == "rfp_created.split_outside_undo"
    with pytest.raises(HTTPException) as exc:
        rr.undo_rfp_created_split_outside(P1, user=_user())
    assert exc.value.status_code == 409
    # Nothing failed: nothing to mark.
    _hv(env).update(split_status="complete")
    with pytest.raises(HTTPException) as exc:
        rr.mark_rfp_created_split_outside(P1, user=_user())
    assert exc.value.status_code == 409


def test_over_page_cap_only_when_every_failed_file_is_over_the_cap():
    hv = _harvest(split_status="complete", split_job_id=JOB)
    job = {"id": JOB, "status": "done_with_errors"}
    assert rfp_split.split_issue(hv, job, _files("done", "failed"))["over_page_cap"] is True
    mixed = _files("done", "failed", "failed")
    mixed[2]["page_count"] = 40
    assert rfp_split.split_issue(hv, job, mixed)["over_page_cap"] is False
    # Staging gave up (no files): the run can stage again.
    assert rfp_split.split_issue(_harvest(split_status="failed", split_error="x"), None, [])["over_page_cap"] is False


def test_outside_reaps_a_dead_job_so_nothing_reads_processing(env):
    old = rfp_split._iso(rfp_split._now() - timedelta(hours=2))
    env.db.tables["bid_split_jobs"] = [{"id": JOB, "status": "processing", "project_id": P1, "source": "rfp",
                                        "rfp_harvest_id": HV, "created_by": None, "created_at": old, "updated_at": old}]
    env.db.tables["bid_split_files"] = [
        {"id": "f0", "job_id": JOB, "filename": "A.pdf", "status": "done", "error": None, "page_count": 10,
         "created_at": old, "updated_at": old},
        {"id": "f1", "job_id": JOB, "filename": "B.pdf", "status": "running", "error": None, "page_count": 10,
         "created_at": old, "updated_at": old},
    ]
    _hv(env).update(split_status="running", split_error=None, split_job_id=JOB)
    issue = rr.issue_for_project(env.db, env.db.tables["rfp_created_projects"][0])
    assert issue["state"] == "failed"                      # dead: nothing queued or running
    out = rr.mark_rfp_created_split_outside(P1, user=_user())
    assert env.db.tables["bid_split_jobs"][0]["status"] != "processing"
    files = {f["id"]: f for f in env.db.tables["bid_split_files"]}
    assert files["f1"]["status"] == "failed" and files["f1"]["error"] == rfp_split._MSG_JOB_INTERRUPTED
    assert _hv(env)["split_status"] in ("complete", "failed")
    assert out["split"]["resolution"]["kind"] == "outside" and out["split"]["state"] != "running"


def test_outside_settles_a_stale_staging_claim_and_its_orphans(env, monkeypatch):
    old = rfp_split._iso(rfp_split._now() - timedelta(hours=2))
    _hv(env).update(split_status="pending", split_error=None, split_job_id=None, split_started_at=old)
    env.db.tables["bid_split_jobs"] = [{"id": "orphan", "status": "processing", "project_id": P1, "source": "rfp",
                                        "rfp_harvest_id": HV, "created_at": old, "updated_at": old}]
    assert rr.issue_for_project(env.db, env.db.tables["rfp_created_projects"][0])["state"] == "failed"
    swept = []
    monkeypatch.setattr(rfp_split.storage, "delete_bid_split_prefix", lambda jid: swept.append(jid))
    rr.mark_rfp_created_split_outside(P1, user=_user())
    assert _hv(env)["split_status"] == "failed" and _hv(env)["split_error"] == rfp_split._MSG_RUN_INTERRUPTED
    assert env.db.tables["bid_split_jobs"] == [] and swept == ["orphan"]
    assert env.db.tables["rfp_created_projects"][0]["split_status"] == "failed"


def test_outside_leaves_a_live_run_alone(env):
    fresh = rfp_split._iso(rfp_split._now())
    env.db.tables["bid_split_jobs"] = [{"id": JOB, "status": "processing", "project_id": P1, "source": "rfp",
                                        "rfp_harvest_id": HV, "created_at": fresh, "updated_at": fresh}]
    _hv(env).update(split_status="failed", split_job_id=JOB)
    rfp_split.clear_interrupted(env.db, env.db.tables["rfp_created_projects"][0])
    assert env.db.tables["bid_split_jobs"][0]["status"] == "processing"


def test_the_created_page_flags_a_staging_failure_and_a_partial_split(env, monkeypatch):
    monkeypatch.setattr(rr, "get_settings", lambda: SimpleNamespace(rfp_email_ingestion_enabled=True))
    item = rr.list_rfp_created(cleared="all", limit=10, before=None, before_id=None, _=_user())[0]
    flags = item["flags"]
    assert flags["split_failed"] is True and flags["split_partial"] is False
    assert flags["split_issue"]["reason"].endswith(SSL) and flags["split_issue"]["package_sent"] is False
    env.db.tables["bid_split_jobs"] = [{"id": JOB, "status": "done_with_errors", "project_id": P1, "source": "rfp"}]
    env.db.tables["bid_split_files"] = _files("done", "failed")
    _hv(env).update(split_status="complete", split_error=None, split_job_id=JOB)
    flags = rr.list_rfp_created(cleared="all", limit=10, before=None, before_id=None, _=_user())[0]["flags"]
    assert flags["split_failed"] is False and flags["split_partial"] is True
    assert flags["split_issue"]["files_failed"][0]["filename"] == "F1.pdf"
    assert flags["split_job_id"] == JOB


# ── C4: the creation bells ───────────────────────────────────────────────────


def test_creation_note_and_the_bells(monkeypatch):
    db = FakeDB({"bid_split_files": _files("done", "failed", "failed")})
    assert rfp_split.creation_note(db, _harvest(split_status="failed", split_error="Staging gave up.")) == (
        "The Bid File Splitter failed on it (Staging gave up); every document was added whole."
    )
    assert rfp_split.creation_note(db, _harvest(split_status="complete", split_job_id=JOB)) == (
        "The Bid File Splitter could not split 2 of 3 files; those files were added whole."
    )
    assert rfp_split.creation_note(db, _harvest(split_status="skipped")) is None
    sent = []
    monkeypatch.setattr(rfp_create, "notify_role", lambda role, pid, kind, text, **kw: sent.append((kind, text, kw)))
    rfp_create._notify(db, {"id": P1, "number": "26.9.7300", "name": "AHU"}, [], "rfp_email",
                       "The Bid File Splitter failed on it (x); every document was added whole.")
    assert all(t.endswith(". The Bid File Splitter failed on it (x); every document was added whole.") for _k, t, _kw in sent)
    assert sent[0][2]["metadata"]["split_note"].startswith("The Bid File Splitter")


# ── D: review fixes ──────────────────────────────────────────────────────────


def test_run_is_refused_while_the_documents_are_still_being_promoted(env):
    rec = env.db.tables["rfp_created_projects"][0]
    for status in ("pending", "running"):
        rec.update(files_status=status, files_claimed_at=rfp_split._iso(rfp_split._now()))
        with pytest.raises(HTTPException) as exc:
            rr.run_rfp_created_split(P1, BackgroundTasks(), user=_user())
        assert exc.value.status_code == 409 and exc.value.detail == rfp_split.MSG_FILES_PROMOTING
    assert _hv(env)["split_status"] == "failed" and env.queued == []
    # A promotion whose worker is gone (stale claim) no longer blocks the run.
    rec.update(files_status="running", files_claimed_at="2026-01-01T00:00:00+00:00")
    assert rr.run_rfp_created_split(P1, BackgroundTasks(), user=_user())["mode"] == "stage"


def test_the_manual_claim_is_fenced_on_the_stamp_not_only_the_status(env):
    # A `running` harvest whose job settled without the write-back: a CAS on
    # the status alone (running -> running) would let a second press through.
    _hv(env).update(split_status="running", split_started_at="2026-09-30T17:00:00+00:00")
    seen = dict(_hv(env))
    fields = {"split_status": "running", "split_started_at": "2026-09-30T18:00:00+00:00"}
    assert rfp_split._claim_manual(env.db, seen, "running", fields) is True
    assert rfp_split._claim_manual(env.db, seen, "running", dict(fields)) is False
    # No stamp yet: fenced on null.
    _hv(env).update(split_status="failed", split_started_at=None)
    seen = dict(_hv(env))
    assert rfp_split._claim_manual(env.db, seen, "failed", {"split_status": "pending", "split_started_at": "x"}) is True
    assert rfp_split._claim_manual(env.db, seen, "pending", {"split_status": "pending", "split_started_at": "y"}) is False


def test_creation_settles_a_job_that_finished_before_it_was_linked(monkeypatch):
    # The split step gave up with the harvest at `running` (its job kept
    # going) and the job settled before creation linked it: no write-back
    # happened, so the link does it.
    db = FakeDB({
        "rfp_harvests": [_harvest(split_status="running", split_job_id=JOB)],
        "bid_split_jobs": [{"id": JOB, "status": "failed", "project_id": None, "source": "rfp"}],
        "bid_split_files": _files("failed", "failed"),
        "rfp_created_projects": [_rec(split_status="running", split_job_id=JOB)],
    })
    monkeypatch.setattr(rfp_create, "link_harvest_mates", lambda *a, **k: None)
    source = rfp_create._Source(kind=rfp_create.SOURCE_EMAIL, table="rfp_emails", row={"id": "e1"},
                                expected_status="create")
    rfp_create._attach_harvest(db, source, db.tables["rfp_harvests"][0], None, P1)
    assert db.tables["bid_split_jobs"][0]["project_id"] == P1
    assert db.tables["rfp_harvests"][0]["split_status"] == "failed"
    assert db.tables["rfp_created_projects"][0]["split_status"] == "failed"
    # A job still processing is left to settle itself.
    db.tables["rfp_harvests"][0].update(split_status="running")
    db.tables["bid_split_jobs"][0].update(status="processing", project_id=None)
    rfp_create._attach_harvest(db, source, db.tables["rfp_harvests"][0], None, P1)
    assert db.tables["rfp_harvests"][0]["split_status"] == "running"


def test_a_mate_row_never_reclaims_an_interrupted_manual_run(events):
    # "Run the splitter" staged in the background and the server restarted:
    # the linked harvest sits at a stale `pending`. A mate row reaching the
    # split step moves on (links) without turning the failure into `skipped`.
    db = FakeDB({"rfp_harvests": [_harvest(split_status="pending", project_id=P1,
                                           split_started_at="2026-01-01T00:00:00+00:00")]})
    out = rfp_split.advance(db, dict(db.tables["rfp_harvests"][0]), settings=_settings())
    assert out.state == "skipped" and out.reason == rfp_split.SKIP_LINKED
    assert db.tables["rfp_harvests"][0]["split_status"] == "pending"
    issue = rfp_split.split_issue(db.tables["rfp_harvests"][0], None, [])
    assert issue["state"] == "failed"

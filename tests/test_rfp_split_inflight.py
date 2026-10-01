# ruff: noqa: F811 - the imported `env` fixture is a parameter by design
"""The split step's four in-flight gaps (docs/RFP_SPLIT.md 10.5).

Pinned:

1. Creation waits for a split that is going to file the documents
   (`rfp_split.creation_must_wait`): a live staging claim, or a row sharing
   the harvest at `split`; never for a terminal split, the step off, a
   linked harvest, or a `none` / `running` harvest nothing will move (no
   deadlock). The sweep's create step waits without spending an attempt
   (email and portal).
2. Staging runs RFP_SPLIT_STAGE_CONCURRENCY entries at once: bounded,
   `staged_names` in entry order, nothing queued until every entry is
   staged, all or nothing on a failure mid-flight (in-flight workers joined
   before the discard, entries not yet started never start).
3. "Retry documents" answers 409 while the split runs on the project; the
   splitter's re-file adopts a whole row the promotion inserted meanwhile
   (source set in place, or the intact file's category) instead of leaving
   a duplicate.
4. The staging heartbeat: the claim is re-stamped while the loop lives (a
   long staging whose first stamp is old is never stale), a claim taken
   over stops the staging, a stale claim ages out in minutes and its orphan
   job is discarded, and a `processing` job with nothing queued or running
   is reaped ("interrupted") so the row moves on and "Run the splitter"
   queues it again. The portal's split step treats our own AI gate busy as
   a wait.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.core import supabase_client
from app.services import bid_split, llm_gate, office_preview, rfp_create, rfp_split
from app.services import rfp_create_files as rcf
from app.services import rfp_email_ingest as ingest
from app.services import rfp_portal_ingest as portal
from app.routers import rfp_created as rr
from tests.test_rfp_email_ingest import FakeDB
from tests.test_rfp_split import HV, JOB, _harvest, _settings
from tests.test_rfp_split_fallback import P1, _files, _hv, _rec, _user, env  # noqa: F401 - `env` is a fixture

OLD = "2026-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(rfp_split, "audit", lambda *a, **k: None)
    monkeypatch.setattr(rfp_split, "model_away", lambda *a, **k: None)
    monkeypatch.setattr(rfp_split, "sandbox_busy", lambda *a, **k: None)
    monkeypatch.setattr(rfp_split.rfp_test, "record", lambda sb, **kw: None)
    monkeypatch.setattr(office_preview, "is_convertible", lambda *a: False)
    monkeypatch.setattr(rfp_split.storage, "upload_file", lambda *a, **k: None)
    monkeypatch.setattr(rfp_split.storage, "copy_file", lambda *a, **k: None)
    monkeypatch.setattr(rfp_split.storage, "delete_file", lambda *a, **k: None)


def _ago(seconds: float) -> str:
    return rfp_split._iso(rfp_split._now() - timedelta(seconds=seconds))


# ── 1. Creation waits for a split in flight ──────────────────────────────────


def _wait_db(status="done", mates=(), **hv):
    return FakeDB({
        "rfp_harvests": [_harvest(**hv)],
        "rfp_emails": [{"id": "e-copy", "status": status, "harvest_id": HV},
                       *[{"id": f"e-{i}", "status": s, "harvest_id": HV} for i, s in enumerate(mates)]],
        "rfp_portal_invitations": [],
    })


def test_creation_waits_only_for_a_split_that_will_move():
    s = _settings()
    me = ("rfp_emails", "e-copy")

    def waits(db):
        return rfp_split.creation_must_wait(db, _hv_of(db), settings=s, exclude=me)

    # A live staging claim: wait, mates or not.
    assert waits(_wait_db(split_status="pending", split_started_at=_ago(10)))
    # A leader at `split` with the harvest at none / running / a stale claim: wait.
    assert waits(_wait_db(mates=["split"], split_status="none"))
    assert waits(_wait_db(mates=["split"], split_status="running", split_job_id=JOB))
    assert waits(_wait_db(mates=["split"], split_status="pending", split_started_at=OLD))
    # Nothing at `split`: nothing will move it, so no deadlock.
    assert not waits(_wait_db(mates=["create"], split_status="none"))
    assert not waits(_wait_db(mates=["done"], split_status="running", split_job_id=JOB))
    assert not waits(_wait_db(split_status="pending", split_started_at=OLD))
    # Terminal, linked, no entries, flags or queue off: never.
    for hs in ("complete", "failed", "skipped"):
        assert not waits(_wait_db(mates=["split"], split_status=hs))
    assert not waits(_wait_db(mates=["split"], split_status="running", project_id=P1))
    assert not waits(_wait_db(mates=["split"], split_status="none", files=[]))
    db = _wait_db(mates=["split"], split_status="none")
    assert not rfp_split.creation_must_wait(db, _hv_of(db), settings=_settings(rfp_split_enabled=False))
    assert not rfp_split.creation_must_wait(db, _hv_of(db), settings=_settings(llm_queue_enabled=False))
    # The creating row itself never counts; a portal invitation mate does.
    db = FakeDB({"rfp_harvests": [_harvest(split_status="none")],
                 "rfp_emails": [{"id": "e-copy", "status": "split", "harvest_id": HV}],
                 "rfp_portal_invitations": []})
    assert not rfp_split.creation_must_wait(db, _hv_of(db), settings=s, exclude=me)
    db.tables["rfp_portal_invitations"].append({"id": "inv-1", "status": "split", "harvest_id": HV})
    assert rfp_split.creation_must_wait(db, _hv_of(db), settings=s, exclude=me)


def _hv_of(db):
    return db.tables["rfp_harvests"][0]


def _waiting(*a, **k):
    raise rfp_create.CreateWaitingForSplit(rfp_split.MSG_CREATE_WAITS)


def test_the_email_create_step_waits_without_spending_an_attempt(monkeypatch):
    settings = _settings(rfp_create_auto_enabled=True)
    monkeypatch.setattr(ingest, "get_settings", lambda: settings)
    monkeypatch.setattr(rfp_create, "create_block_for", lambda *a, **k: None)
    monkeypatch.setattr(rfp_create, "create_from_email", _waiting)
    row = {"id": "e1", "status": "create", "harvest_id": HV, "attempts": 2, "flag_reason": None,
           "extracted_project_name": "Warehouse", "subject": "ITB", "last_error": None, "next_attempt_at": None,
           "test_session_id": None}
    db = FakeDB({"rfp_emails": [row], "rfp_harvests": [_harvest()]})
    assert ingest._step_create(db, dict(row)) is None
    after = db.tables["rfp_emails"][0]
    assert after["status"] == "create" and after["attempts"] == 2
    assert after["last_error"] == rfp_split.MSG_CREATE_WAITS and after["next_attempt_at"]


def test_the_portal_create_step_waits_without_spending_an_attempt(monkeypatch):
    settings = _settings(rfp_create_auto_enabled=True)
    monkeypatch.setattr(rfp_create, "create_block_for", lambda *a, **k: None)
    monkeypatch.setattr(rfp_create, "create_from_portal", _waiting)
    monkeypatch.setattr(portal, "_portal_hook", lambda *a: None)
    row = {"id": "inv-1", "portal": "ngem", "status": "create", "harvest_id": HV, "title": "Plumas St",
           "attempts": 1, "last_error": None, "next_attempt_at": None, "flag_reason": None}
    db = FakeDB({"rfp_portal_invitations": [row], "rfp_harvests": [_harvest()]})
    portal._step_create(db, dict(row), settings)
    after = db.tables["rfp_portal_invitations"][0]
    assert after["status"] == "create" and after["attempts"] == 1
    assert after["last_error"] == rfp_split.MSG_CREATE_WAITS and after["next_attempt_at"]


# ── 2. Concurrent staging ────────────────────────────────────────────────────


class _Stage:
    """A thread-safe fake of the per-entry fetch and row insert: counts the
    entries in flight, records the order things happen in, and can fail one
    entry by name after a short delay."""

    def __init__(self, db, *, delay=0.05, fail=None, gate=None):
        self.db = db
        self.delay = delay
        self.fail = fail
        self.gate = gate
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.fetched: list[str] = []
        self.timeline: list[str] = []

    def fetch(self, decision, file_row, name, dest, mx):
        with self.lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            self.fetched.append(name)
        try:
            if self.gate is not None:
                self.gate(name)
            time.sleep(self.delay)
            if name == self.fail:
                raise rcf.RfpCreateFilesTransient("storage down")
            return decision, b"%PDF"
        finally:
            with self.lock:
                self.in_flight -= 1

    def stage_row(self, sb, job_id, entry, file_row, decision, data, name, context, settings, dest):
        with self.lock:
            row = {"id": f"row-{name}", "job_id": job_id, "filename": name, "status": "pending",
                   "rfp_sandbox_file_id": file_row["id"], "source_format": "pdf", "classified_from": "pages"}
            sb.tables.setdefault("bid_split_files", []).append(dict(row))
            self.timeline.append(f"row:{name}")
            return row


def _stage_env(monkeypatch, n=8, **kw):
    files = [{"file_path": f"S{i}.pdf", "status": "accepted", "sandbox_file_id": f"sf-{i}"} for i in range(n)]
    db = FakeDB({
        "rfp_harvests": [_harvest(split_status="pending", split_started_at=None, files=files)],
        "rfp_ingest_files": [{"id": f"sf-{i}", "source_format": "pdf"} for i in range(n)],
        "bid_split_jobs": [], "bid_split_files": [], "llm_jobs": [],
    })
    stage = _Stage(db, **kw)
    monkeypatch.setattr(rfp_split, "_model_label", lambda s: "m")
    monkeypatch.setattr(rfp_split, "_scratch_dir", lambda s: Path("/tmp"))
    monkeypatch.setattr(rfp_split, "_clean_scratch", lambda p: stage.timeline.append("scratch_cleaned"))
    monkeypatch.setattr(rcf, "promotion_for", lambda e, fr: rcf.Promote("q", "p", e["file_path"], "application/pdf", True))
    monkeypatch.setattr(rcf, "fetch_entry", stage.fetch)
    monkeypatch.setattr(rfp_split, "_stage_row", stage.stage_row)
    monkeypatch.setattr(rfp_split.llm_queue, "enqueue", lambda jt, **kw: stage.timeline.append(f"queue:{kw['target_id']}"))
    monkeypatch.setattr(rfp_split.storage, "delete_bid_split_prefix", lambda job_id: stage.timeline.append("discard"))
    monkeypatch.setattr(bid_split, "refresh_job", lambda job_id: None)
    return db, stage


def test_staging_runs_four_at_a_time_in_entry_order_and_queues_after(monkeypatch):
    db, stage = _stage_env(monkeypatch)
    started = []
    monkeypatch.setattr(rfp_split, "_record", lambda sb, sid, kind, title, **kw: started.append(kw.get("detail")))
    hv = _hv_of(db)
    job_id, staged = rfp_split._start(db, hv, rfp_split._entries(hv), _settings(), {}, None, None)
    assert staged == 8
    assert 1 < stage.peak <= 4                              # bounded, and actually concurrent
    names = [f["filename"] for f in started[-1]["files"]]
    assert names == [f"S{i}.pdf" for i in range(8)]         # entry order, whatever finished first
    rows = [t for t in stage.timeline if t.startswith("row:")]
    queues = [t for t in stage.timeline if t.startswith("queue:")]
    assert len(rows) == 8 and len(queues) == 8
    assert stage.timeline.index(queues[0]) > max(stage.timeline.index(r) for r in rows)   # queued after every row
    assert queues == [f"queue:row-S{i}.pdf" for i in range(8)]
    assert stage.timeline.index("scratch_cleaned") < stage.timeline.index(queues[0])
    assert _hv_of(db)["split_status"] == "running" and _hv_of(db)["split_job_id"] == job_id
    # The setting bounds it: one at a time.
    db, stage = _stage_env(monkeypatch, n=3, delay=0.01)
    rfp_split._start(db, _hv_of(db), rfp_split._entries(_hv_of(db)), _settings(rfp_split_stage_concurrency=1), {}, None, None)
    assert stage.peak == 1


def test_a_failure_mid_flight_discards_everything_after_joining_the_workers(monkeypatch):
    db, stage = _stage_env(monkeypatch, n=12, delay=0.05, fail="S1.pdf")
    hv = _hv_of(db)
    with pytest.raises(rcf.RfpCreateFilesTransient):
        rfp_split._start(db, hv, rfp_split._entries(hv), _settings(), {}, None, None)
    assert not any(t.startswith("queue:") for t in stage.timeline)     # nothing queued
    assert db.tables["bid_split_jobs"] == []                            # the job row went
    assert stage.timeline[-2:] == ["discard", "scratch_cleaned"]        # after every worker landed
    assert len(stage.fetched) < 12                                      # entries not yet started never ran
    assert _hv_of(db)["split_status"] == "pending"                      # `advance` puts the claim back


def test_the_setting_is_validated():
    with pytest.raises(ValueError, match="RFP_SPLIT_STAGE_CONCURRENCY"):
        _settings(rfp_split_stage_concurrency=0)
    with pytest.raises(ValueError, match="RFP_SPLIT_STAGING_STALE_SECONDS"):
        _settings(rfp_split_staging_heartbeat_seconds=60, rfp_split_staging_stale_seconds=120)
    s = _settings()
    assert (s.rfp_split_stage_concurrency, s.rfp_split_staging_heartbeat_seconds,
            s.rfp_split_staging_stale_seconds) == (4, 30, 300)


# ── 3. Retry documents and the re-file race ─────────────────────────────────


def test_retry_documents_is_refused_while_the_split_runs(env, monkeypatch):
    enqueued = []
    monkeypatch.setattr(rcf, "enqueue", lambda pid, **kw: enqueued.append(pid) or {"id": "j1", "status": "queued"})
    rec = env.db.tables["rfp_created_projects"][0]
    rec.update(files_status="failed")
    # A manual run staging (a fresh claim) ...
    _hv(env).update(split_status="pending", split_started_at=rfp_split._iso(rfp_split._now()))
    with pytest.raises(HTTPException) as exc:
        rr.retry_rfp_created_files(P1, user=_user())
    assert exc.value.status_code == 409 and exc.value.detail == rfp_split.MSG_RETRY_WHILE_SPLITTING
    # ... or its job processing.
    env.db.tables["bid_split_jobs"] = [{"id": JOB, "status": "processing", "project_id": P1, "source": "rfp",
                                        "updated_at": rfp_split._iso(rfp_split._now())}]
    _hv(env).update(split_status="running", split_job_id=JOB)
    with pytest.raises(HTTPException) as exc:
        rr.retry_rfp_created_files(P1, user=_user())
    assert exc.value.status_code == 409 and enqueued == []
    # Settled: the retry goes through.
    env.db.tables["bid_split_jobs"][0]["status"] = "done_with_errors"
    _hv(env).update(split_status="complete")
    assert rr.retry_rfp_created_files(P1, user=_user())["job"]["id"] == "j1"
    assert enqueued == [P1]


class _PartialUniqueDB(FakeDB):
    """project_files' two partial unique indexes: (project_id,
    rfp_sandbox_file_id) and (project_id, bid_split_segment_id), each only
    where the column is not null."""

    def table(self, name):
        query = super().table(name)
        if name != "project_files":
            return query

        def check(rows, payload):
            for col in ("rfp_sandbox_file_id", "bid_split_segment_id"):
                if payload.get(col) is None:
                    continue
                if any(r.get("project_id") == payload.get("project_id") and r.get(col) == payload.get(col)
                       for r in rows):
                    raise Exception(f'duplicate key value violates unique constraint "{col}" (23505)')

        query._check_unique = check
        return query


SPLIT_FILE = {"id": "f1", "job_id": JOB, "status": "done", "rfp_sandbox_file_id": "sf-1", "filename": "SET.pdf"}
ORIGINAL = (rcf.Promote("rfp-quarantine", "run/sf-1/source.pdf", "SET.pdf", "application/pdf", True), b"%PDF")


def _whole_row():
    return {"id": "pf-whole", "project_id": P1, "category": "drawing", "filename": "SET.pdf",
            "rfp_sandbox_file_id": "sf-1", "bid_split_file_id": None, "bid_split_segment_id": None,
            "is_source_set": False, "note": None, "storage_path": "p/drawing/SET.pdf"}


def test_the_refile_adopts_a_whole_row_the_promotion_inserted_meanwhile(monkeypatch):
    monkeypatch.setattr(rfp_split.files_needed, "clear_if_satisfied", lambda *a, **k: None)
    segs = [
        {"id": "s-g", "file_id": "f1", "sort_order": 0, "category": "general_drawings", "name": "Covers",
         "storage_path": "bid-splits/j/g.pdf", "size_bytes": 1, "is_original": False},
        {"id": "s-e", "file_id": "f1", "sort_order": 1, "category": "electrical_drawings", "name": "E-Sheets",
         "storage_path": "bid-splits/j/e.pdf", "size_bytes": 1, "is_original": False},
    ]
    # The re-file read `existing` (empty); the promotion's whole row landed before its source-set insert.
    db = _PartialUniqueDB({"project_files": [_whole_row()]})
    rfp_split.promote_split_file(db, project_id=P1, harvest_id=HV, split_file=SPLIT_FILE, segments=segs,
                                 existing=[], fetch_original=lambda: ORIGINAL, name="SET.pdf")
    rows = db.tables["project_files"]
    whole = next(r for r in rows if r["id"] == "pf-whole")
    assert whole["is_source_set"] is True and whole["category"] == "other" and whole["bid_split_file_id"] == "f1"
    assert sorted(r["category"] for r in rows if not r.get("is_source_set")) == ["drawing", "electrical_drawing"]
    assert sum(1 for r in rows if r.get("rfp_sandbox_file_id") == "sf-1") == 1       # no duplicate
    # Intact: the whole row takes the splitter's category and segment in place.
    db = _PartialUniqueDB({"project_files": [_whole_row()]})
    intact = [{"id": "s-1", "file_id": "f1", "sort_order": 0, "category": "specifications", "name": "Specs",
               "storage_path": None, "size_bytes": 1, "is_original": True}]
    rfp_split.promote_split_file(db, project_id=P1, harvest_id=HV, split_file=SPLIT_FILE, segments=intact,
                                 existing=[], fetch_original=lambda: ORIGINAL, name="SET.pdf")
    (row,) = db.tables["project_files"]
    assert row["category"] == "specification" and row["bid_split_segment_id"] == "s-1"
    assert row["bid_split_file_id"] == "f1"


# ── 4. Heartbeat, fast staleness, dead jobs ──────────────────────────────────


def test_a_long_staging_keeps_its_claim_fresh(monkeypatch):
    """The claim started ten minutes ago (past the five-minute window), but
    the loop re-stamps it while files are still in flight, so the harvest
    never reads stale while the staging lives, and the lease renewal rides
    along with each beat."""
    db, _stage = _stage_env(monkeypatch, n=4, delay=0.0)
    started = _ago(600)
    _hv_of(db)["split_started_at"] = started
    renewals = []
    claim = rfp_split._StagingClaim(db, HV, started, _settings(), renew=lambda: renewals.append(1) or True)
    claim.interval = 0.02
    seen = []

    def gate(name):
        if name == "S3.pdf":
            deadline = time.monotonic() + 2
            while _hv_of(db)["split_started_at"] == started and time.monotonic() < deadline:
                time.sleep(0.01)
            seen.append(rfp_split.pending_is_stale(dict(_hv_of(db)), settings=_settings()))

    monkeypatch.setattr(rcf, "fetch_entry", lambda d, fr, n, dest, mx: (gate(n), (d, b"%PDF"))[1])
    hv = _hv_of(db)
    rfp_split._start(db, hv, rfp_split._entries(hv), _settings(rfp_split_stage_concurrency=2), {}, None, None,
                     claim=claim)
    assert seen == [False]                  # the live staging was never stale
    assert renewals                         # the sweep lease was renewed with the beats
    assert _hv_of(db)["split_status"] == "running"
    # Before the staging ever beat, the same stamp reads stale: the window is minutes, not an hour.
    assert rfp_split.pending_is_stale({"split_status": "pending", "split_started_at": started})
    assert not rfp_split.pending_is_stale({"split_status": "pending", "split_started_at": _ago(240)})
    assert rfp_split.pending_is_stale({"split_status": "pending", "split_started_at": _ago(301)})


def test_a_claim_taken_over_mid_staging_stops_it(monkeypatch):
    db, stage = _stage_env(monkeypatch, n=6, delay=0.02)
    claim = rfp_split._StagingClaim(db, HV, None, _settings())
    claim.interval = 0.0

    def gate(name):
        if name == "S1.pdf":   # another worker reclaimed the stale claim and re-stamped it
            _hv_of(db).update(split_status="pending", split_started_at="2026-09-30T18:00:00+00:00")

    stage.gate = gate
    hv = _hv_of(db)
    with pytest.raises(rfp_split.StagingClaimLost):
        rfp_split._start(db, hv, rfp_split._entries(hv), _settings(), {}, None, None, claim=claim)
    assert not any(t.startswith("queue:") for t in stage.timeline) and db.tables["bid_split_jobs"] == []
    # The put-back in `advance` is fenced on the lost stamp: the new owner's claim stands.
    assert rfp_split._cas_claim(db, HV, "pending", claim.stamp, {"split_status": "none"}) is False
    assert _hv_of(db)["split_status"] == "pending"


def test_a_stale_claim_restages_and_discards_the_orphan_job(monkeypatch):
    db = FakeDB({
        "rfp_harvests": [_harvest(split_status="pending", split_started_at=_ago(400))],
        "bid_split_jobs": [
            {"id": "orphan", "status": "processing", "rfp_harvest_id": HV, "source": "rfp"},
            {"id": "busy", "status": "processing", "rfp_harvest_id": HV, "source": "rfp"},
        ],
        "bid_split_files": [
            {"id": "o1", "job_id": "orphan", "status": "pending", "created_at": "1"},
            {"id": "b1", "job_id": "busy", "status": "pending", "created_at": "1"},
        ],
        "llm_jobs": [{"id": "q1", "job_type": "bid_split", "target_id": "b1", "status": "queued"}],
    })
    swept = []
    monkeypatch.setattr(rfp_split.storage, "delete_bid_split_prefix", swept.append)
    monkeypatch.setattr(rfp_split, "_start", lambda *a, **k: ("new-job", 1))
    out = rfp_split.advance(db, dict(_hv_of(db)), settings=_settings())
    assert out.waiting and out.job_id == "new-job"
    assert swept == ["orphan"]
    assert [j["id"] for j in db.tables["bid_split_jobs"]] == ["busy"]     # a job with a live run is left alone
    # A claim four minutes old is a live staging: wait, nothing discarded.
    db.tables["rfp_harvests"][0].update(split_status="pending", split_started_at=_ago(240))
    assert rfp_split.advance(db, dict(_hv_of(db)), settings=_settings()).waiting
    assert swept == ["orphan"]


def _dead_db(*, job_age=600, file_age=600, llm=(), hv_status="running", project_id=None):
    return FakeDB({
        "rfp_harvests": [_harvest(split_status=hv_status, split_job_id=JOB, project_id=project_id,
                                  split_started_at=_ago(job_age))],
        "bid_split_jobs": [{"id": JOB, "status": "processing", "source": "rfp", "project_id": project_id,
                            "created_by": None, "updated_at": _ago(job_age)}],
        "bid_split_files": [
            {"id": "f0", "job_id": JOB, "filename": "A.pdf", "status": "done", "created_at": "1",
             "updated_at": _ago(file_age)},
            {"id": "f1", "job_id": JOB, "filename": "B.pdf", "status": "pending", "created_at": "2",
             "updated_at": _ago(file_age)},
        ],
        "bid_split_segments": [],
        "llm_jobs": list(llm),
    })


def test_the_pipeline_reaps_a_dead_job_and_queues_it_again_up_to_the_cap(monkeypatch):
    """A restart strands a file: the pipeline reaps it and queues it again on
    its own (the outage requeue, review 2026-09-30); only past the per-file
    cap does it stay failed "interrupted" and the row move on with the
    partial flag."""
    db = _dead_db()
    monkeypatch.setattr(bid_split, "get_supabase", lambda: db)
    monkeypatch.setattr(rfp_split, "get_settings", lambda: _settings())
    queued = []

    def enqueue(jt, **kw):
        queued.append(kw["target_id"])
        db.tables["llm_jobs"].append({"id": f"q{len(queued)}", "job_type": "bid_split",
                                      "target_id": kw["target_id"], "status": "queued",
                                      "created_at": f"2026-09-30T00:00:0{len(queued)}+00:00"})

    monkeypatch.setattr(rfp_split.llm_queue, "enqueue", enqueue)
    out = rfp_split.advance(db, dict(_hv_of(db)), settings=_settings())
    assert out.waiting and queued == ["f1"]
    f1 = next(f for f in db.tables["bid_split_files"] if f["id"] == "f1")
    assert f1["status"] == "pending" and f1["error"] is None
    assert db.tables["bid_split_jobs"][0]["status"] == "processing"
    # Past the cap (three runs in all), the interrupted file stays failed.
    db = _dead_db(llm=[{"id": f"q{i}", "job_type": "bid_split", "target_id": "f1", "status": "failed",
                        "error_kind": "unknown", "created_at": f"2026-09-30T00:00:0{i}+00:00"} for i in range(3)])
    monkeypatch.setattr(bid_split, "get_supabase", lambda: db)
    queued.clear()
    out = rfp_split.advance(db, dict(_hv_of(db)), settings=_settings())
    assert out.state == "complete" and queued == []                       # done_with_errors: partial
    f1 = next(f for f in db.tables["bid_split_files"] if f["id"] == "f1")
    assert f1["status"] == "failed" and f1["error"] == "The split was interrupted (the server restarted). Run it again."
    assert db.tables["bid_split_jobs"][0]["status"] == "done_with_errors"


def test_a_live_job_is_never_reaped(monkeypatch):
    # A queue run still active for the open file (a slow 600-page set).
    db = _dead_db(llm=[{"id": "q", "job_type": "bid_split", "target_id": "f1", "status": "running"}])
    monkeypatch.setattr(bid_split, "get_supabase", lambda: db)
    assert rfp_split.advance(db, dict(_hv_of(db)), settings=_settings()).waiting
    # The job or the file touched inside the window.
    for over in ({"job_age": 60}, {"file_age": 60}):
        db = _dead_db(**over)
        assert rfp_split.advance(db, dict(_hv_of(db)), settings=_settings()).waiting
        assert db.tables["bid_split_files"][1]["status"] == "pending"
    # A fresh staging claim on the harvest may be feeding it.
    db = _dead_db()
    db.tables["rfp_harvests"][0].update(split_status="pending", split_started_at=_ago(5))
    assert rfp_split.dead_job_ids(db, db.tables["bid_split_jobs"], {JOB: db.tables["bid_split_files"]},
                                  {JOB: _hv_of(db)}, settings=_settings()) == set()
    # Missing stamps are never read as dead.
    job = dict(db.tables["bid_split_jobs"][0], updated_at=None)
    assert rfp_split.dead_job_ids(db, [job], {}, {}, settings=_settings()) == set()


def test_a_dead_linked_job_flags_interrupted_and_run_queues_it_again(env, monkeypatch):
    monkeypatch.setattr(bid_split, "get_supabase", lambda: env.db)
    env.db.tables["bid_split_jobs"] = [{"id": JOB, "status": "processing", "project_id": P1, "source": "rfp",
                                        "created_by": None, "updated_at": _ago(900)}]
    env.db.tables["bid_split_files"] = [
        {**f, "updated_at": _ago(900), "page_count": 10, "error": None} for f in _files("done", "pending")
    ]
    env.db.tables["bid_split_segments"] = []
    _hv(env).update(split_status="running", split_job_id=JOB, split_error=None, split_started_at=_ago(900))
    rec = env.db.tables["rfp_created_projects"][0]
    issue = rfp_split.issues_for_records(env.db, [rec])[P1]
    assert issue["state"] == "failed" and issue["reason"] == rfp_split._MSG_JOB_INTERRUPTED
    assert rfp_split.split_running(env.db, rec) is False
    out = rr.run_rfp_created_split(P1, BackgroundTasks(), user=_user())
    assert out["mode"] == "requeue" and env.queued == [("f1", "u1")]
    assert _hv(env)["split_status"] == "running"


def test_an_interrupted_manual_staging_reads_interrupted_within_minutes(env):
    rec = env.db.tables["rfp_created_projects"][0]
    _hv(env).update(split_status="pending", split_error=None, split_started_at=_ago(200))
    assert rfp_split.issues_for_records(env.db, [rec])[P1]["state"] == "running"
    _hv(env).update(split_started_at=_ago(360))
    issue = rfp_split.issues_for_records(env.db, [rec])[P1]
    assert issue["state"] == "failed" and issue["reason"] == rfp_split._MSG_RUN_INTERRUPTED


def test_the_manual_staging_stands_down_when_its_claim_moved(env, monkeypatch):
    _hv(env).update(split_status="pending", split_started_at="2026-09-30T18:00:00+00:00")
    stale_copy = dict(_hv(env), split_started_at="2026-09-30T17:00:00+00:00")
    monkeypatch.setattr(rfp_split, "_harvest_full", lambda sb, hid: dict(stale_copy))
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: env.db)
    called = []
    monkeypatch.setattr(rfp_split, "_start", lambda *a, **k: called.append(1) or (JOB, 1))
    rfp_split.stage_for_project(P1, "u1")
    assert called == [] and _hv(env)["split_status"] == "pending"


def test_the_portal_split_step_waits_on_our_own_gate(monkeypatch):
    settings = _settings(rfp_email_ingestion_classify_max_attempts=4)

    def busy(*a, **k):
        try:
            raise llm_gate.LlmBusy("gate full")
        except llm_gate.LlmBusy as inner:
            raise RuntimeError("staging could not call the model") from inner

    monkeypatch.setattr(portal.rfp_split, "advance", busy)
    db = FakeDB({"rfp_portal_invitations": [{"id": "inv-1", "portal": "ngem", "status": "split", "harvest_id": HV,
                                             "title": "Plumas St", "attempts": 3, "last_error": None,
                                             "next_attempt_at": None, "flag_reason": None}],
                 "rfp_harvests": [_harvest()]})
    assert portal._step_split(db, dict(db.tables["rfp_portal_invitations"][0]), settings) is False
    row = db.tables["rfp_portal_invitations"][0]
    assert row["status"] == "split" and row["attempts"] == 3 and row["next_attempt_at"]
    assert db.tables["rfp_harvests"][0]["split_status"] == "none"     # no give-up at the cap


def test_the_email_split_step_hands_the_lease_renewal_to_the_staging(monkeypatch):
    monkeypatch.setattr(ingest, "get_settings", lambda: _settings())
    seen = []
    monkeypatch.setattr(ingest.rfp_split, "advance",
                        lambda sb, hv, **kw: seen.append(kw.get("renew")) or rfp_split.Outcome("waiting"))
    row = {"id": "e1", "status": "split", "harvest_id": HV, "attempts": 0, "flag_reason": None,
           "subject": "ITB", "test_session_id": None}
    db = FakeDB({"rfp_emails": [row], "rfp_harvests": [_harvest()]})

    def renew():
        return True

    ingest._step_split(db, dict(row), renew=renew)
    assert seen == [renew]


# ── 5. Review 2026-09-30: fixes over the in-flight build ─────────────────────


def test_a_heartbeat_whose_answer_was_lost_does_not_lose_the_claim():
    """The beat's write landed but the connection dropped before the answer
    (the SSL bad-record-mac drops seen on dev): the database holds the new
    stamp, so the next beat's CAS on the old one misses. The claim tries the
    stamp it may have written before calling itself lost."""
    db = FakeDB({"rfp_harvests": [_harvest(split_status="pending", split_started_at=OLD)]})
    claim = rfp_split._StagingClaim(db, HV, OLD, _settings())
    real = rfp_split._cas_claim
    calls = {"n": 0}

    def flaky(sb, hid, expected, stamp, fields):
        calls["n"] += 1
        if calls["n"] == 1:
            real(sb, hid, expected, stamp, fields)     # lands ...
            raise RuntimeError("SSLV3_ALERT_BAD_RECORD_MAC")   # ... the answer is lost
        return real(sb, hid, expected, stamp, fields)

    rfp_split._cas_claim, saved = flaky, rfp_split._cas_claim
    try:
        assert claim.beat(force=True) is True          # a hiccup, not a lost claim
        assert claim.beat(force=True) is True          # the landed stamp is still ours
        assert claim.lost is False and _hv_of(db)["split_started_at"] == claim.stamp
        # The put-back after a failure matches too.
        assert claim.cas({"split_status": "none"}) is True
    finally:
        rfp_split._cas_claim = saved


def test_the_claim_keeps_beating_while_the_workers_drain_after_a_failure(monkeypatch):
    db, stage = _stage_env(monkeypatch, n=2, delay=0.0, fail="S0.pdf")
    beats_after_failure = []
    failed_at = {}

    s1_started = threading.Event()

    def gate(name):
        if name == "S0.pdf":
            s1_started.wait(2)       # fail only once S1 is in flight
        if name == "S1.pdf":
            s1_started.set()
            time.sleep(0.3)          # a long upload still in flight after S0 failed

    stage.gate = gate
    claim = rfp_split._StagingClaim(db, HV, None, _settings())
    claim.interval = 0.02
    real_beat = claim.beat

    def beat(**kw):
        if "t" not in failed_at and any(n == "S0.pdf" for n in stage.fetched):
            failed_at["t"] = time.monotonic()
        if "t" in failed_at:
            beats_after_failure.append(time.monotonic())
        return real_beat(**kw)

    claim.beat = beat
    hv = _hv_of(db)
    with pytest.raises(rcf.RfpCreateFilesTransient):
        rfp_split._start(db, hv, rfp_split._entries(hv), _settings(rfp_split_stage_concurrency=2), {}, None, None,
                         claim=claim)
    assert len(beats_after_failure) >= 3                  # the claim stayed fresh while S1 drained
    assert db.tables["bid_split_jobs"] == []


def test_a_claim_lost_during_the_enqueue_discards_the_job(monkeypatch):
    db, stage = _stage_env(monkeypatch, n=3, delay=0.0)
    claim = rfp_split._StagingClaim(db, HV, None, _settings())
    claim.interval = 0.0
    real_enqueue = rfp_split.llm_queue.enqueue

    def enqueue(jt, **kw):
        real_enqueue(jt, **kw)
        _hv_of(db).update(split_started_at="2026-09-30T18:00:00+00:00")   # taken over

    monkeypatch.setattr(rfp_split.llm_queue, "enqueue", enqueue)
    hv = _hv_of(db)
    with pytest.raises(rfp_split.StagingClaimLost):
        rfp_split._start(db, hv, rfp_split._entries(hv), _settings(), {}, None, None, claim=claim)
    assert db.tables["bid_split_jobs"] == [] and "discard" in stage.timeline
    assert sum(1 for t in stage.timeline if t.startswith("queue:")) == 1   # nothing more queued
    assert _hv_of(db)["split_status"] == "pending" and _hv_of(db).get("split_job_id") is None


def test_the_split_claim_refuses_a_harvest_that_just_got_its_project(monkeypatch):
    """The late-link race, split side at the claim: the row read the harvest
    before creation linked its project; the claim is fenced on project_id
    and the step skips `linked` instead of staging a job nobody links."""
    db = FakeDB({"rfp_harvests": [_harvest(split_status="none", project_id=P1)], "bid_split_jobs": []})
    started = []
    monkeypatch.setattr(rfp_split, "_start", lambda *a, **k: started.append(1) or (JOB, 1))
    out = rfp_split.advance(db, dict(_hv_of(db), project_id=None), settings=_settings())
    assert out.state == "skipped" and out.reason == "linked" and started == []
    assert _hv_of(db)["split_status"] == "skipped"


def test_a_split_staged_while_the_project_was_created_links_itself(monkeypatch):
    """The late-link race, split side after staging: creation made the
    project (and read the harvest before it pointed at this job) while the
    mate row staged. `_start` re-reads the project after pointing the
    harvest at the job, links the job, re-files any file already done, and
    refreshes the created record's copy."""
    db, stage = _stage_env(monkeypatch, n=2, delay=0.0)
    db.tables["rfp_created_projects"] = [{"project_id": P1, "harvest_id": HV, "split_status": None}]

    def gate(name):
        if name == "S1.pdf":
            _hv_of(db)["project_id"] = P1       # creation finished meanwhile

    stage.gate = gate
    resynced = []
    monkeypatch.setattr(rfp_split, "resync_after_run", resynced.append)
    real_enqueue = rfp_split.llm_queue.enqueue

    def enqueue(jt, **kw):
        real_enqueue(jt, **kw)
        if kw["target_id"] == "row-S0.pdf":     # a fast file finished before the link
            next(f for f in db.tables["bid_split_files"] if f["id"] == "row-S0.pdf")["status"] = "done"

    monkeypatch.setattr(rfp_split.llm_queue, "enqueue", enqueue)
    hv = _hv_of(db)
    job_id, staged = rfp_split._start(db, hv, rfp_split._entries(hv), _settings(), {}, None, None)
    job = next(j for j in db.tables["bid_split_jobs"] if j["id"] == job_id)
    assert job["project_id"] == P1
    assert resynced == ["row-S0.pdf"]
    assert db.tables["rfp_created_projects"][0]["split_status"] == "running"
    assert db.tables["rfp_created_projects"][0]["split_job_id"] == job_id
    # No project: nothing is linked.
    db, stage = _stage_env(monkeypatch, n=1, delay=0.0)
    job_id, _ = rfp_split._start(db, _hv_of(db), rfp_split._entries(_hv_of(db)), _settings(), {}, None, None)
    assert next(j for j in db.tables["bid_split_jobs"] if j["id"] == job_id).get("project_id") is None


def test_creation_links_a_split_job_that_appeared_after_its_harvest_read(monkeypatch):
    """The late-link race, creation side: the harvest creation read had no
    job; the split pointed the harvest at one before the project link
    landed. `_attach_harvest` re-reads the pointer after its own write."""
    monkeypatch.setattr(rfp_create, "link_harvest_mates", lambda *a, **k: 0)
    db = FakeDB({
        "rfp_harvests": [_harvest(split_status="running", split_job_id=JOB, create_claim_token="t")],
        "bid_split_jobs": [{"id": JOB, "status": "processing", "project_id": None, "source": "rfp"}],
    })
    source = rfp_create._Source(kind=rfp_create.SOURCE_EMAIL, table="rfp_emails", row={"id": "e1"},
                                expected_status="create")
    stale = _harvest(split_status="none", split_job_id=None)
    rfp_create._attach_harvest(db, source, stale, "t", P1)
    assert db.tables["bid_split_jobs"][0]["project_id"] == P1
    assert _hv_of(db)["project_id"] == P1


def test_creation_does_not_wait_on_a_row_nothing_visits(monkeypatch):
    """A mate row frozen at `split` (an ended test session, a sweep switched
    off) would hold creation forever: only a row a sweep is still visiting
    (its next visit scheduled ahead, or touched recently) holds it."""
    s = _settings()
    me = ("rfp_emails", "e-copy")
    db = _wait_db(mates=["split"], split_status="none")
    mate = db.tables["rfp_emails"][1]
    mate.update(next_attempt_at=_ago(3600), updated_at=_ago(3600))
    assert not rfp_split.creation_must_wait(db, _hv_of(db), settings=s, exclude=me)
    mate.update(next_attempt_at=_ago(-20))                  # parked for its next poll
    assert rfp_split.creation_must_wait(db, _hv_of(db), settings=s, exclude=me)
    mate.update(next_attempt_at=_ago(30), updated_at=_ago(60))   # due now, the sweep is on its way
    assert rfp_split.creation_must_wait(db, _hv_of(db), settings=s, exclude=me)

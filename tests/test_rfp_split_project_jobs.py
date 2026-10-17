"""The Bid File Splitter on a project's missing-files drop job
(`bid_split_jobs.source = 'project'`, docs/RFP_MISSING_FILES.md 5): the
split side of the slice, against the in-memory fakes.

Pinned:

- `project_original`: a drop job's `fetch_original` answers the job's own
  stored source PDF (a Skip when it is gone);
- `resync_after_run` on a `project` job: a cut file files one row per
  segment plus the untouched source kept once as the source set, an
  intact file one row under the splitter's category; every row carries
  the splitter provenance and no harvest; a segment resolves the item its
  SOURCE name matches; nothing is re-filed once the package has sent;
- the splitter router: the delete refusal and the correction lock honour
  SOURCE_PROJECT, and a correction re-files the project through the
  project branch;
- the splitter page's own upload still validates and stages through the
  shared `bid_split.check_upload` / `stage_upload`.
"""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile

from app.services import bid_split, files_needed, office_preview, rfp_split, storage
from app.services import rfp_create_files as rcf
from app.services import rfp_file_issues as rfi
from tests.test_rfp_email_ingest import FakeDB

P1 = "5c000000-0000-4000-8000-000000000001"
JOB = "6d000000-0000-4000-8000-000000000001"
FILE = "7e000000-0000-4000-8000-000000000001"
SOURCE_PATH = f"bid-splits/{JOB}/source/abc-Bid Set.pdf"
DROPPED = b"%PDF-the dropped bytes"


def _pdf(pages: int = 1) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _seg(sid, category, name, order, *, original=False):
    return {
        "id": sid, "file_id": FILE, "sort_order": order, "category": category, "other_type": None,
        "name": name, "description": f"{name}.", "page_start": order + 1, "page_end": order + 1,
        "storage_path": SOURCE_PATH if original else f"bid-splits/{JOB}/output/{sid}.pdf",
        "filename": f"{name}.pdf", "size_bytes": 10, "is_original": original,
    }


def _db(segments, **tables):
    base = {
        "bid_split_jobs": [{"id": JOB, "status": "done", "source": rfp_split.SOURCE_PROJECT, "project_id": P1,
                            "rfp_harvest_id": None, "created_by": "u1"}],
        "bid_split_files": [{"id": FILE, "job_id": JOB, "filename": "Bid Set.pdf", "storage_path": SOURCE_PATH,
                             "size_bytes": len(DROPPED), "page_count": 2, "status": "done", "rfp_sandbox_file_id": None,
                             "source_format": None, "classified_from": None}],
        "bid_split_segments": segments,
        "project_files": [],
        "rfp_created_projects": [{"project_id": P1, "harvest_id": None, "files_issue_total": False,
                                  "files_promoted": 0}],
        "rfp_file_issues": [{"id": "i-1", "project_id": P1, "kind": "file", "group": "not_downloaded",
                             "name": "Folder/Bid Set.pdf", "status": "download_failed", "item_key": "k1"},
                            {"id": "i-2", "project_id": P1, "kind": "file", "group": "refused",
                             "name": "Other.pdf", "status": "rejected", "item_key": "k2"}],
        "projects": [],
    }
    base.update(tables)
    return FakeDB(base)


@pytest.fixture
def env(monkeypatch):
    from app.core import supabase_client
    from app.routers import files as files_router

    state = {"db": None, "sent": False, "downloads": [], "copies": [], "uploads": []}
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: state["db"])
    monkeypatch.setattr(files_router, "handoff_locked", lambda pid: state["sent"])
    monkeypatch.setattr(rfp_split, "audit", lambda *a, **k: None)
    monkeypatch.setattr(rfi, "audit", lambda *a, **k: None)
    monkeypatch.setattr(files_needed, "audit", lambda *a, **k: None)
    monkeypatch.setattr(office_preview, "is_convertible", lambda *a, **k: False)
    monkeypatch.setattr(storage, "copy_file", lambda src, dst: state["copies"].append((src, dst)))
    monkeypatch.setattr(storage, "upload_file", lambda path, data, ct, **k: state["uploads"].append((path, ct)))
    monkeypatch.setattr(storage, "delete_file", lambda *a, **k: None)

    def download(path):
        state["downloads"].append(path)
        return DROPPED

    monkeypatch.setattr(storage, "download_file", download)
    return state


def test_project_original_answers_the_jobs_stored_source(env):
    decision, data = rfp_split.project_original({"storage_path": SOURCE_PATH, "filename": "Bid Set.pdf"})
    assert isinstance(decision, rcf.Promote) and data == b"%PDF-the dropped bytes"
    assert decision.filename == "Bid Set.pdf" and decision.content_type == "application/pdf"
    assert decision.path == SOURCE_PATH and decision.note is None and env["downloads"] == [SOURCE_PATH]
    assert rfp_split.project_original({"storage_path": None, "filename": "x.pdf"}) == rcf.Skip("no_source_object")


def test_project_original_skips_a_missing_object(monkeypatch):
    def gone(path):
        raise RuntimeError("Object not found")

    monkeypatch.setattr(storage, "download_file", gone)
    assert rfp_split.project_original({"storage_path": SOURCE_PATH, "filename": "x.pdf"}) == rcf.Skip(
        "missing_in_storage")


def test_resync_after_run_files_a_cut_project_job_by_trade(env):
    db = _db([_seg("s-g", "general_drawings", "Cover Sheets", 0), _seg("s-e", "electrical_drawings", "E-Sheets", 1)])
    env["db"] = db
    rfp_split.resync_after_run(FILE)
    rows = {r["filename"]: r for r in db.tables["project_files"]}
    assert set(rows) == {"Cover Sheets.pdf", "E-Sheets.pdf", "Bid Set.pdf"}
    assert rows["Cover Sheets.pdf"]["category"] == "drawing"
    assert rows["E-Sheets.pdf"]["category"] == "electrical_drawing"
    source = rows["Bid Set.pdf"]
    assert source["category"] == "other" and source["is_source_set"] is True
    assert source["note"] == "Source set: split into 2 documents" and source["rfp_sandbox_file_id"] is None
    for row in rows.values():
        assert row["project_id"] == P1 and row["rfp_harvest_id"] is None and row["bid_split_file_id"] == FILE
    assert rows["E-Sheets.pdf"]["bid_split_segment_id"] == "s-e"
    assert env["downloads"] == [SOURCE_PATH]       # the source set came from the job's own object
    # The item named after the dropped PDF resolved through a segment's source name.
    items = {r["id"]: r for r in db.tables["rfp_file_issues"]}
    assert items["i-1"]["resolved_how"] == "name_match" and items["i-2"].get("resolved_at") is None
    # The promotion's count is the promotion's: untouched.
    assert db.tables["rfp_created_projects"][0]["files_promoted"] == 0
    # A second run on the same cut is a no-op (idempotent): the source set is
    # recognised by its name and size and kept in place, never uploaded again.
    ids = {r["id"] for r in db.tables["project_files"]}
    rfp_split.resync_after_run(FILE)
    assert {r["id"] for r in db.tables["project_files"]} == ids and env["downloads"] == [SOURCE_PATH]
    # A correction to "intact": the source set becomes the intact file in place.
    db.tables["bid_split_segments"] = [_seg("s-1", "specifications", "Project Manual", 0, original=True)]
    rfp_split.resync_after_run(FILE)
    [row] = db.tables["project_files"]
    assert row["id"] == source["id"] and row["category"] == "specification" and row["is_source_set"] is False
    assert row["bid_split_segment_id"] == "s-1" and env["downloads"] == [SOURCE_PATH]


def test_resync_after_run_files_an_intact_project_job_under_its_category(env):
    db = _db([_seg("s-1", "specifications", "Project Manual", 0, original=True)])
    env["db"] = db
    rfp_split.resync_after_run(FILE)
    [row] = db.tables["project_files"]
    assert row["category"] == "specification" and row["filename"] == "Bid Set.pdf"
    assert row["bid_split_segment_id"] == "s-1" and row["is_source_set"] is False
    assert row["rfp_harvest_id"] is None and row["rfp_sandbox_file_id"] is None


def _whole_row(db):
    [row] = db.tables["project_files"]
    assert row["bid_split_file_id"] == FILE and row["bid_split_segment_id"] is None
    assert row["filename"] == "Bid Set.pdf" and row["uploaded_by"] == "u1" and row["is_source_set"] is False
    assert row["size_bytes"] == len(DROPPED) and row["rfp_harvest_id"] is None
    return row


def test_a_split_that_finishes_after_the_package_sent_files_the_drop_whole(env):
    """Review finding 1, path 2: the run ends after the hand-off; the
    segments are never filed by trade, so the dropped PDF lands whole under
    Additional files instead of being lost."""
    db = _db([_seg("s-g", "general_drawings", "Cover Sheets", 0), _seg("s-e", "electrical_drawings", "E-Sheets", 1)])
    env["db"] = db
    env["sent"] = True
    rfp_split.resync_after_run(FILE)
    row = _whole_row(db)
    assert row["category"] == "additional"
    assert row["note"] == rfp_split.MSG_WHOLE_SENT == (
        "The Bid File Splitter split this file, but the package had already been sent when the split "
        "finished, so it was added whole under Additional files.")
    # Idempotent: a second pass files nothing more.
    rfp_split.resync_after_run(FILE)
    assert len(db.tables["project_files"]) == 1


def test_a_failed_run_files_the_drop_whole(env, monkeypatch):
    """Path 1: the file's run ends `failed` (the queue's mark, the inline
    fallback's): the same hook files it whole, by its name's category."""
    db = _db([])
    db.tables["bid_split_files"][0]["filename"] = "Project Manual.pdf"
    env["db"] = db
    monkeypatch.setattr(bid_split, "get_supabase", lambda: db)
    monkeypatch.setattr(bid_split, "_notify_job_finished", lambda *a, **k: None)
    bid_split._mark(FILE, status="failed", error="The PDF has 700 pages; the splitter limit is 600 pages per file.")
    [row] = db.tables["project_files"]
    assert row["category"] == "specification" and row["filename"] == "Project Manual.pdf"
    assert row["note"] == ("The Bid File Splitter could not split this file (the PDF has 700 pages; the splitter "
                           "limit is 600 pages per file); it was added whole.")
    assert db.tables["bid_split_files"][0]["status"] == "failed"
    # Once the package has sent, a failed run lands under Additional files.
    db2 = _db([])
    env["db"] = db2
    env["sent"] = True
    monkeypatch.setattr(bid_split, "get_supabase", lambda: db2)
    bid_split._mark(FILE, status="failed", error=None)
    [row] = db2.tables["project_files"]
    assert row["category"] == "additional" and row["note"] == (
        "The Bid File Splitter could not split this file (the run failed); it was added whole under "
        "Additional files because the package had already been sent.")


def test_a_failed_run_on_another_job_files_nothing(env, monkeypatch):
    db = _db([])
    db.tables["bid_split_jobs"][0]["source"] = "rfp"
    env["db"] = db
    monkeypatch.setattr(bid_split, "get_supabase", lambda: db)
    monkeypatch.setattr(bid_split, "_notify_job_finished", lambda *a, **k: None)
    bid_split._mark(FILE, status="failed", error="x")
    assert db.tables["project_files"] == []


def test_a_re_file_that_raises_files_the_drop_whole(env, monkeypatch):
    """Path 3: the re-file itself raised (storage trouble copying a
    segment)."""
    db = _db([_seg("s-g", "general_drawings", "Cover Sheets", 0), _seg("s-e", "electrical_drawings", "E-Sheets", 1)])
    env["db"] = db

    def broken(src, dst):
        raise RuntimeError("storage down")

    monkeypatch.setattr(storage, "copy_file", broken)
    rfp_split.resync_after_run(FILE)
    row = _whole_row(db)
    assert row["category"] == "drawing"
    assert row["note"] == ("The Bid File Splitter could not split this file (its documents could not be filed); "
                           "it was added whole.")
    # The next successful re-file keeps that row as the source set, in place.
    monkeypatch.setattr(storage, "copy_file", lambda src, dst: env["copies"].append((src, dst)))
    rfp_split.resync_after_run(FILE)
    rows = {r["filename"]: r for r in db.tables["project_files"]}
    assert set(rows) == {"Bid Set.pdf", "Cover Sheets.pdf", "E-Sheets.pdf"}
    assert rows["Bid Set.pdf"]["id"] == row["id"] and rows["Bid Set.pdf"]["is_source_set"] is True
    assert rows["Bid Set.pdf"]["category"] == "other"


def _shape(db):
    return sorted((r["filename"], r["category"], bool(r.get("is_source_set"))) for r in db.tables["project_files"])


CONVERGED = [("Bid Set.pdf", "other", True), ("Cover Sheets.pdf", "drawing", False),
             ("E-Sheets.pdf", "electrical_drawing", False)]


def test_a_re_file_that_fails_partway_keeps_the_original_as_the_source_set(env, monkeypatch):
    """Review N1, scenario A: the second segment's copy fails once. The
    fallback must not file the PDF whole as a drawing beside the segment
    that landed; it keeps it as the source set, and the next successful
    re-file converges (one source set, the segments, nothing whole)."""
    db = _db([_seg("s-g", "general_drawings", "Cover Sheets", 0), _seg("s-e", "electrical_drawings", "E-Sheets", 1)])
    env["db"] = db
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("transient storage error")
        env["copies"].append((src, dst))

    monkeypatch.setattr(storage, "copy_file", flaky)
    rfp_split.resync_after_run(FILE)
    assert _shape(db) == [("Bid Set.pdf", "other", True), ("Cover Sheets.pdf", "drawing", False)]
    source = next(r for r in db.tables["project_files"] if r["is_source_set"])
    assert source["note"] == "Source set: split into 2 documents" and source["bid_split_segment_id"] is None
    rfp_split.resync_after_run(FILE)
    assert _shape(db) == CONVERGED
    assert next(r for r in db.tables["project_files"] if r["is_source_set"])["id"] == source["id"]


def test_a_source_set_upload_that_fails_once_still_converges(env, monkeypatch):
    """Review N1, scenario B: every segment landed, then the source-set
    upload failed once. The fallback files the original as the source set
    (`other`), never whole in `drawing` beside its own segments."""
    db = _db([_seg("s-g", "general_drawings", "Cover Sheets", 0), _seg("s-e", "electrical_drawings", "E-Sheets", 1)])
    env["db"] = db
    calls = {"n": 0}

    def flaky_upload(path, data, ct, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient storage error")
        env["uploads"].append((path, ct))

    monkeypatch.setattr(storage, "upload_file", flaky_upload)
    rfp_split.resync_after_run(FILE)
    assert _shape(db) == CONVERGED
    assert not any(r["category"] in ("drawing", "specification", "additional") and r["filename"] == "Bid Set.pdf"
                   for r in db.tables["project_files"])
    ids = {r["id"] for r in db.tables["project_files"]}
    rfp_split.resync_after_run(FILE)
    assert _shape(db) == CONVERGED and {r["id"] for r in db.tables["project_files"]} == ids


def test_a_whole_filing_is_a_system_insert_for_the_first_file_rule(env):
    """`by_person=False`: a whole filing never clears the unknown-content
    items, while a person's drop job segments may."""
    db = _db([])
    db.tables["rfp_created_projects"][0].update(files_issue_total=True, files_promoted=0)
    db.tables["rfp_file_issues"].append({"id": "i-3", "project_id": P1, "kind": "link", "group": "not_downloaded",
                                         "name": "SharePoint", "status": "needs_sign_in", "item_key": "k3"})
    env["db"] = db
    rfp_split.file_project_job_whole(db, FILE, "the run failed", package_sent=False)
    link = next(r for r in db.tables["rfp_file_issues"] if r["id"] == "i-3")
    assert link.get("resolved_at") is None
    # The segments of the same person's drop count as the person's insert.
    db = _db([_seg("s-g", "general_drawings", "Cover Sheets", 0), _seg("s-e", "electrical_drawings", "E-Sheets", 1)])
    db.tables["rfp_created_projects"][0].update(files_issue_total=True, files_promoted=0)
    db.tables["rfp_file_issues"].append({"id": "i-3", "project_id": P1, "kind": "link", "group": "not_downloaded",
                                         "name": "SharePoint", "status": "needs_sign_in", "item_key": "k3"})
    env["db"] = db
    rfp_split.resync_after_run(FILE)
    link = next(r for r in db.tables["rfp_file_issues"] if r["id"] == "i-3")
    assert link["resolved_how"] == "auto_first_file"
    # The named file items never clear that way (they cleared by name here).
    assert next(r for r in db.tables["rfp_file_issues"] if r["id"] == "i-2").get("resolved_at") is None


def test_a_manual_job_without_a_project_is_never_re_filed(env):
    db = _db([_seg("s-1", "specifications", "Project Manual", 0, original=True)])
    db.tables["bid_split_jobs"][0].update(source="manual", project_id=None)
    env["db"] = db
    assert rfp_split.resync_project_files(db, FILE) is None
    assert db.tables["project_files"] == []


# ── The splitter router ──────────────────────────────────────────────────


@pytest.fixture
def router_env(monkeypatch):
    from app.routers import bid_splitter as bs
    from tests import test_bid_split_corrections as tc

    db = tc.FakeDB(tc._tables(bid_split_jobs=[{"id": "j1", "model": "m", "status": "done",
                                                "source": rfp_split.SOURCE_PROJECT, "project_id": P1,
                                                "rfp_harvest_id": None}]))
    calls = tc._install(monkeypatch, db)
    resyncs = []
    real_resync = rfp_split.resync_project_files
    monkeypatch.setattr(bs.rfp_split, "resync_project_files", lambda sb, fid, *, actor_id=None: (
        resyncs.append((fid, actor_id)) or rfp_split.PromoteResult(used_split=True, documents=1)))
    monkeypatch.setattr(bs.bid_split_training, "capture_correction", lambda *a, **k: None)
    monkeypatch.setattr(bs.llm, "is_configured", lambda *a, **k: True)
    monkeypatch.setattr(bs, "_dispatch", lambda *a, **k: None)
    return SimpleNamespace(db=db, calls=calls, resyncs=resyncs, bs=bs, tc=tc, real_resync=real_resync)


def test_a_project_job_with_a_project_cannot_be_deleted(router_env, monkeypatch):
    bs, tc = router_env.bs, router_env.tc
    with pytest.raises(HTTPException) as exc:
        bs.delete_job("j1", user=tc._WRITER)
    assert exc.value.status_code == 409 and exc.value.detail == rfp_split.MSG_PROJECT_JOB_HAS_PROJECT
    swept = []
    monkeypatch.setattr(bs.storage, "delete_bid_split_prefix", swept.append)
    router_env.db.tables["bid_split_jobs"][0]["project_id"] = None   # the project was discarded
    bs.delete_job("j1", user=tc._WRITER)
    assert swept == ["j1"] and router_env.db.tables["bid_split_jobs"] == []


def test_the_correction_lock_and_the_re_file_honour_a_project_job(router_env, monkeypatch):
    bs, tc = router_env.bs, router_env.tc
    monkeypatch.setattr(bs, "handoff_locked", lambda pid: pid == P1)
    with pytest.raises(HTTPException) as exc:
        bs.correct_file_kind("f1", bs.BidSplitFileKindIn(file_kind="rfp"), user=tc._WRITER)
    assert exc.value.status_code == 409 and exc.value.detail == rfp_split.MSG_PACKAGE_SENT
    assert router_env.resyncs == []
    monkeypatch.setattr(bs, "handoff_locked", lambda pid: False)
    out = bs.correct_file_kind("f1", bs.BidSplitFileKindIn(file_kind="rfp"), user=tc._WRITER)
    assert router_env.resyncs == [("f1", "u1")] and out["project_resync"]["ok"] is True


def test_the_correction_re_file_takes_the_project_branch(router_env, monkeypatch):
    """The real resync on a corrected project-job file: no harvest is read,
    the project branch files the intact file under its new category."""
    bs = router_env.bs
    db = router_env.db
    db.tables.setdefault("project_files", [])
    db.tables.setdefault("rfp_created_projects", [])
    calls = []
    monkeypatch.setattr(rfp_split, "_resync_project_job",
                        lambda sb, split_file, job, *, actor_id: calls.append((split_file["id"], job["source"]))
                        or rfp_split.PromoteResult(used_split=True))
    router_env.real_resync(db, "f1", actor_id="u1")
    assert calls == [("f1", "project")]
    assert bs.rfp_split.SOURCE_PROJECT == "project"


# ── The splitter page's upload through the shared staging ──────────────────


def test_a_job_with_no_files_reads_without_a_bad_query():
    """The smoke's 500: `_attach_segments` sent `in.("-")` for a job with no
    files, which Postgres refuses (not a uuid). No ids, no query."""
    from app.routers import bid_splitter as bs

    class NoSegments:
        def table(self, name):
            raise AssertionError(f"no read expected, got {name}")

    assert bs._attach_segments(NoSegments(), []) == []
    rows = [{"id": None}]
    assert bs._attach_segments(NoSegments(), rows) == [{"id": None, "segments": []}]


def test_the_splitter_upload_still_validates_and_stages(monkeypatch):
    from app.routers import bid_splitter as bs

    db = FakeDB({"bid_split_files": [], "bid_split_jobs": [{"id": "j9", "file_count": 1}]})
    stored = []
    dispatched = []
    monkeypatch.setattr(bs, "get_supabase", lambda: db)
    monkeypatch.setattr(bs, "_job_or_404", lambda _sb, job_id: {"id": job_id})
    monkeypatch.setattr(storage, "upload_file", lambda path, data, ct, **k: stored.append((path, ct)))
    monkeypatch.setattr(bs, "_dispatch", lambda bg, fid, uid, forced_kind=None: dispatched.append((fid, uid)))
    upload = UploadFile(io.BytesIO(_pdf(2)), filename="Set.pdf", size=len(_pdf(2)))
    frow = asyncio.run(bs.upload_job_file("j9", BackgroundTasks(), file=upload, user=SimpleNamespace(id="u1")))
    assert frow["filename"] == "Set.pdf" and frow["page_count"] == 2 and frow["status"] == "pending"
    assert stored[0][1] == "application/pdf" and stored[0][0].startswith("bid-splits/j9/source/")
    assert db.tables["bid_split_jobs"][0]["file_count"] == 1 and dispatched == [(frow["id"], "u1")]
    # The shared validation: the page cap answers 413 with the splitter's sentence.
    monkeypatch.setattr(bid_split, "get_settings", lambda: SimpleNamespace(bid_split_max_pages_per_file=1))
    with pytest.raises(bid_split.UploadRefused) as exc:
        bid_split.check_upload("Set.pdf", _pdf(2))
    assert exc.value.status_code == 413 and str(exc.value) == "Set.pdf: 2 pages exceeds the 1-page limit per file"
    with pytest.raises(bid_split.UploadRefused) as exc:
        bid_split.check_upload("notes.txt", b"hello")
    assert exc.value.status_code == 415 and str(exc.value) == "notes.txt: the splitter accepts PDF files only"

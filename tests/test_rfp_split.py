"""The Bid File Splitter step of RFP Ingestion (app/services/rfp_split,
docs/RFP_SPLIT.md sections 2, 3, 4 and 7) against the in-memory fakes.

Pinned:

- the segment -> file_category map (every splitter category maps, every
  target is a category the files router accepts) and the addendum number
  parser;
- `advance`: the flag-off / no-files / linked skips, the claim, the wait
  while the job processes, complete and failed outcomes with their test
  events, a staging failure that puts the claim back; the model-away wait
  (no job staged, no attempt spent) and the requeue of files that died of
  a model outage once the model is back;
- `promote_split_file`: a cut file becomes one row per segment (server-side
  copies) plus the source set kept as `other`, an intact file one row with
  the mapped category; both idempotent on a retry; a re-cut removes the
  stale rows; the promotion job routes `done` split rows through it and
  everything else through the pre-split mapping;
- the two ingest modules' `_step_split`: flag off falls straight through to
  `create`, a wait pushes next_attempt_at without an attempt, an exception
  walks the ladder;
- the splitter router: the sent-package 409 on the three correction routes,
  the resync after a correction on an unsent project, the refusal to delete
  an rfp job whose project exists, non-PDF rows never re-cut;
- the non-PDF name classifier's validation (bogus or failed answers file as
  `other`);
- the test bench's `split` step chip and events, migration 0132's shape,
  the new settings.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import file_categories as fc
from app.core.config import Settings
from app.services import bid_split, office_preview, rfp_split, storage
from app.services import rfp_create_files as rcf
from app.services import rfp_email_ingest as ingest
from app.services import rfp_portal_ingest as portal
from app.services import rfp_test
from tests.test_rfp_email_ingest import FakeDB

# Captured at import, before the autouse fixture stubs it out.
REAL_SANDBOX_BUSY = rfp_split.sandbox_busy

HV = "hv-1"
P1 = "p-1"
JOB = "job-1"


def _settings(**over):
    base = dict(
        rfp_ingest_enabled=True, llm_queue_enabled=True, bid_file_splitter_enabled=True,
        rfp_split_enabled=True, rfp_harvest_enabled=False,
    )
    base.update(over)
    return Settings(_env_file=None, **base)


def _harvest(**over):
    row = {
        "id": HV, "status": "complete", "project_id": None, "split_status": "none", "split_job_id": None,
        "split_error": None, "test_session_id": None,
        "files": [
            {"file_path": "Drawings/SET.pdf", "status": "accepted", "sandbox_file_id": "sf-1", "kind": "drawing"},
            {"file_path": "Specs/Manual.docx", "status": "accepted", "sandbox_file_id": "sf-2"},
            {"file_path": "bad.pdf", "status": "rejected", "sandbox_file_id": None},
        ],
    }
    row.update(over)
    return row


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(rfp_split, "audit", lambda *a, **k: None)
    # The splitter's model is serving unless a test says otherwise, and the
    # sandbox has answered for every entry (the sandbox-wait tests re-enable
    # the real check).
    monkeypatch.setattr(rfp_split, "model_away", lambda *a, **k: None)
    monkeypatch.setattr(rfp_split, "sandbox_busy", lambda *a, **k: None)
    monkeypatch.setattr(office_preview, "is_convertible", lambda *a: False)
    monkeypatch.setattr(rfp_split.storage, "upload_file", lambda *a, **k: None)
    monkeypatch.setattr(rfp_split.storage, "copy_file", lambda *a, **k: None)
    monkeypatch.setattr(rfp_split.storage, "delete_file", lambda *a, **k: None)


# ── 3.1 The category map, the addendum parser, the non-PDF maps ──────────────


def test_every_splitter_category_maps_to_a_file_category_the_router_accepts():
    assert set(rfp_split.SEGMENT_TO_FILE_CATEGORY) == set(bid_split.CATEGORIES)
    for target in rfp_split.SEGMENT_TO_FILE_CATEGORY.values():
        assert target in fc.VALID_CATEGORIES, target
    assert rfp_split.SEGMENT_TO_FILE_CATEGORY["general_drawings"] == "drawing"
    assert rfp_split.SEGMENT_TO_FILE_CATEGORY["electrical_drawings"] == "electrical_drawing"
    assert rfp_split.SEGMENT_TO_FILE_CATEGORY["addenda"] == "addendum"
    assert rfp_split.SEGMENT_TO_FILE_CATEGORY["rfp"] == "rfp"
    # The drawing set the promotion bell and the estimator gate agree on.
    assert rfp_split.DRAWING_FILE_CATEGORIES == set(fc.DRAWING_CATEGORIES)
    assert set(rcf.DRAWING_CATEGORIES) == set(fc.DRAWING_CATEGORIES)
    # Every triage verdict has a segment category, and the drawing set lands as a general drawing.
    for kind in bid_split.FILE_KINDS:
        assert rfp_split.segment_category_for_kind(kind) in bid_split.CATEGORIES
    assert rfp_split.segment_category_for_kind("drawing_set") == "general_drawings"
    assert rfp_split.segment_category_for_kind("mixed") == "other"
    assert rfp_split.file_category_for_segment({"category": "plumbing_drawings"}) == "plumbing_drawing"
    assert rfp_split.file_category_for_segment({"category": "nonsense"}) == "other"


@pytest.mark.parametrize("name, number", [
    ("Addendum 3", "3"),
    ("Addendum No. 2", "2"),
    ("ADDENDUM #12", "12"),
    ("Add. #3", "3"),
    ("ADD-03", "3"),
    ("Addenda 2A", "2A"),
    ("Addendum 0", "0"),
    ("addendum number 7 - revised sheets", "7"),
    ("Addendum", None),
    ("Electrical Drawings", None),
    ("Additional information 5", None),
    ("", None),
    (None, None),
])
def test_addendum_number_parser(name, number):
    assert rfp_split.parse_addendum_number(name) == number


def test_category_fields_carry_the_addendum_number_and_nothing_else():
    assert rfp_split.category_fields("addendum", {"name": "Add. #4"}) == {
        "category": "addendum", "doc_type": None, "addendum_number": "4", "addendum_issued_on": None,
    }
    assert rfp_split.category_fields("addendum", {"name": "Bulletin"})["addendum_number"] is None
    # An intact file whose segment was collapsed to the fallback name still
    # carries its number in its own filename (live drive 2026-09-18).
    assert rfp_split.category_fields("addendum", {"name": "Addenda"}, "4-26-0915 Addendum No. 1 Tracker.pdf")["addendum_number"] == "1"
    assert rfp_split.category_fields("specification", {"name": "Addenda"}, "Addendum 2.pdf")["addendum_number"] is None
    assert rfp_split.category_fields("specification", {"name": "Addendum 3"})["addendum_number"] is None


@pytest.mark.parametrize("answer, expected", [
    ("electrical_drawings", "electrical_drawings"),
    (" Specifications ", "specifications"),
    ("RFP", "rfp"),
    ("drawings", "other"),
    ("electrical_drawing", "other"),
    (None, "other"),
    (42, "other"),
])
def test_name_classifier_answers_are_validated_against_the_vocabulary(answer, expected):
    assert rfp_split.validate_name_category(answer) == expected


def test_source_format_from_the_sandbox_row_else_the_name():
    assert rfp_split.source_format_for({"source_format": "docx"}, "x.docx") == "docx"
    assert rfp_split.source_format_for(None, "photo.JPG") == "image"
    assert rfp_split.source_format_for({"source_format": ""}, "notes.txt") == "other"


def test_identify_by_name_files_bogus_and_failed_answers_as_other(monkeypatch):
    db = FakeDB({"bid_split_files": [{"id": "f1", "job_id": JOB, "filename": "site.jpg", "status": "running",
                                       "input_snapshot": {"name_context": {"path": "Photos/site.jpg",
                                                                            "subject": "ITB: Warehouse"}}}]})
    monkeypatch.setattr(bid_split, "get_supabase", lambda: db)
    monkeypatch.setattr(bid_split.llm, "active_model", lambda *a, **k: "m")
    monkeypatch.setattr(bid_split.llm, "resolve", lambda *a, **k: SimpleNamespace(provider="anthropic"))
    answers = {"reply": {"category": "site_photos", "other_type": "Site Photos", "confidence": 0.9, "reason": "jpg"}}
    prompts = []

    def fake_complete(feature, *, system, messages, **k):
        prompts.append(messages[0]["content"])
        if isinstance(answers["reply"], Exception):
            raise answers["reply"]
        return answers["reply"]

    monkeypatch.setattr(bid_split.llm, "complete_json", fake_complete)
    frow = db.tables["bid_split_files"][0]
    seg, calls, _ms = bid_split._identify_by_name(db, frow, _settings())
    assert seg["category"] == "other" and seg["other_type"] == "Site Photos" and calls == 1
    assert "Photos/site.jpg" in prompts[0] and "ITB: Warehouse" in prompts[0]
    assert frow["file_kind"] == "other" and frow["file_kind_label"] == "Site Photos"
    # A valid category drops the label; the file kind derives from it.
    answers["reply"] = {"category": "electrical_drawings", "other_type": "ignored", "confidence": 0.7, "reason": None}
    seg, _c, _m = bid_split._identify_by_name(db, frow, _settings())
    assert seg["category"] == "electrical_drawings" and seg["other_type"] is None
    assert frow["file_kind"] == "drawing_set"
    # A failed call never fails the run: `other` with a placeholder label.
    answers["reply"] = RuntimeError("provider down")
    seg, _c, _m = bid_split._identify_by_name(db, frow, _settings())
    assert seg["category"] == "other" and seg["other_type"] == "Unidentified file" and seg["confidence"] is None


# ── 3.2 advance ──────────────────────────────────────────────────────────────


@pytest.fixture
def events(monkeypatch):
    out = []

    def rec(sb, **kw):
        out.append(kw)

    monkeypatch.setattr(rfp_split.rfp_test, "record", rec)
    return out


def _hv(db):
    return db.tables["rfp_harvests"][0]


def test_advance_skips_with_the_documented_reasons(events, monkeypatch):
    # No harvest at all.
    db = FakeDB({"rfp_harvests": [_harvest()]})
    out = rfp_split.advance(db, None, settings=_settings())
    assert out.state == "skipped" and out.reason == "no_files"
    # Flags off: the harvest is marked skipped once and the event says why.
    out = rfp_split.advance(db, _hv(db), settings=_settings(rfp_split_enabled=False), test_session_id="s1")
    assert out.state == "skipped" and out.reason == "flags_off"
    assert _hv(db)["split_status"] == "skipped" and _hv(db)["split_error"] == "flags_off"
    assert events[-1]["kind"] == "skipped" and events[-1]["detail"]["reason"] == "flags_off"
    assert events[-1]["source"] == rfp_test.SOURCE_SPLIT and events[-1]["session_id"] == "s1"
    # A terminal status answers itself (no writes).
    out = rfp_split.advance(db, _hv(db), settings=_settings())
    assert out.state == "skipped" and out.reason == "flags_off"
    # The splitter flag alone is not enough.
    db = FakeDB({"rfp_harvests": [_harvest()]})
    assert rfp_split.advance(db, _hv(db), settings=_settings(bid_file_splitter_enabled=False)).reason == "flags_off"
    # Linked: the harvest already has a project.
    db = FakeDB({"rfp_harvests": [_harvest(project_id="p-old")]})
    assert rfp_split.advance(db, _hv(db), settings=_settings()).reason == "linked"
    # Nothing verified in the sandbox.
    db = FakeDB({"rfp_harvests": [_harvest(files=[{"file_path": "x", "status": "rejected"}])]})
    assert rfp_split.advance(db, _hv(db), settings=_settings()).reason == "no_files"
    # The queue off: the splitter cannot run.
    db = FakeDB({"rfp_harvests": [_harvest()]})
    assert rfp_split.advance(db, _hv(db), settings=_settings(llm_queue_enabled=False)).reason == "queue_off"


def test_advance_claims_stages_and_waits_then_completes(events, monkeypatch):
    db = FakeDB({"rfp_harvests": [_harvest(test_session_id="s1")], "bid_split_jobs": [], "bid_split_files": [],
                 "bid_split_segments": []})
    started = []

    def fake_start(sb, harvest, entries, settings, context, session_id, rfp_email_id, **_kw):
        started.append((harvest["id"], [e["sandbox_file_id"] for e in entries], context, session_id, rfp_email_id))
        assert _hv(db)["split_status"] == "pending"   # the claim is held while staging
        sb.table("bid_split_jobs").insert({"id": JOB, "status": "processing", "source": "rfp"}).execute()
        rfp_split._cas_split(sb, harvest["id"], "pending", {"split_status": "running", "split_job_id": JOB})
        return JOB, 2

    monkeypatch.setattr(rfp_split, "_start", fake_start)
    out = rfp_split.advance(db, _hv(db), settings=_settings(), context={"subject": "ITB"}, rfp_email_id="e1")
    assert out.waiting and out.job_id == JOB
    assert started == [(HV, ["sf-1", "sf-2"], {"subject": "ITB"}, "s1", "e1")]
    assert _hv(db)["split_status"] == "running"
    # Still processing: wait, no writes.
    assert rfp_split.advance(db, _hv(db), settings=_settings()).waiting
    # Another worker holds the claim (a fresh stamp): wait.
    db2 = FakeDB({"rfp_harvests": [_harvest(split_status="pending", split_started_at=rfp_split._iso(rfp_split._now()))]})
    assert rfp_split.advance(db2, _hv(db2), settings=_settings()).waiting
    assert _hv(db2)["split_status"] == "pending"
    # The job settles: complete, with one event per file and the finish.
    db.tables["bid_split_jobs"][0]["status"] = "done_with_errors"
    db.tables["bid_split_files"] = [
        {"id": "f1", "job_id": JOB, "filename": "SET.pdf", "status": "done", "file_kind": "drawing_set",
         "classified_from": "pages", "source_format": "pdf", "created_at": "1"},
        {"id": "f2", "job_id": JOB, "filename": "Manual.docx", "status": "failed", "error": "boom",
         "classified_from": "converted_pdf", "source_format": "docx", "created_at": "2"},
    ]
    db.tables["bid_split_segments"] = [{"id": "s1", "file_id": "f1", "sort_order": 0}, {"id": "s2", "file_id": "f1", "sort_order": 1}]
    events.clear()
    out = rfp_split.advance(db, _hv(db), settings=_settings())
    assert out.state == "complete" and out.job_id == JOB
    assert _hv(db)["split_status"] == "complete" and _hv(db)["split_finished_at"]
    kinds = [(e["kind"], e["detail"].get("status")) for e in events]
    assert kinds == [("file", "done"), ("file", "failed"), ("finished", "complete")]
    assert events[0]["detail"]["segments"] == 2 and events[0]["detail"]["classified_from"] == "pages"
    assert events[1]["level"] == rfp_test.LEVEL_WARN
    assert events[2]["detail"] == {"harvest_id": HV, "job_id": JOB, "status": "complete", "files_done": 1,
                                   "files_failed": 1, "segments": 2}


def test_advance_maps_a_failed_or_missing_job_to_failed(events):
    db = FakeDB({"rfp_harvests": [_harvest(split_status="running", split_job_id=JOB)],
                 "bid_split_jobs": [{"id": JOB, "status": "failed"}], "bid_split_files": [], "bid_split_segments": []})
    out = rfp_split.advance(db, _hv(db), settings=_settings())
    assert out.state == "failed" and _hv(db)["split_status"] == "failed"
    assert _hv(db)["split_error"] == rfp_split._MSG_ALL_FAILED
    db = FakeDB({"rfp_harvests": [_harvest(split_status="running", split_job_id="gone")], "bid_split_jobs": []})
    out = rfp_split.advance(db, _hv(db), settings=_settings())
    assert out.state == "failed" and _hv(db)["split_error"] == rfp_split._MSG_JOB_MISSING


def test_advance_puts_the_claim_back_when_staging_fails(events, monkeypatch):
    db = FakeDB({"rfp_harvests": [_harvest()]})

    def boom(*a, **k):
        raise RuntimeError("storage down")

    monkeypatch.setattr(rfp_split, "_start", boom)
    with pytest.raises(RuntimeError):
        rfp_split.advance(db, _hv(db), settings=_settings())
    assert _hv(db)["split_status"] == "none" and _hv(db)["split_error"] == "storage down"
    # Nothing staged (every entry refused): skipped, not failed.
    monkeypatch.setattr(rfp_split, "_start", lambda *a, **k: (JOB, 0))
    out = rfp_split.advance(db, _hv(db), settings=_settings())
    assert out.state == "skipped" and out.reason == "no_files" and _hv(db)["split_status"] == "skipped"


def test_model_away_reads_the_configuration_then_the_health_snapshot(monkeypatch):
    monkeypatch.undo()  # the autouse stub; this test wants the real function
    from app.services import llm, llm_health

    s = _settings()
    monkeypatch.setattr(llm, "is_configured", lambda feature, settings: False)
    assert rfp_split.model_away(s) == ("unconfigured", "No model is configured for bid file splitting.")
    monkeypatch.setattr(llm, "is_configured", lambda feature, settings: True)

    def feat(state, detail=""):
        return SimpleNamespace(features=[SimpleNamespace(key="bid_split", state=state, detail=detail)])

    monkeypatch.setattr(llm_health, "cached", lambda settings: feat("provider_down", "box off"))
    assert rfp_split.model_away(s) == ("provider_down", "box off")
    monkeypatch.setattr(llm_health, "cached", lambda settings: feat("ok"))
    assert rfp_split.model_away(s) is None
    # A broken probe never blocks the step.
    monkeypatch.setattr(llm_health, "cached", lambda settings: (_ for _ in ()).throw(RuntimeError("probe")))
    assert rfp_split.model_away(s) is None


def test_advance_waits_without_staging_while_the_model_is_away(events, monkeypatch):
    db = FakeDB({"rfp_harvests": [_harvest(test_session_id="s1")]})
    monkeypatch.setattr(rfp_split, "model_away", lambda *a, **k: ("provider_down", "The local AI server is off."))
    monkeypatch.setattr(rfp_split, "_start", lambda *a, **k: (_ for _ in ()).throw(AssertionError("staged")))
    out = rfp_split.advance(db, _hv(db), settings=_settings(), rfp_email_id="e1")
    assert out.waiting and out.model_away and out.reason == "The local AI server is off." and out.job_id is None
    # No claim was taken: the harvest is untouched and the next pass starts fresh.
    assert _hv(db)["split_status"] == "none" and _hv(db)["split_error"] is None
    assert events[-1]["kind"] == "waiting" and events[-1]["level"] == rfp_test.LEVEL_WARN
    assert events[-1]["detail"] == {"harvest_id": HV, "job_id": None, "state": "provider_down",
                                    "why": "The local AI server is off.", "requeued": 0}
    assert events[-1]["source"] == rfp_test.SOURCE_SPLIT and events[-1]["rfp_email_id"] == "e1"
    # The skips still come first: a linked harvest never waits on the model.
    db = FakeDB({"rfp_harvests": [_harvest(project_id="p-old")]})
    assert rfp_split.advance(db, _hv(db), settings=_settings()).reason == "linked"


def test_sandbox_busy_waits_until_every_entry_has_its_verdict():
    """The harvest completes when its downloads land; the sandbox verdicts
    come later. A split that looks before they do must wait, not stage
    (2026-09-21: five verified PDFs skipped as `no_files` because the split
    ran 12 seconds before the first verdict)."""
    real = REAL_SANDBOX_BUSY
    db = FakeDB({
        "rfp_harvests": [_harvest(sandbox_run_id="run-1")],
        "rfp_ingest_files": [{"id": "sf-1", "status": "verified"}, {"id": "sf-2", "status": "running"}],
        "rfp_ingest_runs": [{"id": "run-1", "status": "running"}],
    })
    entries = rfp_split._entries(_hv(db))
    assert real(db, _hv(db), entries) == (1, 2)
    # A file row not yet written at all is pending too.
    db.tables["rfp_ingest_files"] = [{"id": "sf-1", "status": "verified"}]
    assert real(db, _hv(db), entries) == (1, 2)
    # No run row yet (the harvest is still writing it): still pending.
    db.tables["rfp_ingest_runs"] = []
    assert real(db, _hv(db), entries) == (1, 2)
    # The run is terminal: every file has its answer; a row still not
    # terminal is the promotion check's to refuse.
    db.tables["rfp_ingest_runs"] = [{"id": "run-1", "status": "done_with_errors"}]
    assert real(db, _hv(db), entries) is None
    # Every entry terminal (verified, rejected, failed): nothing to wait for.
    db.tables["rfp_ingest_runs"] = [{"id": "run-1", "status": "running"}]
    db.tables["rfp_ingest_files"] = [{"id": "sf-1", "status": "verified"}, {"id": "sf-2", "status": "rejected"}]
    assert real(db, _hv(db), entries) is None


def test_advance_waits_on_the_sandbox_without_a_claim(events, monkeypatch):
    db = FakeDB({
        "rfp_harvests": [_harvest(test_session_id="s1", sandbox_run_id="run-1")],
        "rfp_ingest_files": [{"id": "sf-1", "status": "verified"}, {"id": "sf-2", "status": "pending"}],
        "rfp_ingest_runs": [{"id": "run-1", "status": "running"}],
    })
    monkeypatch.setattr(rfp_split, "sandbox_busy", lambda sb, h, e: (1, 2))
    monkeypatch.setattr(rfp_split, "_start", lambda *a, **k: (_ for _ in ()).throw(AssertionError("staged")))
    monkeypatch.setattr(rfp_split, "model_away", lambda *a, **k: (_ for _ in ()).throw(AssertionError("model")))
    out = rfp_split.advance(db, _hv(db), settings=_settings(), rfp_email_id="e1")
    assert out.waiting and not out.model_away and out.job_id is None
    assert out.reason == rfp_split._MSG_SANDBOX_BUSY
    # No claim, no status change: the next pass looks again.
    assert _hv(db)["split_status"] == "none" and _hv(db)["split_error"] is None
    assert events[-1]["kind"] == "waiting" and events[-1]["level"] == rfp_test.LEVEL_INFO
    assert events[-1]["title"] == "Split waiting: the sandbox is still checking 1 of 2 documents"
    assert events[-1]["detail"] == {"harvest_id": HV, "job_id": None, "state": "sandbox_busy",
                                    "why": rfp_split._MSG_SANDBOX_BUSY, "still_checking": 1, "total": 2,
                                    "sandbox_run_id": "run-1"}
    assert events[-1]["source"] == rfp_test.SOURCE_SPLIT and events[-1]["rfp_email_id"] == "e1"
    # The skips still come first: a linked harvest never waits on the sandbox.
    db = FakeDB({"rfp_harvests": [_harvest(project_id="p-old")]})
    assert rfp_split.advance(db, _hv(db), settings=_settings()).reason == "linked"


def test_start_keeps_the_job_when_nothing_is_staged(monkeypatch, events):
    """Every entry refused by the promotion check: the job row stays
    (failed, zero files) instead of being deleted, and the bench event
    names every refusal so the skip is explainable."""
    db = FakeDB({
        "rfp_harvests": [_harvest(split_status="pending", test_session_id="s1")],
        "rfp_ingest_files": [{"id": "sf-1", "source_format": "pdf"}, {"id": "sf-2", "source_format": "docx"}],
        "bid_split_jobs": [], "bid_split_files": [],
    })
    monkeypatch.setattr(rfp_split, "_model_label", lambda s: "m")
    monkeypatch.setattr(rfp_split, "_scratch_dir", lambda s: Path("/tmp"))
    monkeypatch.setattr(rfp_split, "_clean_scratch", lambda p: None)
    monkeypatch.setattr(rcf, "promotion_for", lambda e, fr: rcf.Skip(
        "hazard:remote_goto" if e["sandbox_file_id"] == "sf-1" else "pages_unverified"))
    monkeypatch.setattr(rcf, "fetch_entry", lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetched")))
    job_id, staged = rfp_split._start(db, _hv(db), rfp_split._entries(_hv(db)), _settings(), {}, "s1", "e1")
    assert staged == 0
    job = db.tables["bid_split_jobs"][0]
    assert job["id"] == job_id and job["status"] == "failed" and job["file_count"] == 0 and job["completed_at"]
    assert db.tables["bid_split_files"] == []
    assert events[-1]["kind"] == "skipped" and events[-1]["level"] == rfp_test.LEVEL_WARN
    assert events[-1]["title"] == "Split skipped: no document passed the sandbox checks (hazard:remote_goto, pages_unverified)"
    assert events[-1]["detail"]["job_id"] == job_id and events[-1]["detail"]["reason"] == "no_files"
    assert events[-1]["detail"]["files"] == [
        {"filename": "SET.pdf", "staged": False, "reason": "hazard:remote_goto"},
        {"filename": "Manual.docx", "staged": False, "reason": "pages_unverified"},
    ]
    assert events[-1]["rfp_email_id"] == "e1" and events[-1]["session_id"] == "s1"
    # `_start` leaves the claim; `advance` finishes the harvest with the job linked.
    assert _hv(db)["split_status"] == "pending"


def test_advance_links_the_kept_job_when_nothing_is_staged(events, monkeypatch):
    db = FakeDB({"rfp_harvests": [_harvest()], "bid_split_jobs": []})

    def fake_start(sb, harvest, entries, settings, context, session_id, rfp_email_id, **_kw):
        sb.table("bid_split_jobs").insert({"id": JOB, "status": "failed", "file_count": 0, "source": "rfp"}).execute()
        return JOB, 0

    monkeypatch.setattr(rfp_split, "_start", fake_start)
    out = rfp_split.advance(db, _hv(db), settings=_settings())
    assert out.state == "skipped" and out.reason == "no_files" and out.job_id == JOB
    assert _hv(db)["split_status"] == "skipped" and _hv(db)["split_error"] == "no_files"
    assert _hv(db)["split_job_id"] == JOB and _hv(db)["split_finished_at"]
    assert db.tables["bid_split_jobs"] == [{"id": JOB, "status": "failed", "file_count": 0, "source": "rfp"}]


def _outage_db(runs):
    """A running harvest whose job settled with two failed files: f1 died of
    the model being away, f2 of a real error. `runs` = how many queue runs
    f1 already has."""
    history = [{"id": f"j{i}", "job_type": "bid_split", "target_id": "f1", "status": "failed",
                "error_kind": "unreachable", "created_at": str(i)} for i in range(runs)]
    return FakeDB({
        "rfp_harvests": [_harvest(split_status="running", split_job_id=JOB, test_session_id="s1")],
        "bid_split_jobs": [{"id": JOB, "status": "failed"}],
        "bid_split_files": [
            {"id": "f1", "job_id": JOB, "filename": "SET.pdf", "status": "failed", "error": "The local AI server is off.",
             "classified_from": "pages", "source_format": "pdf", "created_at": "1"},
            {"id": "f2", "job_id": JOB, "filename": "Manual.docx", "status": "failed", "error": "boom",
             "classified_from": "converted_pdf", "source_format": "docx", "created_at": "2"},
        ],
        "bid_split_segments": [],
        "llm_jobs": history + [{"id": "jx", "job_type": "bid_split", "target_id": "f2", "status": "failed",
                                "error_kind": "server_error", "created_at": "9"}],
    })


def test_check_waits_then_requeues_the_files_that_died_of_a_model_outage(events, monkeypatch):
    db = _outage_db(runs=1)
    # Still away: nothing is marked, the harvest stays running, the bench says why.
    monkeypatch.setattr(rfp_split, "model_away", lambda *a, **k: ("provider_down", "box off"))
    out = rfp_split.advance(db, _hv(db), settings=_settings())
    assert out.waiting and out.model_away and out.job_id == JOB
    assert _hv(db)["split_status"] == "running" and db.tables["bid_split_jobs"][0]["status"] == "failed"
    assert [e["kind"] for e in events] == ["waiting"] and events[0]["detail"]["job_id"] == JOB
    # Back: f1 goes pending and is queued again; f2 (a real failure) is left alone.
    monkeypatch.setattr(rfp_split, "model_away", lambda *a, **k: None)
    marks, queued = [], []

    def mark(file_id, **fields):
        marks.append((file_id, fields))
        for f in db.tables["bid_split_files"]:
            if f["id"] == file_id:
                f.update(fields)
        db.tables["bid_split_jobs"][0]["status"] = "processing"

    monkeypatch.setattr(bid_split, "_mark", mark)
    monkeypatch.setattr(rfp_split.llm_queue, "enqueue", lambda job_type, **kw: queued.append((job_type, kw)) or {"id": "j-new"})
    events.clear()
    out = rfp_split.advance(db, _hv(db), settings=_settings())
    assert out.waiting and not out.model_away and out.job_id == JOB
    assert marks == [("f1", {"status": "pending", "error": None})]
    assert queued == [("bid_split", {"target_id": "f1", "payload": {"file_id": "f1"}, "created_by": None,
                                     "settings": queued[0][1]["settings"]})]
    assert _hv(db)["split_status"] == "running"
    assert events[-1]["kind"] == "requeued" and events[-1]["level"] == rfp_test.LEVEL_INFO
    assert events[-1]["detail"]["requeued"] == 1 and events[-1]["detail"]["files"] == [{"file_id": "f1", "filename": "SET.pdf"}]
    # The job is processing again: the next poll waits on it.
    assert rfp_split.advance(db, _hv(db), settings=_settings()).waiting
    # An enqueue failure puts the file's own failure back; the next poll grades it.
    db = _outage_db(runs=1)
    marks.clear()
    monkeypatch.setattr(rfp_split.llm_queue, "enqueue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("queue")))
    out = rfp_split.advance(db, _hv(db), settings=_settings())
    assert out.waiting
    assert marks == [("f1", {"status": "pending", "error": None}), ("f1", {"status": "failed", "error": "The local AI server is off."})]
    assert events[-1]["kind"] == "requeued" and events[-1]["detail"]["requeued"] == 0 and events[-1]["level"] == rfp_test.LEVEL_WARN


def test_check_grades_an_outage_failure_as_real_after_the_run_cap(events, monkeypatch):
    db = _outage_db(runs=rfp_split._OUTAGE_RUNS_MAX)
    monkeypatch.setattr(bid_split, "_mark", lambda *a, **k: (_ for _ in ()).throw(AssertionError("requeued")))
    out = rfp_split.advance(db, _hv(db), settings=_settings())
    assert out.state == "failed" and _hv(db)["split_status"] == "failed"
    assert [e["kind"] for e in events] == ["file", "file", "finished"]
    # Nothing failed of an outage at all: the plain terminal path.
    db = _outage_db(runs=0)
    db.tables["llm_jobs"] = [j for j in db.tables["llm_jobs"] if j["target_id"] != "f1"]
    assert rfp_split.advance(db, _hv(db), settings=_settings()).state == "failed"


def test_start_stages_pdfs_office_files_and_names_and_enqueues(monkeypatch, events):
    """The staging loop: the promotion decision and the byte check decide
    what is staged (a Skip is left out), a PDF is counted and queued, an
    office file is staged as its converted PDF, a file with nothing to
    sample is staged by name, and every row carries the sandbox id."""
    from pypdf import PdfWriter
    import io

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buf = io.BytesIO()
    writer.write(buf)
    pdf = buf.getvalue()
    db = FakeDB({
        "rfp_harvests": [_harvest(split_status="pending", files=[
            {"file_path": "SET.pdf", "status": "accepted", "sandbox_file_id": "sf-1"},
            {"file_path": "Manual.docx", "status": "accepted", "sandbox_file_id": "sf-2"},
            {"file_path": "Photo.jpg", "status": "accepted", "sandbox_file_id": "sf-3"},
            {"file_path": "Hazard.pdf", "status": "accepted", "sandbox_file_id": "sf-4"},
        ])],
        "rfp_ingest_files": [
            {"id": "sf-1", "source_format": "pdf"}, {"id": "sf-2", "source_format": "docx", "converted_path": "c.pdf"},
            {"id": "sf-3", "source_format": "png"}, {"id": "sf-4", "source_format": "pdf"},
        ],
        "bid_split_jobs": [], "bid_split_files": [],
    })
    monkeypatch.setattr(rfp_split, "_model_label", lambda s: "m")
    monkeypatch.setattr(rfp_split, "_scratch_dir", lambda s: Path("/tmp"))
    monkeypatch.setattr(rfp_split, "_clean_scratch", lambda p: None)

    def promotion_for(entry, file_row):
        if entry["sandbox_file_id"] == "sf-4":
            return rcf.Skip("hazard:javascript")
        fmt = (file_row or {}).get("source_format")
        ct = "application/pdf" if fmt == "pdf" else "application/octet-stream"
        return rcf.Promote("q", "p", entry["file_path"], ct, True)

    monkeypatch.setattr(rcf, "promotion_for", promotion_for)
    monkeypatch.setattr(rcf, "fetch_entry", lambda d, fr, n, dest, mx: (d, pdf if d.content_type == "application/pdf" else b"bytes"))
    monkeypatch.setattr(rcf, "converted_promotion", lambda fr, n, fmt, why=None: rcf.Promote("d", "c.pdf", "Manual.pdf", "application/pdf", False))
    monkeypatch.setattr(rcf, "fetch_verified", lambda d, dest, mx: pdf)
    uploads = []
    monkeypatch.setattr(rfp_split.storage, "upload_file", lambda path, data, ct: uploads.append((path, len(data), ct)))
    queued = []
    monkeypatch.setattr(rfp_split.llm_queue, "enqueue", lambda jt, **kw: queued.append((jt, kw["target_id"], kw["payload"])))
    refreshed = []
    monkeypatch.setattr(bid_split, "refresh_job", refreshed.append)

    job_id, staged = rfp_split._start(db, _hv(db), rfp_split._entries(_hv(db)), _settings(), {"subject": "ITB"}, "s1", "e1")
    assert staged == 3 and refreshed == [job_id]
    job = db.tables["bid_split_jobs"][0]
    assert job["source"] == "rfp" and job["rfp_harvest_id"] == HV and job["created_by"] is None and job["file_count"] == 3
    rows = {r["filename"]: r for r in db.tables["bid_split_files"]}
    assert set(rows) == {"SET.pdf", "Manual.docx", "Photo.jpg"}
    assert rows["SET.pdf"]["classified_from"] == "pages" and rows["SET.pdf"]["page_count"] == 1
    assert rows["SET.pdf"]["source_format"] == "pdf" and rows["SET.pdf"]["rfp_sandbox_file_id"] == "sf-1"
    assert rows["Manual.docx"]["classified_from"] == "converted_pdf" and rows["Manual.docx"]["page_count"] is None
    assert rows["Manual.docx"]["storage_path"].endswith("Manual.pdf") and rows["Manual.docx"]["source_format"] == "docx"
    assert rows["Photo.jpg"]["classified_from"] == "name" and rows["Photo.jpg"]["source_format"] == "image"
    assert rows["Photo.jpg"]["input_snapshot"]["name_context"]["subject"] == "ITB"
    assert all(r["status"] == "pending" for r in rows.values())
    assert [q[1] for q in queued] == [rows["SET.pdf"]["id"], rows["Manual.docx"]["id"], rows["Photo.jpg"]["id"]]
    assert all(q[0] == "bid_split" and q[2] == {"file_id": q[1]} for q in queued)
    assert _hv(db)["split_status"] == "running" and _hv(db)["split_job_id"] == job_id
    assert events[-1]["kind"] == "started" and events[-1]["detail"]["job_id"] == job_id
    names = {f["filename"]: f for f in events[-1]["detail"]["files"]}
    assert names["Hazard.pdf"] == {"filename": "Hazard.pdf", "staged": False, "reason": "hazard:javascript"}


def test_advance_reclaims_a_stale_staging_claim(events, monkeypatch):
    """A `pending` claim whose worker died mid-staging (stamp older than
    RFP_SPLIT_STAGING_STALE_SECONDS, or no stamp at all) goes back to `none`
    and the split starts again on the same pass; a fresh claim is left alone."""
    old = rfp_split._iso(rfp_split._now() - timedelta(seconds=rfp_split.stale_seconds(_settings()) + 60))
    db = FakeDB({"rfp_harvests": [_harvest(split_status="pending", split_started_at=old)], "bid_split_jobs": []})
    started = []

    def fake_start(sb, harvest, entries, settings, context, session_id, rfp_email_id, **_kw):
        started.append(harvest["id"])
        assert _hv(db)["split_status"] == "pending" and _hv(db)["split_started_at"] != old
        sb.table("bid_split_jobs").insert({"id": JOB, "status": "processing", "source": "rfp"}).execute()
        rfp_split._cas_split(sb, harvest["id"], "pending", {"split_status": "running", "split_job_id": JOB})
        return JOB, 1

    monkeypatch.setattr(rfp_split, "_start", fake_start)
    out = rfp_split.advance(db, _hv(db), settings=_settings())
    assert out.waiting and started == [HV] and _hv(db)["split_status"] == "running"
    # A fresh claim (another worker staging right now) waits.
    fresh = rfp_split._iso(rfp_split._now())
    db = FakeDB({"rfp_harvests": [_harvest(split_status="pending", split_started_at=fresh)]})
    assert rfp_split.advance(db, _hv(db), settings=_settings()).waiting
    assert started == [HV] and _hv(db)["split_status"] == "pending"
    # The claim itself stamps the row, so a fresh claim is never mistaken for a stale one.
    db = FakeDB({"rfp_harvests": [_harvest()]})
    monkeypatch.setattr(rfp_split, "_start", lambda *a, **k: (JOB, 0))
    rfp_split.advance(db, _hv(db), settings=_settings())
    assert not rfp_split.pending_is_stale({"split_status": "pending", "split_started_at": fresh})
    assert rfp_split.pending_is_stale({"split_status": "pending", "split_started_at": None})
    assert rfp_split.pending_is_stale({"split_status": "pending", "split_started_at": old})
    assert not rfp_split.pending_is_stale({"split_status": "running", "split_started_at": None})


def test_start_discards_the_job_when_staging_throws(monkeypatch):
    """Storage dying part way through staging must not leave a `processing`
    job (the app shell polls for those) with orphaned objects: the job row
    and its prefix go, the exception still walks the caller's ladder."""
    db = FakeDB({
        "rfp_harvests": [_harvest(split_status="pending")],
        "rfp_ingest_files": [{"id": "sf-1", "source_format": "pdf"}, {"id": "sf-2", "source_format": "pdf"}],
        "bid_split_jobs": [], "bid_split_files": [],
    })
    monkeypatch.setattr(rfp_split, "_model_label", lambda s: "m")
    monkeypatch.setattr(rfp_split, "_scratch_dir", lambda s: Path("/tmp"))
    monkeypatch.setattr(rfp_split, "_clean_scratch", lambda p: None)
    monkeypatch.setattr(
        rcf, "promotion_for", lambda e, fr: rcf.Promote("q", "p", e["file_path"], "application/pdf", True)
    )
    calls = []

    def fetch(decision, file_row, name, dest, mx):
        calls.append(name)
        if len(calls) == 2:
            raise rcf.RfpCreateFilesTransient("storage down")
        return decision, b"not a pdf"   # unreadable: a `failed` row, staged all the same

    monkeypatch.setattr(rcf, "fetch_entry", fetch)
    swept = []
    monkeypatch.setattr(rfp_split.storage, "delete_bid_split_prefix", swept.append)
    with pytest.raises(rcf.RfpCreateFilesTransient):
        rfp_split._start(db, _hv(db), rfp_split._entries(_hv(db)), _settings(), {}, None, None)
    assert len(calls) == 2
    assert db.tables["bid_split_jobs"] == [] and len(swept) == 1
    assert _hv(db)["split_status"] == "pending"   # `advance` puts the claim back, not `_start`


# ── 4. promote_split_file ────────────────────────────────────────────────────


SPLIT_FILE = {"id": "f1", "job_id": JOB, "status": "done", "rfp_sandbox_file_id": "sf-1", "filename": "SET.pdf"}
SEGS = [
    {"id": "s-g", "file_id": "f1", "sort_order": 0, "category": "general_drawings", "name": "Cover Sheets",
     "description": "Covers.", "storage_path": "bid-splits/j/output/g.pdf", "size_bytes": 10, "is_original": False},
    {"id": "s-e", "file_id": "f1", "sort_order": 1, "category": "electrical_drawings", "name": "E-Sheets",
     "description": "Power.", "storage_path": "bid-splits/j/output/e.pdf", "size_bytes": 20, "is_original": False},
    {"id": "s-a", "file_id": "f1", "sort_order": 2, "category": "addenda", "name": "Addendum 2",
     "description": None, "storage_path": "bid-splits/j/output/a.pdf", "size_bytes": 5, "is_original": False},
]
ORIGINAL = (rcf.Promote("rfp-quarantine", "run/sf-1/source.pdf", "SET.pdf", "application/pdf", True), b"%PDF-source")


def _promote(db, split_file=SPLIT_FILE, segments=SEGS, fetch=lambda: ORIGINAL, monkeypatch=None, copies=None):
    existing = rfp_split.rows_for_file(rfp_split.project_rows(db, P1), split_file)
    return rfp_split.promote_split_file(
        db, project_id=P1, harvest_id=HV, split_file=split_file, segments=segments, existing=existing,
        fetch_original=fetch, name="SET.pdf",
    )


def _pf(db):
    return sorted(db.tables["project_files"], key=lambda r: (r["category"], r["filename"]))


def test_promote_split_file_files_every_segment_and_keeps_the_source_set(monkeypatch):
    db = FakeDB({"project_files": []})
    copies, uploads = [], []
    monkeypatch.setattr(rfp_split.storage, "copy_file", lambda a, b: copies.append((a, b)))
    monkeypatch.setattr(rfp_split.storage, "upload_file", lambda p, d, ct: uploads.append((p, d, ct)))
    monkeypatch.setattr(office_preview, "is_convertible", lambda *a: False)
    result = _promote(db)
    assert result.used_split and result.documents == 3 and result.inserted == 3 and result.drawings == 2
    rows = {r["bid_split_segment_id"]: r for r in db.tables["project_files"] if r["bid_split_segment_id"]}
    assert set(rows) == {"s-g", "s-e", "s-a"}
    assert rows["s-g"]["category"] == "drawing" and rows["s-g"]["filename"] == "Cover Sheets.pdf"
    assert rows["s-e"]["category"] == "electrical_drawing" and rows["s-e"]["size_bytes"] == 20
    assert rows["s-a"]["category"] == "addendum" and rows["s-a"]["addendum_number"] == "2"
    assert rows["s-a"]["addendum_issued_on"] is None
    for r in rows.values():
        assert r["rfp_sandbox_file_id"] is None and r["bid_split_file_id"] == "f1" and r["is_source_set"] is False
        assert r["uploaded_by"] is None and r["rfp_harvest_id"] == HV and r["mime_type"] == "application/pdf"
    assert [c[0] for c in copies] == ["bid-splits/j/output/g.pdf", "bid-splits/j/output/e.pdf", "bid-splits/j/output/a.pdf"]
    assert copies[1][1].startswith(f"{P1}/electrical_drawing/")
    source = [r for r in db.tables["project_files"] if r["is_source_set"]]
    assert len(source) == 1 and source[0]["category"] == "other"
    assert source[0]["note"] == "Source set: split into 3 documents"
    assert source[0]["rfp_sandbox_file_id"] == "sf-1" and source[0]["bid_split_segment_id"] is None
    assert uploads == [(source[0]["storage_path"], b"%PDF-source", "application/pdf")]
    # A retry: nothing copied or uploaded again, the counts read the rows present.
    copies.clear(), uploads.clear()
    again = _promote(db)
    assert again.documents == 3 and again.inserted == 0 and again.replaced == 0
    assert copies == [] and uploads == [] and len(db.tables["project_files"]) == 4


def test_promote_split_file_recuts_replace_stale_rows_and_categories_follow(monkeypatch):
    db = FakeDB({"project_files": []})
    _promote(db)
    deleted = []
    monkeypatch.setattr(rfp_split.storage, "delete_file", deleted.append)
    # The user moved the E-sheets to low voltage and dropped the addendum: a new cut.
    recut = [
        dict(SEGS[0]),
        {**SEGS[1], "id": "s-lv", "category": "low_voltage_drawings", "name": "LV Sheets", "storage_path": "bid-splits/j/output/lv.pdf"},
    ]
    result = _promote(db, segments=recut)
    assert result.documents == 2 and result.inserted == 1 and result.replaced == 2   # s-e and s-a rows went
    cats = sorted((r["category"], r["is_source_set"]) for r in db.tables["project_files"])
    assert cats == [("drawing", False), ("low_voltage_drawing", False), ("other", True)]
    assert any(p.startswith(f"{P1}/electrical_drawing/") for p in deleted)
    assert [r for r in db.tables["project_files"] if r["is_source_set"]][0]["note"] == "Source set: split into 2 documents"
    # A category-only correction on a kept segment updates the row in place.
    relabeled = [{**recut[0], "category": "civil_drawings"}, recut[1]]
    result = _promote(db, segments=relabeled)
    assert result.inserted == 0 and result.replaced == 0
    assert {r["category"] for r in db.tables["project_files"]} == {"civil_drawing", "low_voltage_drawing", "other"}


def test_promote_split_file_intact_and_the_collapse_between_shapes(monkeypatch):
    uploads = []
    monkeypatch.setattr(rfp_split.storage, "upload_file", lambda p, d, ct: uploads.append(p))
    db = FakeDB({"project_files": []})
    intact = [{"id": "s-o", "file_id": "f1", "sort_order": 0, "category": "specifications", "name": "Project Manual",
               "description": "Specs.", "storage_path": "bid-splits/j/source/x.pdf", "size_bytes": 99, "is_original": True}]
    original = (rcf.Promote("rfp-quarantine", "run/sf-1/source.pdf", "Manual.pdf", "application/pdf", True,
                            note="Converted from the original .doc by the ingestion sandbox"), b"%PDF-1")
    result = _promote(db, segments=intact, fetch=lambda: original)
    assert result.used_split and result.documents == 1 and result.inserted == 1 and result.drawings == 0
    row = db.tables["project_files"][0]
    assert row["category"] == "specification" and row["rfp_sandbox_file_id"] == "sf-1"
    assert row["bid_split_segment_id"] == "s-o" and row["bid_split_file_id"] == "f1" and row["is_source_set"] is False
    assert row["note"] == "Converted from the original .doc by the ingestion sandbox" and row["size_bytes"] == 6
    # Re-identified as an RFP: the same row takes the new category and segment.
    result = _promote(db, segments=[{**intact[0], "id": "s-o2", "category": "rfp"}], fetch=lambda: original)
    assert result.inserted == 0 and len(db.tables["project_files"]) == 1
    assert db.tables["project_files"][0]["category"] == "rfp" and db.tables["project_files"][0]["bid_split_segment_id"] == "s-o2"
    # Then cut into a drawing set: the intact row becomes the source set in place.
    result = _promote(db, segments=SEGS, fetch=lambda: original)
    assert result.inserted == 3 and uploads == [row["storage_path"]]   # no second upload of the original
    src = [r for r in db.tables["project_files"] if r["is_source_set"]]
    assert len(src) == 1 and src[0]["id"] == row["id"] and src[0]["category"] == "other" and src[0]["bid_split_segment_id"] is None
    # And back to intact: the segments go, the source set row is the document again.
    result = _promote(db, segments=intact, fetch=lambda: original)
    assert result.replaced == 3 and len(db.tables["project_files"]) == 1
    assert db.tables["project_files"][0]["is_source_set"] is False and db.tables["project_files"][0]["category"] == "specification"
    assert db.tables["project_files"][0]["note"] is None
    # A file that failed, or has no segments, is not this path's to promote.
    assert not _promote(db, split_file={**SPLIT_FILE, "status": "failed"}).used_split
    assert not _promote(db, segments=[]).used_split


def test_promote_split_file_records_a_skip_when_the_original_is_refused(monkeypatch):
    db = FakeDB({"project_files": []})
    result = _promote(db, fetch=lambda: rcf.Skip("changed_since_verification"))
    assert result.documents == 3 and result.skipped == [{"file_path": "SET.pdf", "reason": "source_set:changed_since_verification"}]
    assert not any(r["is_source_set"] for r in db.tables["project_files"])


def test_entries_keep_one_entry_per_sandbox_file():
    """A portal that lists the same bytes twice (PipelineSuite: at the root
    and again under the addendum folder) harvests two entries with one
    sandbox id; the splitter stages that file once, first entry wins."""
    hv = _harvest(files=[
        {"file_path": "SET.pdf", "status": "accepted", "sandbox_file_id": "sf-1"},
        {"file_path": "Addendum 01/SET.pdf", "status": "accepted", "sandbox_file_id": "sf-1"},
        {"file_path": "Manual.docx", "status": "reused", "sandbox_file_id": "sf-2"},
        {"file_path": "bad.pdf", "status": "rejected", "sandbox_file_id": None},
    ])
    assert [e["file_path"] for e in rfp_split._entries(hv)] == ["SET.pdf", "Manual.docx"]


def test_promotion_job_routes_done_split_rows_and_falls_back_for_the_rest(monkeypatch, tmp_path):
    """rfp_create_files.execute: a `done` split row with segments goes
    through promote_split_file; a failed row and one without a split row
    take the pre-split mapping; files_promoted counts documents."""
    from tests.test_rfp_create_files import FilesDB, PDF, _sha

    db = FilesDB({
        "rfp_created_projects": [{"project_id": P1, "harvest_id": HV, "files_status": "pending", "files_claim_token": None,
                                  "files_claimed_at": None, "files_promoted": 0, "files_skipped": [], "split_job_id": JOB}],
        "rfp_harvests": [_harvest(files=[
            {"file_path": "SET.pdf", "status": "accepted", "sandbox_file_id": "sf-1", "kind": "drawing"},
            {"file_path": "Specs/Manual.pdf", "status": "accepted", "sandbox_file_id": "sf-2", "kind": "specification"},
            {"file_path": "Other.pdf", "status": "accepted", "sandbox_file_id": "sf-3"},
        ])],
        "rfp_ingest_files": [
            {"id": "sf-1", "status": "verified", "hazards": {}, "quarantine_path": "q/1", "source_format": "pdf",
             "sha256": _sha(PDF), "filename": "SET.pdf", "size_bytes": len(PDF), "manifest": {}},
            {"id": "sf-2", "status": "verified", "hazards": {}, "quarantine_path": "q/2", "source_format": "pdf",
             "sha256": _sha(PDF), "filename": "Manual.pdf", "size_bytes": len(PDF), "manifest": {}},
            {"id": "sf-3", "status": "verified", "hazards": {}, "quarantine_path": "q/3", "source_format": "pdf",
             "sha256": _sha(PDF), "filename": "Other.pdf", "size_bytes": len(PDF), "manifest": {}},
        ],
        "bid_split_files": [
            {"id": "f1", "job_id": JOB, "status": "done", "rfp_sandbox_file_id": "sf-1", "filename": "SET.pdf", "created_at": "1"},
            {"id": "f2", "job_id": JOB, "status": "failed", "rfp_sandbox_file_id": "sf-2", "filename": "Manual.pdf", "created_at": "2"},
        ],
        "bid_split_segments": [dict(s) for s in SEGS],
        "project_files": [], "projects": [{"id": P1, "name": "W", "number": "1"}], "estimator_assignments": [],
        "llm_jobs": [],
    })
    settings = _settings(rfp_ingest_scratch_dir=str(tmp_path))
    monkeypatch.setattr(rcf, "get_settings", lambda: settings)
    monkeypatch.setattr(rcf, "get_supabase", lambda: db)
    monkeypatch.setattr(rcf.llm_queue, "renew_lease", lambda job=None: True)
    monkeypatch.setattr(rcf, "audit", lambda *a, **k: None)
    monkeypatch.setattr(rcf, "_notify_drawings", lambda *a, **k: None)
    monkeypatch.setattr(rcf.office_preview, "is_convertible", lambda *a: False)
    uploads = []
    monkeypatch.setattr(rcf.storage, "upload_file", lambda p, d, ct: uploads.append(p))
    monkeypatch.setattr(rfp_split.storage, "upload_file", lambda p, d, ct: uploads.append(p))

    def download(bucket, path, dest, *, max_bytes):
        Path(dest).write_bytes(PDF)
        return len(PDF)

    monkeypatch.setattr(rcf.rs, "download_to_file", download)
    monkeypatch.setattr(rcf, "_now", lambda: __import__("datetime").datetime(2026, 9, 18, tzinfo=__import__("datetime").timezone.utc))

    rcf.execute(P1)
    rec = db.tables["rfp_created_projects"][0]
    assert rec["files_status"] == "complete" and rec["files_skipped"] == []
    rows = db.tables["project_files"]
    by_cat = sorted((r["category"], bool(r["bid_split_segment_id"]), r["is_source_set"]) for r in rows)
    assert by_cat == [
        ("addendum", True, False), ("drawing", True, False), ("electrical_drawing", True, False),
        ("other", False, False),           # Other.pdf: no split row, pre-split mapping (kind null)
        ("other", False, True),            # the source set
        ("specification", False, False),   # Manual.pdf: the split failed, pre-split mapping (Procore kind)
    ]
    assert rec["files_promoted"] == 5      # documents: 3 segments + 2 intact files; the source set is not one
    assert [r for r in rows if r["is_source_set"]][0]["rfp_sandbox_file_id"] == "sf-1"
    # A retry promotes nothing twice.
    rec.update(files_status="pending")
    rcf.execute(P1)
    assert len(db.tables["project_files"]) == 6 and rec["files_promoted"] == 5
    # The same sandbox file listed twice by the portal (PipelineSuite root +
    # addendum folder) is promoted once and counted once.
    db.tables["rfp_harvests"][0]["files"].insert(
        1, {"file_path": "Addendum 01/SET.pdf", "status": "accepted", "sandbox_file_id": "sf-1", "kind": "drawing"}
    )
    rec.update(files_status="pending")
    rcf.execute(P1)
    assert len(db.tables["project_files"]) == 6 and rec["files_promoted"] == 5


def test_count_documents_and_rows_for_file():
    db = FakeDB({"project_files": [
        {"id": "a", "project_id": P1, "rfp_sandbox_file_id": "sf-1", "bid_split_file_id": "f1", "is_source_set": True},
        {"id": "b", "project_id": P1, "rfp_sandbox_file_id": None, "bid_split_file_id": "f1", "is_source_set": False},
        {"id": "c", "project_id": P1, "rfp_sandbox_file_id": "sf-9", "bid_split_file_id": None, "is_source_set": False},
        {"id": "d", "project_id": "p-2", "rfp_sandbox_file_id": "sf-1", "bid_split_file_id": None, "is_source_set": False},
        {"id": "e", "project_id": P1, "rfp_sandbox_file_id": None, "bid_split_file_id": None, "is_source_set": False},
    ]})
    assert {r["id"] for r in rfp_split.project_rows(db, P1)} == {"a", "b", "c"}
    assert rfp_split.count_documents(db, P1) == 2
    assert {r["id"] for r in rfp_split.rows_for_file(rfp_split.project_rows(db, P1), SPLIT_FILE)} == {"a", "b"}


# ── 3.2 The ingest steps ─────────────────────────────────────────────────────


def _email_row(db, email_id="e1"):
    return next(r for r in db.tables["rfp_emails"] if r["id"] == email_id)


def _email_db(**over):
    row = {"id": "e1", "status": "split", "harvest_id": HV, "flag_reason": "no_candidate", "attempts": 1,
           "last_error": None, "next_attempt_at": None, "subject": "ITB", "invitation_method": "organic",
           "extracted_project_name": "Warehouse", "test_session_id": None}
    row.update(over)
    return FakeDB({"rfp_emails": [row], "rfp_harvests": [_harvest()]})


def test_email_step_split_falls_through_to_create_while_the_flag_is_off(monkeypatch):
    monkeypatch.setattr(ingest, "get_settings", lambda: _settings(rfp_split_enabled=False))
    db = _email_db()
    assert ingest._step_split(db, dict(_email_row(db))) == "create"
    row = _email_row(db)
    assert row["status"] == "create" and row["attempts"] == 0 and row["last_error"] is None
    assert row["flag_reason"] == "no_candidate" and row["decided_at_step"] == "split"
    assert db.tables["rfp_harvests"][0]["split_status"] == "skipped"
    assert db.tables["rfp_harvests"][0]["split_error"] == "flags_off"
    # The whole pipeline pass: split then create in one call (create off drains to done).
    monkeypatch.setattr(ingest, "get_settings", lambda: _settings(rfp_split_enabled=False, rfp_create_auto_enabled=False))
    db = _email_db()
    ingest._process_email(db, dict(_email_row(db)))
    assert _email_row(db)["status"] == "done" and _email_row(db)["decided_at_step"] == "create"
    # No harvest at all: skipped as no_files, straight on.
    db = _email_db(harvest_id=None)
    assert ingest._step_split(db, dict(_email_row(db))) == "create"


def test_email_step_split_waits_without_an_attempt_and_walks_the_ladder_on_trouble(monkeypatch):
    monkeypatch.setattr(ingest, "get_settings", lambda: _settings())
    db = _email_db()
    calls = []

    def fake_advance(sb, harvest, *, settings, context, test_session_id, rfp_email_id, renew=None):
        calls.append((harvest["id"], context, test_session_id, rfp_email_id))
        return rfp_split.Outcome("waiting", job_id=JOB)

    monkeypatch.setattr(ingest.rfp_split, "advance", fake_advance)
    before = ingest._now()
    assert ingest._step_split(db, dict(_email_row(db))) is None
    row = _email_row(db)
    assert row["status"] == "split" and row["attempts"] == 1
    assert ingest._parse_ts(row["next_attempt_at"]) >= before + timedelta(seconds=30)
    assert calls == [(HV, {"subject": "ITB", "project_name": "Warehouse", "invitation_method": "organic"}, None, "e1")]
    # The model away: the model-wait interval, the reason on the row, no attempt spent.
    monkeypatch.setattr(ingest.rfp_split, "advance",
                        lambda *a, **k: rfp_split.Outcome("waiting", reason="box off", model_away=True))
    row["next_attempt_at"] = None
    before = ingest._now()
    assert ingest._step_split(db, dict(_email_row(db))) is None
    row = _email_row(db)
    assert row["status"] == "split" and row["attempts"] == 1 and row["last_error"] == "box off"
    assert ingest._parse_ts(row["next_attempt_at"]) >= before + timedelta(
        seconds=_settings().rfp_email_ingestion_classify_retry_seconds)
    # A terminal outcome moves the row on.
    monkeypatch.setattr(ingest.rfp_split, "advance", lambda *a, **k: rfp_split.Outcome("complete", job_id=JOB))
    row["next_attempt_at"] = None
    assert ingest._step_split(db, dict(_email_row(db))) == "create"
    assert _email_row(db)["status"] == "create"
    # Trouble: the ladder (attempt spent, backoff), the row stays at split.
    db = _email_db()

    def boom(*a, **k):
        raise RuntimeError("storage down")

    monkeypatch.setattr(ingest.rfp_split, "advance", boom)
    assert ingest._step_split(db, dict(_email_row(db))) is None
    row = _email_row(db)
    assert row["status"] == "split" and row["attempts"] == 2 and row["last_error"] == "storage down"
    assert row["next_attempt_at"]


def test_email_status_vocabulary_and_actions_treat_split_like_create():
    assert ingest.STATUS_PENDING.index("split") == ingest.STATUS_PENDING.index("create") - 1
    assert "split" not in ingest._DISMISSABLE and "split" not in ingest.STATUS_PRE_METHOD
    assert "split" in [s for s in ingest.STATUS_PENDING if s not in ingest.STATUS_PRE_METHOD]  # set_method accepts it
    from app.routers import rfp_emails as rr

    assert "split" in rr.TAB_STATUSES["processed"]


def _inv_db(**over):
    row = {"id": "inv-1", "portal": "ngem", "status": "split", "harvest_id": HV, "title": "Phone Towers",
           "attempts": 0, "last_error": None, "next_attempt_at": None, "flag_reason": None}
    row.update(over)
    return FakeDB({"rfp_portal_invitations": [row], "rfp_harvests": [_harvest()]})


def test_portal_step_split_mirrors_the_email_step(monkeypatch):
    settings = _settings(rfp_split_enabled=False)
    db = _inv_db()
    assert portal._step_split(db, dict(db.tables["rfp_portal_invitations"][0]), settings) is True
    row = db.tables["rfp_portal_invitations"][0]
    assert row["status"] == "create" and row["decided_at_step"] == "split" and row["attempts"] == 0
    assert db.tables["rfp_harvests"][0]["split_status"] == "skipped"
    # The sweep runs split then create in one pass (create off: done).
    db = _inv_db()
    portal._process_invitation(db, dict(db.tables["rfp_portal_invitations"][0]), portal._SweepState(), settings,
                               llm_down=False, renew=lambda: True)
    assert db.tables["rfp_portal_invitations"][0]["status"] == "done"
    # Waiting: parked by the poll interval, no attempt spent.
    monkeypatch.setattr(portal.rfp_split, "advance", lambda *a, **k: rfp_split.Outcome("waiting"))
    db = _inv_db(attempts=1)
    assert portal._step_split(db, dict(db.tables["rfp_portal_invitations"][0]), _settings()) is False
    row = db.tables["rfp_portal_invitations"][0]
    assert row["status"] == "split" and row["attempts"] == 1 and row["next_attempt_at"]
    # The model away: the model-wait interval and the reason as the sentence, no attempt spent.
    monkeypatch.setattr(portal.rfp_split, "advance",
                        lambda *a, **k: rfp_split.Outcome("waiting", reason="box off", model_away=True))
    db = _inv_db(attempts=1)
    before = portal._now()
    assert portal._step_split(db, dict(db.tables["rfp_portal_invitations"][0]), _settings()) is False
    row = db.tables["rfp_portal_invitations"][0]
    assert row["status"] == "split" and row["attempts"] == 1 and row["last_error"] == "box off"
    assert portal._parse_ts(row["next_attempt_at"]) >= before + timedelta(
        seconds=_settings().rfp_email_ingestion_classify_retry_seconds)
    # Trouble: the ladder.
    monkeypatch.setattr(portal.rfp_split, "advance", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    db = _inv_db()
    assert portal._step_split(db, dict(db.tables["rfp_portal_invitations"][0]), _settings()) is False
    row = db.tables["rfp_portal_invitations"][0]
    assert row["status"] == "split" and row["attempts"] == 1 and row["last_error"] == "down"
    # `split` may be ignored, like `create`.
    assert "split" in portal.IGNORABLE_STATUSES and "split" in portal.STATUS_PENDING


# ── 3.3 The splitter router: the sent lock, the resync, the delete refusal ──


@pytest.fixture
def router_env(monkeypatch):
    from tests import test_bid_split_corrections as tc
    from app.routers import bid_splitter as bs

    db = tc.FakeDB(tc._tables(bid_split_jobs=[{"id": "j1", "model": "m", "status": "done", "source": "rfp",
                                                "project_id": P1, "rfp_harvest_id": HV}]))
    calls = tc._install(monkeypatch, db)
    resyncs = []
    monkeypatch.setattr(bs.rfp_split, "resync_project_files", lambda sb, fid, *, actor_id=None: (
        resyncs.append((fid, actor_id)) or rfp_split.PromoteResult(used_split=True, documents=2, inserted=1)))
    monkeypatch.setattr(bs.bid_split_training, "capture_correction", lambda *a, **k: None)
    monkeypatch.setattr(bs.llm, "is_configured", lambda *a, **k: True)
    monkeypatch.setattr(bs, "_dispatch", lambda *a, **k: None)
    return SimpleNamespace(db=db, calls=calls, resyncs=resyncs, bs=bs, tc=tc)


def test_corrections_answer_409_once_the_package_has_sent(router_env, monkeypatch):
    bs, tc = router_env.bs, router_env.tc
    monkeypatch.setattr(bs, "handoff_locked", lambda pid: pid == P1)
    with pytest.raises(HTTPException) as exc:
        bs.correct_file_kind("f1", bs.BidSplitFileKindIn(file_kind="rfp"), user=tc._WRITER)
    assert exc.value.status_code == 409 and exc.value.detail == rfp_split.MSG_PACKAGE_SENT
    body = bs.BidSplitSegmentsIn(segments=[{"category": "specifications", "page_start": 1, "page_end": 6}])
    with pytest.raises(HTTPException) as exc:
        bs.correct_segments("f1", body, user=tc._WRITER)
    assert exc.value.status_code == 409 and exc.value.detail == rfp_split.MSG_PACKAGE_SENT
    with pytest.raises(HTTPException) as exc:
        bs.reprocess_file("f1", None, user=tc._WRITER)
    assert exc.value.status_code == 409 and exc.value.detail == rfp_split.MSG_PACKAGE_SENT
    assert router_env.resyncs == [] and len(router_env.db.tables["bid_split_segments"]) == 2
    assert exc.value.detail == "This project's package has already been sent; correct the files on the project."


def test_corrections_on_an_unsent_project_re_file_it(router_env, monkeypatch):
    bs, tc = router_env.bs, router_env.tc
    monkeypatch.setattr(bs, "handoff_locked", lambda pid: False)
    out = bs.correct_file_kind("f1", bs.BidSplitFileKindIn(file_kind="rfp"), user=tc._WRITER)
    assert router_env.resyncs == [("f1", "u1")]
    assert out["project_resync"] == {"project_id": P1, "ok": True, "documents": 2, "inserted": 1, "replaced": 0, "skipped": []}
    body = bs.BidSplitSegmentsIn(segments=[{"category": "specifications", "page_start": 1, "page_end": 6}])
    out = bs.correct_segments("f1", body, user=tc._WRITER)
    assert router_env.resyncs[-1] == ("f1", "u1") and out["project_resync"]["ok"] is True
    # A manual job: no lock, no resync.
    router_env.db.tables["bid_split_jobs"][0].update(source="manual", project_id=None)
    out = bs.correct_file_kind("f1", bs.BidSplitFileKindIn(file_kind="addendum"), user=tc._WRITER)
    assert out["project_resync"] is None and len(router_env.resyncs) == 2


def test_an_rfp_job_with_a_project_cannot_be_deleted(router_env, monkeypatch):
    bs, tc = router_env.bs, router_env.tc
    with pytest.raises(HTTPException) as exc:
        bs.delete_job("j1", user=tc._WRITER)
    assert exc.value.status_code == 409 and exc.value.detail == rfp_split.MSG_JOB_HAS_PROJECT
    swept = []
    monkeypatch.setattr(bs.storage, "delete_bid_split_prefix", swept.append)
    router_env.db.tables["bid_split_jobs"][0]["project_id"] = None   # the project was discarded (FK set null)
    bs.delete_job("j1", user=tc._WRITER)
    assert swept == ["j1"] and router_env.db.tables["bid_split_jobs"] == []


def test_non_pdf_rows_are_identified_never_re_cut(router_env, monkeypatch):
    bs, tc = router_env.bs, router_env.tc
    monkeypatch.setattr(bs, "handoff_locked", lambda pid: False)
    frow = router_env.db.tables["bid_split_files"][0]
    frow.update(classified_from="name", source_format="image", page_count=None, file_kind="other",
                file_kind_label="Site Photos")
    router_env.db.tables["bid_split_segments"] = [{**tc.SEG_GENERAL, "category": "other", "other_type": "Site Photos",
                                                    "page_start": 1, "page_end": 1, "is_original": True,
                                                    "storage_path": frow["storage_path"]}]
    for call in (lambda: bs.reprocess_file("f1", None, user=tc._WRITER),
                 lambda: bs.correct_segments("f1", bs.BidSplitSegmentsIn(
                     segments=[{"category": "rfp", "page_start": 1, "page_end": 1}]), user=tc._WRITER),
                 lambda: bs.correct_file_kind("f1", bs.BidSplitFileKindIn(file_kind="drawing_set"), user=tc._WRITER)):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 409 and exc.value.detail == rfp_split.MSG_NOT_A_PDF
    # An intact re-identification works without a page count (1..1).
    out = bs.correct_file_kind("f1", bs.BidSplitFileKindIn(file_kind="rfp"), user=tc._WRITER)
    seg = out["segments"][0]
    assert seg["category"] == "rfp" and (seg["page_start"], seg["page_end"]) == (1, 1) and seg["is_original"] is True
    assert router_env.resyncs == [("f1", "u1")]


def test_job_payloads_carry_the_project_and_the_lock(router_env, monkeypatch):
    bs, tc = router_env.bs, router_env.tc
    router_env.db.tables["projects"] = [{"id": P1, "number": "26.9.7210", "name": "Warehouse"}]
    monkeypatch.setattr(bs, "handoff_locked", lambda pid: True)
    monkeypatch.setattr(bs.llm_queue, "poll_info", lambda *a, **k: None)
    job = bs.get_job("j1", _=tc._WRITER)
    assert job["source"] == "rfp" and job["project_id"] == P1 and job["project_number"] == "26.9.7210"
    assert job["package_sent"] is True and job["project_name"] == "Warehouse"
    assert {"source_format", "classified_from", "page_count"} <= set(job["files"][0])
    rows = bs.list_jobs(limit=500, offset=0, status_filter=None, _=tc._WRITER)
    assert rows[0]["project_number"] == "26.9.7210" and rows[0]["package_sent"] is True
    assert "rfp_sandbox_file_id, source_format, classified_from" in bs._FILE_COLUMNS


# ── 7. Test bench, migration, settings ───────────────────────────────────────


def test_step_chips_gain_the_split_step():
    assert rfp_test.STEPS.index("split") == rfp_test.STEPS.index("harvest") + 1
    chips = {c["step"]: c["state"] for c in rfp_test.step_chips({"status": "split", "next_attempt_at": "soon"})}
    assert chips["split"] == "waiting" and chips["harvest"] == "done" and chips["create"] == "pending"
    chips = {c["step"]: c["state"] for c in rfp_test.step_chips({"status": "merged"})}
    assert chips["split"] == "skipped"
    chips = {c["step"]: c["state"] for c in rfp_test.step_chips({"status": "created", "harvest_id": None})}
    assert chips["harvest"] == "skipped" and chips["split"] == "skipped" and chips["create"] == "done"
    chips = {c["step"]: c["state"] for c in rfp_test.step_chips({"status": "created", "harvest_id": HV, "split_status": "skipped"})}
    assert chips["split"] == "skipped" and chips["harvest"] == "done"
    chips = {c["step"]: c["state"] for c in rfp_test.step_chips({"status": "created", "harvest_id": HV, "split_status": "complete"})}
    assert chips["split"] == "done"
    events = [{"id": 5, "source": "split", "kind": "file"}]
    chips = {c["step"]: c for c in rfp_test.step_chips({"status": "done"}, events)}
    assert chips["split"]["event_id"] == 5


def test_cleanup_removes_the_sessions_rfp_split_jobs(monkeypatch):
    db = FakeDB({
        "rfp_test_sessions": [{"id": "s1", "status": "ended"}],
        "rfp_harvests": [{"id": HV, "test_session_id": "s1", "sandbox_run_id": None}],
        "bid_split_jobs": [{"id": "j1", "source": "rfp", "rfp_harvest_id": HV},
                           {"id": "j2", "source": "manual", "rfp_harvest_id": None},
                           {"id": "j3", "source": "rfp", "rfp_harvest_id": "hv-other"}],
        "projects": [], "gc_contacts": [], "general_contractors": [], "rfp_ingest_runs": [], "rfp_emails": [],
        "ingested_emails": [], "email_log": [], "rfp_test_events": [],
    })
    swept = []
    monkeypatch.setattr(storage, "delete_bid_split_prefix", swept.append)
    report = rfp_test.cleanup(db, "s1", actor_id=None)
    assert report["deleted"]["split_jobs"] == 1 and swept == ["j1"]
    assert {j["id"] for j in db.tables["bid_split_jobs"]} == {"j2", "j3"}


def test_migration_0132_shape():
    migrations = Path(__file__).resolve().parents[1] / "supabase/migrations"
    sql = (migrations / "0132_rfp_split.sql").read_text(encoding="utf-8")
    assert sql.startswith("-- 0132 - ") and sql.rstrip().endswith("notify pgrst, 'reload schema';")
    assert "\u2014" not in sql and "\u2013" not in sql   # em dash, en dash
    # The enum labels come first, before any statement that could use them.
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    statements = [s.strip() for s in re.split(r";\s*\n", body) if s.strip()]
    first_non_enum = next(i for i, s in enumerate(statements) if "add value if not exists" not in s)
    labels = re.findall(r"add value if not exists '([a-z_]+)'", "\n".join(statements[:first_non_enum]))
    assert set(labels) == {"civil_drawing", "structural_drawing", "architectural_drawing", "mechanical_drawing",
                           "plumbing_drawing", "fire_protection_drawing", "low_voltage_drawing", "rfp"}
    assert set(labels) <= fc.VALID_CATEGORIES
    for needle in (
        "bid_split_segment_id uuid references bid_split_segments(id) on delete set null",
        "bid_split_file_id    uuid references bid_split_files(id) on delete set null",
        "is_source_set        boolean not null default false",
        "project_files_split_segment_uidx",
        "project_files_addendum_meta_ck",
        "source in ('manual', 'rfp')",
        "classified_from in ('pages', 'converted_pdf', 'name')",
        "split_status in ('none', 'pending', 'running', 'complete', 'failed', 'skipped')",
        "471859200",
    ):
        assert needle in sql, needle
    email_check = re.search(r"add constraint rfp_emails_status_check check \(status in \(([^)]+)\)\)", sql, re.S)
    values = {v.strip().strip("'") for v in email_check.group(1).replace("\n", ",").split(",") if v.strip()}
    # Frozen history: `blocked_sender` joined the vocabulary in 0133, not here.
    assert values | {"blocked_sender"} == (
        set(ingest.STATUS_PENDING) | set(ingest.STATUS_HUMAN) | set(ingest.STATUS_TERMINAL)
    )
    portal_check = re.search(r"add constraint rfp_portal_invitations_status_check\s+check \(status in \(([^)]+)\)\)", sql, re.S)
    # Frozen history: the parked `historical`, `expired`, `withdrawn` joined
    # the vocabulary in 0136 (docs/RFP_BUILDINGCONNECTED.md 5), not here.
    assert {v.strip().strip("'") for v in portal_check.group(1).replace("\n", ",").split(",") if v.strip()} == (
        set(portal.ALL_STATUSES) - {"historical", "expired", "withdrawn"}
    )


def test_settings_defaults_match_the_contract():
    s = Settings(_env_file=None)
    assert s.rfp_split_enabled is False and s.rfp_split_poll_seconds == 30
    assert s.bid_split_max_files_per_job == 250
    assert s.rfp_harvest_max_files == 250 and s.rfp_ingest_max_files_per_run == 250
    assert rfp_split.enabled(_settings()) and not rfp_split.enabled(_settings(rfp_split_enabled=False))
    with pytest.raises(ValueError):
        Settings(_env_file=None, rfp_split_poll_seconds=2)
    assert rfp_split.context_for_portal({"title": "Phone Towers", "portal": "ngem"}) == {
        "subject": "Phone Towers", "project_name": "Phone Towers", "invitation_method": "ngem"}

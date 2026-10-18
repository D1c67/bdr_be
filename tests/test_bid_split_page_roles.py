"""Bid File Splitter: per-page roles, segment page_meta, the plans_reader
job source and request-free dispatch (migration 0152, docs/PLANS_READER.md
3.2 and section 6).

What these tests pin:
- page_role rides the per-page schema (required, the PAGE_ROLES enum from
  plans_reader_schemas) and the prompt explains every role;
- _validate_batch never fails on a missing or unknown role (older model
  outputs): it reads as 'other';
- roles survive island repair (reassigned pages keep them) and merge_pages
  (on each page entry);
- segment_page_meta builds one entry per page of the stored object, the
  prepended cover pages first (prepended, pdf_page), or None when a page is
  missing or damaged; intact files get a per-page list from the triage;
- execute() writes page_meta on every segment it inserts (cut, whole-file
  from the page pass, intact triage verdict) and keeps the pristine roles
  in model_output.pages;
- the segment-edit and re-type paths in the router keep or rebuild
  page_meta from model_output.pages;
- a plans_reader job has no project side effects (no re-file, no whole-file
  fallback, no splitter bell, no hand-off lock even with a project_id),
  fires the AI Estimating split-done hook on done and failed, is refused
  (409) by every mutating splitter route, and is hidden from the default
  job list; manual jobs behave exactly as before;
- stage_upload moves the job back to processing;
- sweep_stale_files fails orphaned plans_reader files only;
- dispatch(None, ...) enqueues, and falls back to a daemon thread when the
  queue is off or the enqueue fails.

No network, no model: llm, storage and Supabase are faked in memory.
"""

import sys
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.config import Settings
from app.routers import bid_splitter as bs
from app.services import bid_split, llm_queue, rfp_split, storage
from app.services.plans_reader_schemas import PAGE_ROLES
from tests.test_bid_split_corrections import (
    FILE_ROW,
    SEG_ELECTRICAL,
    SEG_GENERAL,
    FakeDB,
    _blank_pdf,
    _install,
    _put,
    _seg_in,
    _segments,
    _tables,
)


def _page(page, category, role="plan", title=None, confidence=0.9, other_type=None):
    entry = {
        "page": page,
        "category": category,
        "other_type": other_type,
        "title": title,
        "confidence": confidence,
    }
    if role is not None:
        entry["page_role"] = role
    return entry


# ── Schema and prompt ────────────────────────────────────────────────────


def test_page_schema_requires_page_role_from_the_shared_vocabulary():
    item = bid_split._PAGE_SCHEMA["properties"]["pages"]["items"]
    assert "page_role" in item["required"]
    assert item["properties"]["page_role"] == {"type": "string", "enum": list(PAGE_ROLES)}
    # Strict structured output: every property is listed as required.
    assert set(item["required"]) == set(item["properties"])


def test_classify_prompt_explains_every_role():
    for role in PAGE_ROLES:
        assert f"- {role}:" in bid_split._CLASSIFY_SYSTEM, role
    assert "\u2014" not in bid_split._CLASSIFY_SYSTEM  # no em dashes


# ── _validate_batch ──────────────────────────────────────────────────────


def test_validate_batch_keeps_a_known_role():
    out = bid_split._validate_batch({"pages": [_page(1, "electrical_drawings", "schedule")]}, [1])
    assert out[0]["page_role"] == "schedule"


@pytest.mark.parametrize("role", [None, "", "panel_schedule", 7, ["plan"]])
def test_validate_batch_defaults_a_missing_or_unknown_role_to_other(role):
    entry = _page(1, "electrical_drawings", role=None)
    if role is not None:
        entry["page_role"] = role
    out = bid_split._validate_batch({"pages": [entry]}, [1])
    assert out[0]["page_role"] == "other"
    # The rest of the entry is validated exactly as before.
    assert out[0]["category"] == "electrical_drawings" and out[0]["confidence"] == 0.9


# ── repair_islands / merge_pages ─────────────────────────────────────────


def test_island_repair_keeps_the_role_on_reassigned_pages():
    pages = [
        _page(1, "electrical_drawings", "plan"),
        _page(2, "electrical_drawings", "plan"),
        _page(3, "mechanical_drawings", "schedule", title="M-601 Schedules"),
        _page(4, "electrical_drawings", "diagram"),
        _page(5, "electrical_drawings", "detail"),
    ]
    repaired = bid_split.repair_islands(pages, max_island=1)
    assert repaired[2]["category"] == "electrical_drawings"
    assert repaired[2]["page_role"] == "schedule"
    assert repaired[2]["title"] == "M-601 Schedules"
    assert repaired[2]["reassigned_from"] == "Mechanical Drawings"


def test_merge_pages_keeps_each_role_on_its_page_entry():
    pages = [
        _page(1, "general_drawings", "cover_index", title="G-001 Cover"),
        _page(2, "general_drawings", "legend_symbols", title=None),  # untitled
        _page(3, "electrical_drawings", "plan", title="E-101"),
    ]
    segs = bid_split.merge_pages(pages)
    assert "roles" not in segs[0]
    assert [p["page_role"] for p in segs[0]["pages"]] == ["cover_index", "legend_symbols"]
    assert segs[0]["titles"] == ["G-001 Cover"]  # titles still skip untitled pages
    # The untitled legend still reaches page_meta through its page entry.
    meta = bid_split.segment_page_meta(segs[0]["pages"], 1, 2)
    assert [m["page_role"] for m in meta] == ["cover_index", "legend_symbols"]


# ── segment_page_meta ────────────────────────────────────────────────────


def _meta(page, pdf_page, role, title=None, confidence=0.9, prepended=False):
    return {
        "page": page, "pdf_page": pdf_page, "prepended": prepended,
        "title": title, "page_role": role, "confidence": confidence,
    }


def test_segment_page_meta_shape_and_range():
    entries = [
        _page(1, "general_drawings", "cover_index", title="G-001 Cover", confidence=0.95),
        _page(2, "electrical_drawings", "plan", title="E-101 / Power", confidence=1.7),
        _page(3, "electrical_drawings", role="bogus", title=None, confidence=-1),
    ]
    assert bid_split.segment_page_meta(entries, 2, 3) == [
        _meta(2, 1, "plan", "E-101 - Power", 1.0),
        _meta(3, 2, "other", None, 0.0),
    ]


def test_segment_page_meta_lists_the_prepended_cover_pages_first():
    entries = [
        _page(1, "general_drawings", "cover_index", title="G-001", confidence=0.95),
        _page(2, "general_drawings", "legend_symbols", title="G-002"),
        _page(3, "electrical_drawings", "plan", title="E-101"),
        _page(4, "electrical_drawings", "schedule", title="E-601"),
    ]
    meta = bid_split.segment_page_meta(entries, 3, 4, [1, 2])
    assert meta == [
        _meta(1, 1, "cover_index", "G-001", 0.95, prepended=True),
        _meta(2, 2, "legend_symbols", "G-002", prepended=True),
        _meta(3, 3, "plan", "E-101"),
        _meta(4, 4, "schedule", "E-601"),
    ]
    # A missing cover page is a missing page: no partial list.
    assert bid_split.segment_page_meta(entries[1:], 3, 4, [1, 2]) is None


def test_segment_page_meta_is_none_on_a_damaged_confidence():
    entries = [_page(1, "rfp", "spec_text"), _page(2, "rfp", "spec_text", confidence="x")]
    assert bid_split.segment_page_meta(entries, 1, 2) is None


def test_segment_prefix_pages_matches_the_cut_rule():
    trade = {"category": "electrical_drawings", "page_start": 3, "page_end": 6}
    assert bid_split.segment_prefix_pages(trade, [1, 2], 6) == [1, 2]
    assert bid_split.segment_prefix_pages({**trade, "category": "specifications"}, [1, 2], 6) == []
    assert bid_split.segment_prefix_pages({**trade, "category": "general_drawings"}, [1, 2], 6) == []
    # A whole-file segment is the untouched source: nothing prepended.
    assert bid_split.segment_prefix_pages({**trade, "page_start": 1}, [], 6) == []


@pytest.mark.parametrize(
    "category, role", [("specifications", "spec_text"), ("rfp", "spec_text"),
                       ("addenda", "spec_text"), ("other", "other")],
)
def test_intact_page_meta_carries_the_triage_verdict(category, role):
    assert bid_split.intact_page_meta(category, 2, 0.87654) == [
        _meta(1, 1, role, None, 0.877),
        _meta(2, 2, role, None, 0.877),
    ]


@pytest.mark.parametrize("pages, confidence", [(0, 0.9), (None, 0.9), (3, None), (3, "x")])
def test_intact_page_meta_is_none_on_damaged_input(pages, confidence):
    assert bid_split.intact_page_meta("specifications", pages, confidence) is None


@pytest.mark.parametrize(
    "entries, start, end",
    [
        (None, 1, 1),
        ([], 1, 1),
        ([_page(1, "rfp")], 1, 2),  # page 2 missing: no partial list
        ([_page(1, "rfp")], 2, 1),  # inverted range
        ([_page(1, "rfp")], 0, 1),
    ],
)
def test_segment_page_meta_is_none_without_full_coverage(entries, start, end):
    assert bid_split.segment_page_meta(entries, start, end) is None


# ── execute(): page_meta on the inserted rows ────────────────────────────


def _install_execute(monkeypatch, db, *, verdict, page_answers, total_pages=6):
    """Fake the model, renderer and storage around a real execute() run.
    `page_answers` maps page -> classification entry (missing keys allowed,
    like an older model output)."""
    events = SimpleNamespace(resync=[], whole=[], notified=[], uploads={})
    monkeypatch.setattr(bid_split, "get_supabase", lambda: db)
    monkeypatch.setattr(bid_split.llm, "active_model", lambda *a, **k: "test-model")
    monkeypatch.setattr(bid_split.llm, "resolve", lambda *a, **k: SimpleNamespace(provider="test"))
    monkeypatch.setattr(
        bid_split.pdf_split, "render_pages", lambda pdf, idx, **k: [b"img"] * len(idx)
    )

    def _images_json(feature, *, schema_name, images, **k):
        if schema_name == "file_triage":
            return dict(verdict)
        pages = [int(label.split()[1]) for label, _ in images]
        return {"pages": [page_answers[p] for p in pages]}

    monkeypatch.setattr(bid_split.llm, "complete_images_json", _images_json)
    monkeypatch.setattr(
        bid_split.llm,
        "complete_json",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("naming down")),
    )
    monkeypatch.setattr(storage, "download_file", lambda path: _blank_pdf(total_pages))
    monkeypatch.setattr(
        storage, "upload_file", lambda path, content, ct, **k: events.uploads.__setitem__(path, content)
    )
    monkeypatch.setattr(storage, "delete_file", lambda path: None)
    monkeypatch.setattr(
        storage, "build_bid_split_output_path", lambda job_id, name: f"bid-splits/{job_id}/output/{name}"
    )
    monkeypatch.setattr(rfp_split, "resync_after_run", events.resync.append)
    monkeypatch.setattr(
        rfp_split, "file_project_job_whole_quietly", lambda fid, reason, **k: events.whole.append(fid)
    )
    monkeypatch.setattr(
        bid_split.notifications, "notify_user", lambda *a, **k: events.notified.append((a, k))
    )
    return events


def _execute_db(source="manual", project_id=None):
    return FakeDB(
        {
            "bid_split_jobs": [
                {"id": "j1", "status": "processing", "source": source, "project_id": project_id,
                 "created_by": "u1"}
            ],
            "bid_split_files": [
                {"id": "f1", "job_id": "j1", "filename": "SET.pdf",
                 "storage_path": "bid-splits/j1/source/SET.pdf", "size_bytes": 1000,
                 "status": "pending", "classified_from": None}
            ],
            "bid_split_segments": [],
        }
    )


_DRAWING_VERDICT = {
    "kind": "drawing_set", "kind_label": None, "title": None, "description": None,
    "confidence": 0.95, "confidence_reason": "title blocks",
}

_SET_PAGES = {
    1: _page(1, "general_drawings", "cover_index", title="G-001 Cover Sheet"),
    2: _page(2, "general_drawings", "legend_symbols", title="G-002 Symbols"),
    3: _page(3, "electrical_drawings", "plan", title="E-101 Power Plan"),
    4: _page(4, "mechanical_drawings", "schedule", title="E-601 Panel Schedules", confidence=0.4),
    5: _page(5, "electrical_drawings", role=None, title="E-501 Details"),
    6: _page(6, "electrical_drawings", "diagram", title="E-701 One-Line"),
}


def test_execute_writes_page_meta_on_every_cut_segment(monkeypatch):
    db = _execute_db()
    events = _install_execute(monkeypatch, db, verdict=_DRAWING_VERDICT, page_answers=_SET_PAGES)
    bid_split.execute("f1")
    segs = sorted(db.tables["bid_split_segments"], key=lambda r: r["sort_order"])
    assert [(s["category"], s["page_start"], s["page_end"]) for s in segs] == [
        ("general_drawings", 1, 2),
        ("electrical_drawings", 3, 6),
    ]
    assert segs[0]["page_meta"] == [
        _meta(1, 1, "cover_index", "G-001 Cover Sheet"),
        _meta(2, 2, "legend_symbols", "G-002 Symbols"),
    ]
    # The electrical object holds the two cover pages, then its own: its
    # page_meta follows the object, so pdf_page addresses it and its length
    # is the object's page count. The island page keeps its role, the
    # legacy page reads other.
    meta = segs[1]["page_meta"]
    assert [(m["page"], m["pdf_page"], m["prepended"]) for m in meta] == [
        (1, 1, True), (2, 2, True), (3, 3, False), (4, 4, False), (5, 5, False), (6, 6, False),
    ]
    assert [m["page_role"] for m in meta] == [
        "cover_index", "legend_symbols", "plan", "schedule", "other", "diagram",
    ]
    assert meta[3] == _meta(4, 4, "schedule", "E-601 Panel Schedules", 0.4)
    assert bid_split.pdf_split.page_count(events.uploads[segs[1]["storage_path"]]) == len(meta)
    # Pristine per-page output keeps the roles too (before island repair).
    frow = db.tables["bid_split_files"][0]
    stored = {p["page"]: p for p in frow["model_output"]["pages"]}
    assert stored[4]["category"] == "mechanical_drawings" and stored[4]["page_role"] == "schedule"
    assert stored[5]["page_role"] == "other"
    assert frow["status"] == "done"


def test_execute_whole_file_segment_from_the_page_pass_keeps_page_meta(monkeypatch):
    db = _execute_db()
    pages = {p: _page(p, "electrical_drawings", "plan", title=f"E-{p}") for p in range(1, 4)}
    _install_execute(monkeypatch, db, verdict=_DRAWING_VERDICT, page_answers=pages, total_pages=3)
    bid_split.execute("f1")
    [seg] = db.tables["bid_split_segments"]
    assert seg["is_original"] is True
    assert [m["page"] for m in seg["page_meta"]] == [1, 2, 3]
    assert not any(m["prepended"] for m in seg["page_meta"])


def test_execute_intact_triage_verdict_writes_triage_page_meta(monkeypatch):
    db = _execute_db()
    verdict = {**_DRAWING_VERDICT, "kind": "specifications", "title": "Project Manual"}
    _install_execute(monkeypatch, db, verdict=verdict, page_answers={}, total_pages=3)
    bid_split.execute("f1")
    [seg] = db.tables["bid_split_segments"]
    assert seg["is_original"] is True and seg["category"] == "specifications"
    assert seg["page_meta"] == [_meta(p, p, "spec_text", None, 0.95) for p in (1, 2, 3)]


# ── source = plans_reader: no project side effects ───────────────────────


def test_plans_reader_run_skips_the_project_resync_and_the_bell(monkeypatch):
    db = _execute_db(source="plans_reader", project_id="p-should-never-be-touched")
    events = _install_execute(monkeypatch, db, verdict=_DRAWING_VERDICT, page_answers=_SET_PAGES)
    bid_split.execute("f1")
    assert events.resync == []
    assert events.notified == []
    assert db.tables["bid_split_jobs"][0]["status"] == "done"
    assert len(db.tables["bid_split_segments"]) == 2


def test_manual_run_still_resyncs_and_rings_the_bell(monkeypatch):
    db = _execute_db(source="manual")
    events = _install_execute(monkeypatch, db, verdict=_DRAWING_VERDICT, page_answers=_SET_PAGES)
    bid_split.execute("f1")
    assert events.resync == ["f1"]
    assert len(events.notified) == 1
    assert events.notified[0][0][2] == bid_split.JOB_FINISHED_NOTIFICATION


def test_plans_reader_failed_file_is_never_filed_whole(monkeypatch):
    db = _execute_db(source="plans_reader")
    events = _install_execute(monkeypatch, db, verdict=_DRAWING_VERDICT, page_answers={})
    bid_split._mark("f1", status="failed", error="boom")
    assert events.whole == [] and events.notified == []
    assert db.tables["bid_split_files"][0]["status"] == "failed"
    assert db.tables["bid_split_jobs"][0]["status"] == "failed"


def test_manual_failed_file_still_reaches_the_whole_file_hook(monkeypatch):
    db = _execute_db(source="manual")
    events = _install_execute(monkeypatch, db, verdict=_DRAWING_VERDICT, page_answers={})
    bid_split._mark("f1", status="failed", error="boom")
    assert events.whole == ["f1"]


def test_unreadable_job_source_defers_to_the_project_hooks(monkeypatch):
    class _Broken:
        def table(self, name):
            raise RuntimeError("db down")

    assert bid_split.job_source("j1", sb=_Broken()) is None
    assert bid_split.job_source(None) is None
    db = FakeDB({"bid_split_jobs": [{"id": "j1", "source": None}]})
    assert bid_split.job_source("j1", sb=db) == "manual"


def test_project_for_job_ignores_a_plans_reader_project_id(monkeypatch):
    db = FakeDB({
        "bid_split_jobs": [
            {"id": "j1", "source": "plans_reader", "project_id": "p1"},
            {"id": "j2", "source": "rfp", "project_id": "p2"},
        ]
    })
    assert bs._project_for_job(db, "j1") is None
    assert bs._project_for_job(db, "j2") == "p2"


def test_attach_projects_gives_plans_reader_jobs_no_project(monkeypatch):
    db = FakeDB({"projects": [{"id": "p1", "number": "26.10.1", "name": "Job"}]})
    monkeypatch.setattr(bs, "handoff_locked", lambda pid: True)
    jobs = [
        {"id": "j1", "source": "plans_reader", "project_id": "p1"},
        {"id": "j2", "source": "rfp", "project_id": "p1"},
    ]
    bs._attach_projects(db, jobs)
    assert jobs[0]["project_number"] is None and jobs[0]["package_sent"] is False
    assert jobs[1]["project_number"] == "26.10.1" and jobs[1]["package_sent"] is True


# ── The AI Estimating split-done hook (docs/PLANS_READER.md 7) ───────────


@pytest.fixture(autouse=True)
def runs_hook(monkeypatch):
    """A stand-in plans_reader_runs module recording on_split_file_done.
    Autouse: no test here may reach the real run module (it talks to
    Supabase) through a plans_reader job's terminal mark."""
    import app.services as services_pkg

    fake = ModuleType("app.services.plans_reader_runs")
    fake.calls = []
    fake.on_split_file_done = fake.calls.append
    monkeypatch.setitem(sys.modules, "app.services.plans_reader_runs", fake)
    monkeypatch.setattr(services_pkg, "plans_reader_runs", fake, raising=False)
    return fake


def test_plans_reader_done_file_fires_the_run_hook(monkeypatch, runs_hook):
    db = _execute_db(source="plans_reader")
    events = _install_execute(monkeypatch, db, verdict=_DRAWING_VERDICT, page_answers=_SET_PAGES)
    seen = []
    runs_hook.on_split_file_done = lambda fid: seen.append(
        (fid, db.tables["bid_split_files"][0]["status"], len(db.tables["bid_split_segments"]))
    )
    bid_split.execute("f1")
    # Once, after the done mark and the segment rows landed; no project hook.
    assert seen == [("f1", "done", 2)]
    assert events.resync == []


def test_plans_reader_failed_file_fires_the_run_hook(monkeypatch, runs_hook):
    db = _execute_db(source="plans_reader")
    events = _install_execute(monkeypatch, db, verdict=_DRAWING_VERDICT, page_answers={})
    bid_split._mark("f1", status="failed", error="boom")
    assert runs_hook.calls == ["f1"] and events.whole == []


def test_manual_job_never_fires_the_run_hook(monkeypatch, runs_hook):
    db = _execute_db(source="manual")
    _install_execute(monkeypatch, db, verdict=_DRAWING_VERDICT, page_answers=_SET_PAGES)
    bid_split.execute("f1")
    bid_split._mark("f1", status="failed", error="boom")
    assert runs_hook.calls == []


def test_run_hook_missing_or_broken_never_fails_the_mark(monkeypatch, runs_hook):
    db = _execute_db(source="plans_reader")
    _install_execute(monkeypatch, db, verdict=_DRAWING_VERDICT, page_answers={})
    del runs_hook.on_split_file_done  # phase 2 not landed yet
    bid_split._mark("f1", status="failed", error="boom")
    assert db.tables["bid_split_files"][0]["status"] == "failed"

    def _boom(fid):
        raise RuntimeError("run hook down")

    runs_hook.on_split_file_done = _boom
    bid_split._after_done("f1", "j1")  # logged, never raised


# ── stage_upload: the job reads processing while a file waits ────────────


def test_stage_upload_moves_a_settled_job_back_to_processing(monkeypatch):
    db = FakeDB({
        "bid_split_jobs": [{"id": "j1", "status": "done", "completed_at": "2026-10-17T01:00:00Z",
                            "file_count": 1, "source": "plans_reader"}],
        "bid_split_files": [{"id": "f0", "job_id": "j1", "status": "done"}],
    })
    monkeypatch.setattr(storage, "upload_file", lambda *a, **k: None)
    monkeypatch.setattr(storage, "build_bid_split_source_path", lambda job_id, name: f"bid-splits/{job_id}/source/{name}")
    frow = bid_split.stage_upload(db, {"id": "j1"}, "B.pdf", b"%PDF-1.4", 3, "u1")
    assert frow["status"] == "pending"
    job = db.tables["bid_split_jobs"][0]
    assert (job["status"], job["completed_at"], job["file_count"]) == ("processing", None, 2)


# ── sweep_stale_files ────────────────────────────────────────────────────


_NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def _sweep_db(source="plans_reader"):
    old, fresh = "2026-10-17T08:00:00Z", "2026-10-17T11:50:00Z"
    return FakeDB({
        "bid_split_jobs": [{"id": "j1", "status": "processing", "source": source, "created_by": "u1"}],
        "bid_split_files": [
            {"id": "f-old", "job_id": "j1", "status": "running", "started_at": old,
             "updated_at": old, "created_at": old},
            {"id": "f-fresh", "job_id": "j1", "status": "running", "started_at": old,
             "updated_at": fresh, "created_at": old},
            {"id": "f-queued", "job_id": "j1", "status": "pending", "started_at": None,
             "updated_at": old, "created_at": old},
            {"id": "f-done", "job_id": "j1", "status": "done", "started_at": old,
             "updated_at": old, "created_at": old},
        ],
    })


def test_sweep_fails_only_orphaned_plans_reader_files(monkeypatch, runs_hook):
    db = _sweep_db()
    monkeypatch.setattr(bid_split, "get_supabase", lambda: db)
    monkeypatch.setattr(
        llm_queue, "active_job", lambda jt, fid: {"id": "q"} if fid == "f-queued" else None
    )
    assert bid_split.sweep_stale_files("j1", now=_NOW) == ["f-old"]
    rows = {r["id"]: r for r in db.tables["bid_split_files"]}
    assert rows["f-old"]["status"] == "failed" and rows["f-old"]["error"] == bid_split.MSG_STALE_RUN
    assert rows["f-fresh"]["status"] == "running" and rows["f-queued"]["status"] == "pending"
    # The failed mark reaches the run like any other failure.
    assert runs_hook.calls == ["f-old"]


def test_sweep_leaves_a_file_alone_when_the_queue_lookup_fails(monkeypatch, runs_hook):
    db = _sweep_db()
    monkeypatch.setattr(bid_split, "get_supabase", lambda: db)

    def _down(*a):
        raise RuntimeError("queue down")

    monkeypatch.setattr(llm_queue, "active_job", _down)
    assert bid_split.sweep_stale_files("j1", now=_NOW) == []
    assert runs_hook.calls == []


def test_sweep_refuses_a_splitter_page_job(monkeypatch):
    db = _sweep_db(source="manual")
    monkeypatch.setattr(bid_split, "get_supabase", lambda: db)
    monkeypatch.setattr(llm_queue, "active_job", lambda *a: None)
    assert bid_split.sweep_stale_files("j1", now=_NOW) == []
    assert db.tables["bid_split_files"][0]["status"] == "running"


def _plans_reader_router_db(file_status="done"):
    jobs = [{"id": "j1", "model": "m", "status": "done", "source": "plans_reader", "project_id": None}]
    frow = {**FILE_ROW, "status": file_status}
    return FakeDB(_tables(bid_split_jobs=jobs, bid_split_files=[frow]))


def test_plans_reader_job_cannot_be_deleted_here(monkeypatch):
    db = _plans_reader_router_db()
    swept = []
    monkeypatch.setattr(bs, "get_supabase", lambda: db)
    monkeypatch.setattr(bs, "audit", lambda *a, **k: None)
    monkeypatch.setattr(storage, "delete_bid_split_prefix", swept.append)
    with pytest.raises(HTTPException) as exc:
        bs.delete_job("j1", user=SimpleNamespace(id="u1"))
    assert exc.value.status_code == 409 and exc.value.detail == bs.MSG_PLANS_READER_JOB
    assert swept == [] and len(db.tables["bid_split_jobs"]) == 1


@pytest.mark.parametrize(
    "file_status, call",
    [
        ("failed", lambda: bs.retry_file("f1", SimpleNamespace(add_task=None), user=SimpleNamespace(id="u1"))),
        ("done", lambda: bs.reprocess_file("f1", SimpleNamespace(add_task=None), user=SimpleNamespace(id="u1"))),
        ("done", lambda: bs.correct_file_kind(
            "f1", bs.BidSplitFileKindIn(file_kind="specifications"), user=SimpleNamespace(id="u1"))),
        ("done", lambda: _put(None, [_seg_in("electrical_drawings", 1, 6)])),
    ],
    ids=["retry", "reprocess", "retype", "edit-segments"],
)
def test_plans_reader_job_files_cannot_be_changed_here(monkeypatch, file_status, call):
    db = _plans_reader_router_db(file_status)
    calls = _install(monkeypatch, db)
    monkeypatch.setattr(bs.llm, "is_configured", lambda *a, **k: True)
    monkeypatch.setattr(bid_split, "dispatch", lambda *a, **k: pytest.fail("must not dispatch"))
    before = [dict(r) for r in db.tables["bid_split_segments"]]
    with pytest.raises(HTTPException) as exc:
        call()
    assert exc.value.status_code == 409 and exc.value.detail == bs.MSG_PLANS_READER_JOB
    assert db.tables["bid_split_segments"] == before
    assert db.tables["bid_split_files"][0]["status"] == file_status
    assert calls.deleted == [] and calls.uploaded == {}


def test_plans_reader_job_takes_no_upload_here(monkeypatch):
    import asyncio

    db = _plans_reader_router_db()
    monkeypatch.setattr(bs, "get_supabase", lambda: db)
    monkeypatch.setattr(bid_split, "stage_upload", lambda *a, **k: pytest.fail("must not stage"))
    upload = SimpleNamespace(filename="x.pdf")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(bs.upload_job_file("j1", SimpleNamespace(), file=upload, user=SimpleNamespace(id="u1")))
    assert exc.value.status_code == 409 and exc.value.detail == bs.MSG_PLANS_READER_JOB


def test_list_jobs_source_filter(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        bs.list_jobs(limit=10, offset=0, status_filter=None, _=None, source_filter="email")
    assert exc.value.status_code == 422
    db = FakeDB({
        "bid_split_jobs": [
            {"id": "j1", "source": "manual", "created_at": "2026-10-17T01:00:00Z"},
            {"id": "j2", "source": "plans_reader", "created_at": "2026-10-17T02:00:00Z"},
        ],
        "bid_split_files": [],
    })
    monkeypatch.setattr(bs, "get_supabase", lambda: db)
    only = bs.list_jobs(limit=10, offset=0, status_filter=None, _=None, source_filter="plans_reader")
    assert [j["id"] for j in only] == ["j2"]
    # The default list (the splitter history and the app shell's in-flight
    # poll) never shows AI Estimating's internal jobs.
    default = bs.list_jobs(limit=10, offset=0, status_filter=None, _=None)
    assert [j["id"] for j in default] == ["j1"]
    db.tables["bid_split_jobs"][1]["status"] = "processing"
    db.tables["bid_split_jobs"][0]["status"] = "done"
    assert bs.list_jobs(limit=10, offset=0, status_filter="processing", _=None) == []


# ── Router: page_meta on the segment-edit and re-type paths ──────────────


_FULL_MODEL_PAGES = [
    _page(p, "electrical_drawings", role, title=f"Sheet {p}", confidence=0.5 + p / 20)
    for p, role in zip(range(1, 7), ["cover_index", "legend_symbols", "plan", "schedule", "detail", "diagram"])
]


def _router_db(**seg_over):
    frow = {**FILE_ROW, "model_output": {"triage": {"kind": "drawing_set"}, "pages": _FULL_MODEL_PAGES}}
    general = {**SEG_GENERAL, **seg_over}
    return FakeDB(_tables(bid_split_files=[frow], bid_split_segments=[general, dict(SEG_ELECTRICAL)]))


def test_put_segments_builds_page_meta_from_the_model_pages(monkeypatch):
    kept = [_meta(1, 1, "cover_index", "kept"), _meta(2, 2, "legend_symbols", "kept")]
    db = _router_db(page_meta=kept)
    _install(monkeypatch, db)
    out = _put(db, [_seg_in("general_drawings", 1, 2), _seg_in("electrical_drawings", 3, 4),
                    _seg_in("plumbing_drawings", 5, 6)])
    segs = _segments(db)
    # Untouched row keeps its stored detail; re-cut rows rebuild theirs.
    assert segs[0]["page_meta"] == kept
    # Re-cut trade rows list the prepended covers (pages 1-2) first.
    assert segs[1]["page_meta"] == [
        _meta(1, 1, "cover_index", "Sheet 1", 0.55, prepended=True),
        _meta(2, 2, "legend_symbols", "Sheet 2", 0.6, prepended=True),
        _meta(3, 3, "plan", "Sheet 3", 0.65),
        _meta(4, 4, "schedule", "Sheet 4", 0.7),
    ]
    assert [m["page_role"] for m in segs[2]["page_meta"] if not m["prepended"]] == ["detail", "diagram"]
    # The router serves page_meta with every segment (select *).
    assert [s["page_meta"] for s in out["segments"]] == [s["page_meta"] for s in segs]


def test_put_segments_reused_row_without_page_meta_is_backfilled(monkeypatch):
    db = _router_db()  # pre-0152 row: no page_meta
    _install(monkeypatch, db)
    _put(db, [_seg_in("general_drawings", 1, 2), _seg_in("electrical_drawings", 3, 6)])
    segs = _segments(db)
    assert [m["page"] for m in segs[0]["page_meta"]] == [1, 2]
    assert [m["page"] for m in segs[1]["page_meta"]] == [1, 2, 3, 4, 5, 6]
    assert [m["pdf_page"] for m in segs[1]["page_meta"]] == [1, 2, 3, 4, 5, 6]


def test_put_segments_without_a_page_pass_stores_null(monkeypatch):
    frow = {**FILE_ROW, "model_output": {"triage": {"kind": "specifications"}}}
    db = FakeDB(_tables(bid_split_files=[frow]))
    _install(monkeypatch, db)
    _put(db, [_seg_in("rfp", 1, 2), _seg_in("specifications", 3, 6)])
    assert [s["page_meta"] for s in _segments(db)] == [None, None]


def test_retype_intact_keeps_page_meta_when_a_page_pass_ran(monkeypatch):
    db = _router_db()
    _install(monkeypatch, db)
    bs.correct_file_kind("f1", bs.BidSplitFileKindIn(file_kind="specifications"), user=SimpleNamespace(id="u1"))
    [seg] = _segments(db)
    assert seg["is_original"] is True
    assert [m["page"] for m in seg["page_meta"]] == [1, 2, 3, 4, 5, 6]


# ── dispatch(background=None) ────────────────────────────────────────────


class _FakeThread:
    started: list = []

    def __init__(self, target, args, name, daemon):
        self.target, self.args, self.name, self.daemon = target, args, name, daemon

    def start(self):
        _FakeThread.started.append(self)


@pytest.fixture
def dispatch_env(monkeypatch):
    env = SimpleNamespace(enqueued=[], queue_on=True, enqueue_error=None)
    _FakeThread.started = []

    def _enqueue(job_type, **k):
        if env.enqueue_error:
            raise env.enqueue_error
        env.enqueued.append((job_type, k))
        return {"id": "q1"}

    monkeypatch.setattr(llm_queue, "enqueue", _enqueue)
    monkeypatch.setattr(
        bid_split, "get_settings", lambda: Settings(_env_file=None, llm_queue_enabled=env.queue_on)
    )
    monkeypatch.setattr(bid_split.threading, "Thread", _FakeThread)
    return env


def test_dispatch_without_background_enqueues_only(dispatch_env):
    bid_split.dispatch(None, "f1", "u1")
    assert dispatch_env.enqueued == [
        (llm_queue.JOB_BID_SPLIT, {"target_id": "f1", "payload": {"file_id": "f1"}, "created_by": "u1"})
    ]
    assert _FakeThread.started == []


def test_dispatch_without_background_falls_back_to_a_thread_when_the_queue_is_off(dispatch_env):
    dispatch_env.queue_on = False
    bid_split.dispatch(None, "f1", "u1", forced_kind="mixed")
    assert dispatch_env.enqueued == []
    [thread] = _FakeThread.started
    assert thread.target is bid_split.run_file
    assert thread.args == ("f1", "mixed") and thread.daemon is True


def test_dispatch_without_background_falls_back_to_a_thread_when_enqueue_fails(dispatch_env):
    dispatch_env.enqueue_error = RuntimeError("queue down")
    bid_split.dispatch(None, "f1", "u1")
    [thread] = _FakeThread.started
    assert thread.args == ("f1", None)


def test_dispatch_with_background_still_uses_it(dispatch_env):
    dispatch_env.queue_on = False
    tasks = []
    bid_split.dispatch(SimpleNamespace(add_task=lambda *a: tasks.append(a)), "f1", "u1")
    assert tasks == [(bid_split.run_file, "f1", None)]
    assert _FakeThread.started == []

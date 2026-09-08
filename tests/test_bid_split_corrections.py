"""Bid File Splitter user corrections + training capture (0113).

Covers:

  PATCH /files/{id}          file-kind correction: label rules, 409 on
                             non-done, collapse to a single is_original
                             segment on intact kinds (cut objects swept, the
                             source object never), drawing_set/mixed leave
                             segments alone, confidence nulled, no-op
                             short-circuit.
  PUT /files/{id}/segments   full-replacement segment edit: the coverage
                             validation matrix, row replacement through the
                             shared cut_segment path (whole-file passthrough,
                             cover-sheet prepend), LLM names preserved on
                             untouched rows, file_kind re-derived.
  cut_segment                whole-file passthrough and prefix/no-prefix
                             output filenames.
  capture_correction         first correction freezes the model side (raw
                             output + input snapshot + source-PDF copy),
                             later corrections replace only the user side and
                             reset the review, page-run diff correctness, and
                             a capture failure never fails the endpoint.
  /training/bid-split        dev gate, light list (counts + flags only),
                             full detail, review toggle.
  snapshot helpers           _persist_training_io writes both jsonbs;
                             _snapshot_pages stores a DEEP copy (island
                             repair mutates pages_meta afterwards).

The live LLM pipeline stays unstubbed (suite policy); everything here runs
against hand-rolled supabase/storage fakes with handlers invoked directly.
"""

import io
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.deps import require_dev
from app.models.schemas import TrainingReviewIn
from app.routers import bid_splitter as bs
from app.routers import training as training_router
from app.services import bid_split, bid_split_training, storage


def _blank_pdf(pages: int) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ── Fake Supabase (in-memory, honors explicit selects) ───────────────────


def _split_select(sel: str) -> list[str]:
    """Split a select string on top-level commas (embeds carry parens)."""
    parts, depth, cur = [], 0, ""
    for ch in sel:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


class _Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self._op = None
        self._payload = None
        self._on_conflict = None
        self._filters = []
        self._sel = "*"
        self._order = None
        self._desc = False
        self._limit = None
        self._range = None

    def select(self, sel="*", *a, **k):
        self._op, self._sel = "select", sel
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
        self._filters.append((col, val))
        return self

    def in_(self, col, vals):
        self._filters.append((col, list(vals)))
        return self

    def order(self, col, desc=False, **k):
        self._order, self._desc = col, desc
        return self

    def limit(self, n, *a, **k):
        self._limit = n
        return self

    def range(self, start, end, *a, **k):
        self._range = (start, end)
        return self

    def _matches(self, row):
        return all(
            row.get(c) in v if isinstance(v, list) else row.get(c) == v
            for c, v in self._filters
        )

    def _project(self, row):
        """Honor an explicit column list (the real PostgREST would), and
        resolve the training routes' profile embeds."""
        if self._sel.strip().startswith("*"):
            out = dict(row)
        else:
            out = {}
            for part in _split_select(self._sel):
                col = part.strip()
                if not col or "(" in col:
                    continue
                out[col] = row.get(col)
        if self.table == "bid_split_training_examples" and "profiles!" in self._sel:
            profiles = self.db.tables.get("profiles", [])
            for alias, fk in (
                ("corrected_by_profile", "corrected_by"),
                ("reviewed_by_profile", "reviewed_by"),
            ):
                if alias in self._sel:
                    out[alias] = next(
                        (
                            {"full_name": p["full_name"]}
                            for p in profiles
                            if p["id"] == row.get(fk)
                        ),
                        None,
                    )
        return out

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == "select":
            hits = [r for r in rows if self._matches(r)]
            if self._order:
                hits = sorted(
                    hits,
                    key=lambda r: (r.get(self._order) is None, r.get(self._order)),
                    reverse=self._desc,
                )
            total = len(hits)
            if self._range is not None:
                hits = hits[self._range[0] : self._range[1] + 1]
            if self._limit is not None:
                hits = hits[: self._limit]
            return SimpleNamespace(data=[self._project(r) for r in hits], count=total)
        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for i, p in enumerate(payloads):
                row = dict(p)
                row.setdefault("id", f"{self.table}-{len(rows) + i + 1}")
                rows.append(row)
                out.append(dict(row))
            return SimpleNamespace(data=out)
        if self._op == "upsert":
            row = dict(self._payload)
            key = self._on_conflict
            existing = next((r for r in rows if key and r.get(key) == row.get(key)), None)
            if existing:
                existing.update(row)
                return SimpleNamespace(data=[dict(existing)])
            row.setdefault("id", f"{self.table}-{len(rows) + 1}")
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


class FakeDB:
    def __init__(self, tables=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}

    def table(self, name):
        return _Query(self, name)


# ── Fixtures ─────────────────────────────────────────────────────────────

RAW_MODEL_OUTPUT = {
    "triage": {"kind": "drawing_set", "kind_label": None, "confidence": 0.9},
    "pages": [{"page": 1, "category": "general_drawings"}],
}

INPUT_SNAPSHOT = {"model": "claude-opus-5", "triage": {"sample_pages": [1, 2, 3, 6]}}

FILE_ROW = {
    "id": "f1",
    "job_id": "j1",
    "filename": "SET.pdf",
    "storage_path": "bid-splits/j1/source/SET.pdf",
    "size_bytes": 1000,
    "page_count": 6,
    "status": "done",
    "error": None,
    "llm_calls": 2,
    "llm_ms": 100,
    "file_kind": "drawing_set",
    "file_kind_label": None,
    "file_kind_confidence": 0.9,
    "user_corrected": False,
    "started_at": None,
    "finished_at": None,
    "created_at": "2026-08-26T00:00:00Z",
    "updated_at": "2026-08-26T00:00:00Z",
    "input_snapshot": INPUT_SNAPSHOT,
    "model_output": RAW_MODEL_OUTPUT,
}

SEG_GENERAL = {
    "id": "s-g",
    "file_id": "f1",
    "sort_order": 0,
    "category": "general_drawings",
    "other_type": None,
    "name": "Cover",
    "description": "Covers.",
    "confidence": 0.9,
    "page_start": 1,
    "page_end": 2,
    "storage_path": "bid-splits/j1/output/g.pdf",
    "filename": "Cover (pages 1-2).pdf",
    "size_bytes": 10,
    "is_original": False,
}

SEG_ELECTRICAL = {
    "id": "s-e",
    "file_id": "f1",
    "sort_order": 1,
    "category": "electrical_drawings",
    "other_type": None,
    "name": "E-Sheets",
    "description": "Power plans.",
    "confidence": 0.8,
    "page_start": 3,
    "page_end": 6,
    "storage_path": "bid-splits/j1/output/e.pdf",
    "filename": "E-Sheets (pages 3-6).pdf",
    "size_bytes": 10,
    "is_original": False,
}


def _tables(**over):
    t = {
        "bid_split_jobs": [{"id": "j1", "model": "claude-opus-5", "status": "done"}],
        "bid_split_files": [dict(FILE_ROW)],
        "bid_split_segments": [dict(SEG_GENERAL), dict(SEG_ELECTRICAL)],
        "bid_split_training_examples": [],
        "profiles": [
            {"id": "u1", "full_name": "Tom Writer"},
            {"id": "dev1", "full_name": "Dev One"},
        ],
    }
    t.update(over)
    return t


def _install(monkeypatch, db, pdf=None):
    """Point the correction endpoints (router + services) at the fakes."""
    calls = SimpleNamespace(deleted=[], uploaded={}, copied=[], audits=[])
    monkeypatch.setattr(bs, "get_supabase", lambda: db)
    monkeypatch.setattr(bid_split, "get_supabase", lambda: db)
    monkeypatch.setattr(bs.llm_queue, "active_job", lambda *a, **k: None)
    monkeypatch.setattr(bs, "audit", lambda *a, **k: calls.audits.append(a))
    monkeypatch.setattr(storage, "download_file", lambda path: pdf or _blank_pdf(6))
    monkeypatch.setattr(
        storage,
        "upload_file",
        lambda path, content, ct, **k: calls.uploaded.__setitem__(path, content),
    )
    monkeypatch.setattr(storage, "delete_file", calls.deleted.append)
    monkeypatch.setattr(
        storage, "copy_file", lambda src, dst: calls.copied.append((src, dst))
    )
    monkeypatch.setattr(
        storage,
        "build_bid_split_output_path",
        lambda job_id, name: f"bid-splits/{job_id}/output/{name}",
    )
    return calls


_WRITER = SimpleNamespace(id="u1")


def _segments(db):
    return sorted(
        (r for r in db.tables["bid_split_segments"] if r["file_id"] == "f1"),
        key=lambda r: r["sort_order"],
    )


# ── PATCH /files/{id}: schema rules ──────────────────────────────────────


def test_kind_label_required_for_other():
    with pytest.raises(ValidationError):
        bs.BidSplitFileKindIn(file_kind="other")
    with pytest.raises(ValidationError):
        bs.BidSplitFileKindIn(file_kind="other", file_kind_label="   ")
    body = bs.BidSplitFileKindIn(file_kind="other", file_kind_label=" Geotechnical Report ")
    assert body.file_kind_label == "Geotechnical Report"


def test_kind_label_forced_none_off_other():
    body = bs.BidSplitFileKindIn(file_kind="rfp", file_kind_label="ignored")
    assert body.file_kind_label is None


def test_kind_vocabulary_comes_from_the_service_constant():
    with pytest.raises(ValidationError):
        bs.BidSplitFileKindIn(file_kind="spec_book")
    for kind in bid_split.FILE_KINDS:
        label = "X" if kind == "other" else None
        assert bs.BidSplitFileKindIn(file_kind=kind, file_kind_label=label).file_kind == kind


# ── PATCH /files/{id}: behavior ──────────────────────────────────────────


def test_correct_kind_409_unless_done(monkeypatch):
    db = FakeDB(_tables(bid_split_files=[{**FILE_ROW, "status": "running"}]))
    _install(monkeypatch, db)
    with pytest.raises(HTTPException) as exc:
        bs.correct_file_kind("f1", bs.BidSplitFileKindIn(file_kind="rfp"), user=_WRITER)
    assert exc.value.status_code == 409


def test_correct_kind_collapses_to_the_intact_shape(monkeypatch):
    db = FakeDB(_tables())
    calls = _install(monkeypatch, db)
    out = bs.correct_file_kind(
        "f1", bs.BidSplitFileKindIn(file_kind="specifications"), user=_WRITER
    )
    # The old cut objects are swept; the source object never is.
    assert calls.deleted == [
        "bid-splits/j1/output/g.pdf",
        "bid-splits/j1/output/e.pdf",
    ]
    assert FILE_ROW["storage_path"] not in calls.deleted
    segs = _segments(db)
    assert len(segs) == 1
    seg = segs[0]
    assert seg["is_original"] is True
    assert seg["category"] == "specifications" and seg["other_type"] is None
    assert (seg["page_start"], seg["page_end"]) == (1, 6)
    assert seg["storage_path"] == FILE_ROW["storage_path"]
    assert seg["filename"] == "SET.pdf" and seg["size_bytes"] == 1000
    assert seg["confidence"] is None
    assert seg["name"] == "Specifications"
    assert seg["description"] == "Reclassified by the user; left intact."
    frow = db.tables["bid_split_files"][0]
    assert frow["file_kind"] == "specifications" and frow["file_kind_label"] is None
    assert frow["file_kind_confidence"] is None  # a user verdict, not an estimate
    assert frow["user_corrected"] is True
    # Response: file + segments, without the heavy training jsonbs.
    assert out["id"] == "f1" and len(out["segments"]) == 1
    assert "input_snapshot" not in out and "model_output" not in out
    assert calls.audits and calls.audits[0][1] == "bid_split.correct_kind"


def test_correct_kind_other_carries_the_label_onto_the_segment(monkeypatch):
    db = FakeDB(_tables())
    _install(monkeypatch, db)
    bs.correct_file_kind(
        "f1",
        bs.BidSplitFileKindIn(file_kind="other", file_kind_label="Geotechnical Report"),
        user=_WRITER,
    )
    seg = _segments(db)[0]
    assert seg["category"] == "other" and seg["other_type"] == "Geotechnical Report"
    assert seg["name"] == "Geotechnical Report"
    frow = db.tables["bid_split_files"][0]
    assert frow["file_kind_label"] == "Geotechnical Report"


def test_correct_kind_to_mixed_leaves_segments_alone(monkeypatch):
    db = FakeDB(_tables())
    calls = _install(monkeypatch, db)
    bs.correct_file_kind("f1", bs.BidSplitFileKindIn(file_kind="mixed"), user=_WRITER)
    assert calls.deleted == []
    assert [s["id"] for s in _segments(db)] == ["s-g", "s-e"]
    frow = db.tables["bid_split_files"][0]
    assert frow["file_kind"] == "mixed" and frow["file_kind_confidence"] is None
    assert frow["user_corrected"] is True


def test_correct_kind_noop_short_circuits(monkeypatch):
    db = FakeDB(_tables())
    calls = _install(monkeypatch, db)
    out = bs.correct_file_kind(
        "f1", bs.BidSplitFileKindIn(file_kind="drawing_set"), user=_WRITER
    )
    assert out["id"] == "f1" and len(out["segments"]) == 2
    assert db.tables["bid_split_training_examples"] == []  # no capture
    assert calls.audits == []  # no audit
    assert db.tables["bid_split_files"][0]["user_corrected"] is False


def test_correct_kind_intact_needs_a_page_count(monkeypatch):
    db = FakeDB(_tables(bid_split_files=[{**FILE_ROW, "page_count": None}]))
    _install(monkeypatch, db)
    with pytest.raises(HTTPException) as exc:
        bs.correct_file_kind("f1", bs.BidSplitFileKindIn(file_kind="rfp"), user=_WRITER)
    assert exc.value.status_code == 409


# ── POST /files/{id}/reprocess: forced re-split after a kind change ──────


def _install_reprocess(monkeypatch, db):
    """Reprocess needs the corrections fakes plus a configured model and a
    capturable dispatch."""
    calls = _install(monkeypatch, db)
    calls.dispatched = []
    monkeypatch.setattr(bs.llm, "is_configured", lambda *_a, **_k: True)
    monkeypatch.setattr(
        bs,
        "_dispatch",
        lambda background, file_id, user_id, forced_kind=None: calls.dispatched.append(
            (file_id, forced_kind)
        ),
    )
    return calls


def test_reprocess_409_unless_done(monkeypatch):
    db = FakeDB(_tables(bid_split_files=[{**FILE_ROW, "status": "running"}]))
    _install_reprocess(monkeypatch, db)
    with pytest.raises(HTTPException) as exc:
        bs.reprocess_file("f1", None, user=_WRITER)
    assert exc.value.status_code == 409


def test_reprocess_409_unless_a_split_kind(monkeypatch):
    db = FakeDB(_tables(bid_split_files=[{**FILE_ROW, "file_kind": "specifications"}]))
    _install_reprocess(monkeypatch, db)
    with pytest.raises(HTTPException) as exc:
        bs.reprocess_file("f1", None, user=_WRITER)
    assert exc.value.status_code == 409


def test_reprocess_409_while_a_run_is_in_flight(monkeypatch):
    db = FakeDB(_tables())
    _install_reprocess(monkeypatch, db)
    monkeypatch.setattr(bs.llm_queue, "active_job", lambda *a, **k: {"id": "job"})
    with pytest.raises(HTTPException) as exc:
        bs.reprocess_file("f1", None, user=_WRITER)
    assert exc.value.status_code == 409


def test_reprocess_marks_pending_and_forces_the_rows_kind(monkeypatch):
    db = FakeDB(_tables(bid_split_files=[{**FILE_ROW, "file_kind": "mixed"}]))
    calls = _install_reprocess(monkeypatch, db)
    out = bs.reprocess_file("f1", None, user=_WRITER)
    assert out == {"status": "pending"}
    assert db.tables["bid_split_files"][0]["status"] == "pending"
    # The forced kind comes off the file row, never a request body.
    assert calls.dispatched == [("f1", "mixed")]
    assert any(a[1] == "bid_split.reprocess" for a in calls.audits)


def test_retry_reforces_a_failed_forced_run(monkeypatch):
    row = {
        **FILE_ROW,
        "status": "failed",
        "file_kind": "drawing_set",
        "user_corrected": True,
    }
    db = FakeDB(_tables(bid_split_files=[row]))
    calls = _install_reprocess(monkeypatch, db)
    bs.retry_file("f1", None, user=_WRITER)
    assert calls.dispatched == [("f1", "drawing_set")]


def test_retry_stays_unforced_without_a_user_correction(monkeypatch):
    db = FakeDB(_tables(bid_split_files=[{**FILE_ROW, "status": "failed"}]))
    calls = _install_reprocess(monkeypatch, db)
    bs.retry_file("f1", None, user=_WRITER)
    assert calls.dispatched == [("f1", None)]


def test_execute_refuses_a_non_split_forced_kind():
    with pytest.raises(ValueError):
        bid_split.execute("f1", forced_kind="rfp")


def test_classify_context_forced_swaps_the_lead_in():
    verdict = {"kind": "drawing_set", "kind_label": None}
    assert bid_split._classify_context(verdict).startswith("A quick scan identified")
    forced = bid_split._classify_context(verdict, forced=True)
    assert forced.startswith("The user identified")
    assert "drawing set" in forced
    assert bid_split._classify_context(
        {"kind": "mixed", "kind_label": None}, forced=True
    ).startswith("The user identified")


# ── PUT /files/{id}/segments: validation matrix ──────────────────────────


def _seg_in(category, page_start, page_end, other_type=None):
    return {
        "category": category,
        "other_type": other_type,
        "page_start": page_start,
        "page_end": page_end,
    }


def _put(db, segments):
    return bs.correct_segments(
        "f1", bs.BidSplitSegmentsIn(segments=segments), user=_WRITER
    )


@pytest.mark.parametrize(
    "segments, fragment",
    [
        # start > end
        ([_seg_in("electrical_drawings", 1, 3), _seg_in("electrical_drawings", 3, 2)],
         "Segment 2"),
        # overlap
        ([_seg_in("electrical_drawings", 1, 3), _seg_in("plumbing_drawings", 3, 6)],
         "Segment 2"),
        # gap
        ([_seg_in("electrical_drawings", 1, 3), _seg_in("plumbing_drawings", 5, 6)],
         "Segment 2"),
        # not starting at 1
        ([_seg_in("electrical_drawings", 2, 6)], "must start at page 1"),
        # not ending at page_count
        ([_seg_in("electrical_drawings", 1, 5)], "the file has 6 pages"),
    ],
)
def test_put_segments_coverage_matrix(monkeypatch, segments, fragment):
    db = FakeDB(_tables())
    _install(monkeypatch, db)
    with pytest.raises(HTTPException) as exc:
        _put(db, segments)
    assert exc.value.status_code == 422
    assert fragment in exc.value.detail
    # Nothing moved: the old rows still stand.
    assert [s["id"] for s in _segments(db)] == ["s-g", "s-e"]


def test_put_segments_schema_rules():
    with pytest.raises(ValidationError):
        bs.BidSplitSegmentIn(category="other", page_start=1, page_end=2)  # label required
    with pytest.raises(ValidationError):
        bs.BidSplitSegmentIn(category="plumbing", page_start=1, page_end=2)  # unknown
    with pytest.raises(ValidationError):
        bs.BidSplitSegmentsIn(segments=[])  # empty replacement
    seg = bs.BidSplitSegmentIn(category="rfp", other_type="ignored", page_start=1, page_end=2)
    assert seg.other_type is None  # forced off non-other


def test_put_segments_409_on_null_page_count(monkeypatch):
    db = FakeDB(_tables(bid_split_files=[{**FILE_ROW, "page_count": None}]))
    _install(monkeypatch, db)
    with pytest.raises(HTTPException) as exc:
        _put(db, [_seg_in("electrical_drawings", 1, 6)])
    assert exc.value.status_code == 409


def test_put_segments_409_while_a_run_is_in_flight(monkeypatch):
    db = FakeDB(_tables())
    _install(monkeypatch, db)
    monkeypatch.setattr(bs.llm_queue, "active_job", lambda *a, **k: {"id": "q1"})
    with pytest.raises(HTTPException) as exc:
        _put(db, [_seg_in("electrical_drawings", 1, 6)])
    assert exc.value.status_code == 409


def test_put_segments_409_unless_done(monkeypatch):
    db = FakeDB(_tables(bid_split_files=[{**FILE_ROW, "status": "failed"}]))
    _install(monkeypatch, db)
    with pytest.raises(HTTPException) as exc:
        _put(db, [_seg_in("electrical_drawings", 1, 6)])
    assert exc.value.status_code == 409


# ── PUT /files/{id}/segments: behavior ───────────────────────────────────


def test_put_segments_replaces_rows_with_cover_prefix_and_names(monkeypatch):
    db = FakeDB(_tables())
    calls = _install(monkeypatch, db)
    extracted = []
    real_extract = bs.pdf_split.extract_pages
    monkeypatch.setattr(
        bid_split.pdf_split,
        "extract_pages",
        lambda pdf, pages: (extracted.append(list(pages)), real_extract(pdf, pages))[1],
    )
    out = _put(
        db,
        [
            _seg_in("general_drawings", 1, 2),
            _seg_in("electrical_drawings", 3, 4),
            _seg_in("plumbing_drawings", 5, 6),
        ],
    )
    segs = _segments(db)
    assert [s["category"] for s in segs] == [
        "general_drawings", "electrical_drawings", "plumbing_drawings",
    ]
    assert [s["sort_order"] for s in segs] == [0, 1, 2]
    # The untouched general run keeps the model's score; the edited rows are
    # marked and carry the score of the old row they mostly came from (both
    # cut out of the 0.8 electrical run).
    assert segs[0]["confidence"] == 0.9 and segs[0]["user_edited"] is False
    assert segs[1]["confidence"] is None and segs[2]["confidence"] is None
    assert segs[1]["user_edited"] is True and segs[2]["user_edited"] is True
    assert segs[1]["prior_confidence"] == 0.8
    assert segs[2]["prior_confidence"] == 0.8
    # The untouched general run keeps its LLM name/description; the new rows
    # fall back to deterministic names.
    assert segs[0]["name"] == "Cover" and segs[0]["description"] == "Covers."
    assert segs[1]["name"] == "Electrical Drawings"
    assert segs[1]["description"] == "Reassigned by the user."
    assert segs[2]["name"] == "Plumbing Drawings"
    # Trade segments got the cover pages prepended (extract_pages saw
    # prefix + range); the stored range stays the segment's own.
    assert extracted == [[1, 2, 3, 4], [1, 2, 5, 6]]
    assert segs[1]["filename"] == "Electrical Drawings (pages 3-4 + cover sheets).pdf"
    assert segs[2]["filename"] == "Plumbing Drawings (pages 5-6 + cover sheets).pdf"
    assert (segs[1]["page_start"], segs[1]["page_end"]) == (3, 4)
    # The untouched general run is not re-cut: it keeps its stored object
    # (covers unchanged), so only the two new trade segments upload.
    assert segs[0]["filename"] == "Cover (pages 1-2).pdf"
    assert segs[0]["storage_path"] == "bid-splits/j1/output/g.pdf"
    assert len(calls.uploaded) == 2
    # Only the replaced cut object is swept; the reused one and the source stay.
    assert set(calls.deleted) == {"bid-splits/j1/output/e.pdf"}
    # All trades, no doc category: still a drawing set, confidence nulled.
    frow = db.tables["bid_split_files"][0]
    assert frow["file_kind"] == "drawing_set" and frow["file_kind_confidence"] is None
    assert frow["user_corrected"] is True
    assert len(out["segments"]) == 3
    assert calls.audits and calls.audits[0][1] == "bid_split.correct_segments"


def test_put_segments_reuses_everything_when_nothing_changed(monkeypatch):
    """Submitting the standing list back verbatim moves no bytes at all:
    no source download, no uploads, no object deletes - the rows are
    rewritten pointing at the same stored objects."""
    db = FakeDB(_tables())
    calls = _install(monkeypatch, db)

    def _refuse_download(path):
        raise AssertionError("source download should be skipped")

    monkeypatch.setattr(storage, "download_file", _refuse_download)
    _put(db, [_seg_in("general_drawings", 1, 2), _seg_in("electrical_drawings", 3, 6)])
    segs = _segments(db)
    assert calls.uploaded == {} and calls.deleted == []
    assert [s["storage_path"] for s in segs] == [
        "bid-splits/j1/output/g.pdf", "bid-splits/j1/output/e.pdf",
    ]
    # LLM names/descriptions survive; the edit still counts as a correction.
    assert [s["name"] for s in segs] == ["Cover", "E-Sheets"]
    assert db.tables["bid_split_files"][0]["user_corrected"] is True


def test_put_segments_recuts_matched_trades_when_covers_changed(monkeypatch):
    """An identity-matched trade segment is NOT reused when the file's
    General / Cover pages changed: its stored object has the old covers
    baked in, so it re-cuts with the new prefix."""
    db = FakeDB(_tables())
    calls = _install(monkeypatch, db)
    extracted = []
    real_extract = bid_split.pdf_split.extract_pages
    monkeypatch.setattr(
        bid_split.pdf_split,
        "extract_pages",
        lambda pdf, pages: (extracted.append(list(pages)), real_extract(pdf, pages))[1],
    )
    # Covers shrink from pages 1-2 to page 1; electrical keeps its 3-6 range.
    _put(
        db,
        [
            _seg_in("general_drawings", 1, 1),
            _seg_in("structural_drawings", 2, 2),
            _seg_in("electrical_drawings", 3, 6),
        ],
    )
    segs = _segments(db)
    # Electrical kept its LLM name (identity match) but was re-cut with the
    # new single-page cover prefix; its old object is swept.
    assert segs[2]["name"] == "E-Sheets"
    assert segs[2]["filename"] == "E-Sheets (pages 3-6 + cover sheets).pdf"
    assert segs[2]["storage_path"] != "bid-splits/j1/output/e.pdf"
    assert [1, 3, 4, 5, 6] in extracted
    assert "bid-splits/j1/output/e.pdf" in calls.deleted
    assert len(calls.uploaded) == 3


def test_put_segments_whole_file_row_becomes_the_original(monkeypatch):
    db = FakeDB(_tables())
    calls = _install(monkeypatch, db)
    _put(db, [_seg_in("specifications", 1, 6)])
    segs = _segments(db)
    assert len(segs) == 1
    seg = segs[0]
    assert seg["is_original"] is True
    assert seg["storage_path"] == FILE_ROW["storage_path"]
    assert seg["filename"] == "SET.pdf" and seg["size_bytes"] == 1000
    assert calls.uploaded == {}  # nothing cut, nothing copied
    assert FILE_ROW["storage_path"] not in calls.deleted
    # Kind re-derived from the single spec segment.
    assert db.tables["bid_split_files"][0]["file_kind"] == "specifications"


def test_put_segments_doc_category_makes_it_mixed(monkeypatch):
    db = FakeDB(_tables())
    _install(monkeypatch, db)
    _put(db, [_seg_in("rfp", 1, 2), _seg_in("electrical_drawings", 3, 6)])
    assert db.tables["bid_split_files"][0]["file_kind"] == "mixed"


# ── cut_segment ──────────────────────────────────────────────────────────


def test_cut_segment_whole_file_passthrough(monkeypatch):
    monkeypatch.setattr(
        storage, "upload_file", lambda *a, **k: pytest.fail("must not upload")
    )
    seg = {"category": "specifications", "name": "Specs", "page_start": 1, "page_end": 6}
    path, name, size, is_original = bid_split.cut_segment(
        _blank_pdf(6), dict(FILE_ROW), seg, [], 6
    )
    assert (path, name, size, is_original) == (
        FILE_ROW["storage_path"], "SET.pdf", 1000, True,
    )


def test_cut_segment_prefix_and_plain_filenames(monkeypatch):
    uploaded = {}
    monkeypatch.setattr(
        storage, "upload_file", lambda path, content, ct, **k: uploaded.__setitem__(path, content)
    )
    monkeypatch.setattr(
        storage,
        "build_bid_split_output_path",
        lambda job_id, name: f"bid-splits/{job_id}/output/{name}",
    )
    pdf = _blank_pdf(6)
    seg = {"category": "electrical_drawings", "name": "E", "page_start": 3, "page_end": 4}
    _path, name, size, is_original = bid_split.cut_segment(pdf, dict(FILE_ROW), seg, [1, 2], 6)
    assert name == "E (pages 3-4 + cover sheets).pdf"
    assert is_original is False and size > 0
    # No general run in the file: plain range cut, no suffix.
    _path, name, _size, _orig = bid_split.cut_segment(pdf, dict(FILE_ROW), seg, [], 6)
    assert name == "E (pages 3-4).pdf"
    # Non-trade categories never take the prefix even when covers exist.
    seg = {"category": "specifications", "name": "S", "page_start": 3, "page_end": 4}
    _path, name, _size, _orig = bid_split.cut_segment(pdf, dict(FILE_ROW), seg, [1, 2], 6)
    assert name == "S (pages 3-4).pdf"


# ── capture_correction ───────────────────────────────────────────────────


def _before_after(**after_over):
    before = dict(FILE_ROW)
    segments_before = [dict(SEG_GENERAL), dict(SEG_ELECTRICAL)]
    after = {
        **FILE_ROW,
        "file_kind": "specifications",
        "file_kind_label": None,
        "file_kind_confidence": None,
        "user_corrected": True,
        **after_over,
    }
    segments_after = [
        {
            "category": "specifications", "other_type": None, "name": "Specs",
            "page_start": 1, "page_end": 6, "is_original": True, "confidence": None,
        }
    ]
    return before, segments_before, after, segments_after


def test_capture_first_correction_freezes_the_model_side(monkeypatch):
    db = FakeDB(_tables())
    copied = []
    monkeypatch.setattr(storage, "copy_file", lambda src, dst: copied.append((src, dst)))
    before, segs_before, after, segs_after = _before_after()
    bid_split_training.capture_correction(db, before, segs_before, after, segs_after, "u1")
    examples = db.tables["bid_split_training_examples"]
    assert len(examples) == 1
    ex = examples[0]
    assert ex["file_id"] == "f1" and ex["job_id"] == "j1"
    assert ex["source_filename"] == "SET.pdf" and ex["page_count"] == 6
    assert ex["model"] == "claude-opus-5"  # denormalized from the job row
    # Model side = the pre-mutation state, raw run output riding along.
    mo = ex["model_output"]
    assert mo["file_kind"] == "drawing_set" and mo["file_kind_confidence"] == 0.9
    assert [s["category"] for s in mo["segments"]] == [
        "general_drawings", "electrical_drawings",
    ]
    assert mo["segments"][0]["confidence"] == 0.9  # model side keeps confidence
    assert mo["raw"] == RAW_MODEL_OUTPUT
    # Input snapshot: source references + the run's frozen prompts/settings.
    snap = ex["input_snapshot"]
    assert snap["source_path"] == FILE_ROW["storage_path"]
    assert snap["filename"] == "SET.pdf" and snap["page_count"] == 6
    assert snap["triage"] == INPUT_SNAPSHOT["triage"]
    # Source PDF copied server-side so the example outlives the job.
    assert ex["training_source_path"] == "bid-splits/training/f1/SET.pdf"
    assert copied == [(FILE_ROW["storage_path"], "bid-splits/training/f1/SET.pdf")]
    # User side and diff.
    assert ex["user_output"]["file_kind"] == "specifications"
    assert "confidence" not in ex["user_output"]["segments"][0]
    diff = ex["diff_json"]
    assert diff["kind"] == {"model": "drawing_set", "user": "specifications", "changed": True}
    assert diff["pages"]["changed_runs"] == [
        {"page_start": 1, "page_end": 2,
         "model_category": "general_drawings", "user_category": "specifications"},
        {"page_start": 3, "page_end": 6,
         "model_category": "electrical_drawings", "user_category": "specifications"},
    ]
    assert diff["counts"] == {
        "kind_changed": 1, "pages_changed": 6, "runs_changed": 2,
        "segments_model": 2, "segments_user": 1,
    }
    assert diff["flags"] == []
    assert ex["modified"] is True
    assert ex["corrected_by"] == "u1" and ex["corrected_at"]
    assert ex["reviewed_by"] is None and ex["reviewed_at"] is None


def test_capture_flags_runs_without_raw_model_output(monkeypatch):
    db = FakeDB(_tables())
    monkeypatch.setattr(storage, "copy_file", lambda *a: None)
    before, segs_before, after, segs_after = _before_after()
    before["model_output"] = None  # a run from before 0113
    bid_split_training.capture_correction(db, before, segs_before, after, segs_after, "u1")
    ex = db.tables["bid_split_training_examples"][0]
    assert ex["model_output"]["raw"] is None
    assert ex["diff_json"]["flags"] == ["no_raw_model_output"]


def test_capture_copy_failure_never_fails_the_capture(monkeypatch):
    db = FakeDB(_tables())

    def _boom(*_a):
        raise RuntimeError("storage down")

    monkeypatch.setattr(storage, "copy_file", _boom)
    before, segs_before, after, segs_after = _before_after()
    bid_split_training.capture_correction(db, before, segs_before, after, segs_after, "u1")
    ex = db.tables["bid_split_training_examples"][0]
    assert ex["training_source_path"] is None  # captured anyway


def test_capture_second_correction_keeps_the_model_side_and_resets_review(monkeypatch):
    db = FakeDB(_tables())
    copied = []
    monkeypatch.setattr(storage, "copy_file", lambda src, dst: copied.append((src, dst)))
    before, segs_before, after, segs_after = _before_after()
    bid_split_training.capture_correction(db, before, segs_before, after, segs_after, "u1")
    frozen_model_output = dict(db.tables["bid_split_training_examples"][0]["model_output"])
    # A dev signs it off...
    db.tables["bid_split_training_examples"][0].update(
        {"reviewed_by": "dev1", "reviewed_at": "2026-08-26T01:00:00Z", "review_note": "ok"}
    )
    # ...then a second correction diffs against the MODEL, not the prior edit:
    # the before-state is now the first correction's output.
    before2 = {**after, "input_snapshot": INPUT_SNAPSHOT, "model_output": RAW_MODEL_OUTPUT}
    after2 = {**after, "file_kind": "rfp"}
    segs_after2 = [
        {"category": "rfp", "other_type": None, "name": "RFP",
         "page_start": 1, "page_end": 6, "is_original": True, "confidence": None},
    ]
    bid_split_training.capture_correction(db, before2, segs_after, after2, segs_after2, "u2")
    examples = db.tables["bid_split_training_examples"]
    assert len(examples) == 1  # upsert on file_id
    ex = examples[0]
    assert ex["model_output"] == frozen_model_output  # frozen on first capture
    assert ex["user_output"]["file_kind"] == "rfp"
    assert ex["diff_json"]["kind"]["model"] == "drawing_set"  # not "specifications"
    assert ex["corrected_by"] == "u2"
    assert ex["reviewed_by"] is None and ex["reviewed_at"] is None and ex["review_note"] is None
    assert len(copied) == 1  # the source copy is not re-made


def test_capture_page_run_diff_on_a_synthetic_case(monkeypatch):
    """Only the pages that changed category fold into runs; an untouched
    tail stays out of the diff."""
    db = FakeDB(_tables())
    monkeypatch.setattr(storage, "copy_file", lambda *a: None)
    before, segs_before, after, _ = _before_after()
    after["file_kind"] = "drawing_set"
    segs_after = [
        {"category": "general_drawings", "other_type": None, "name": "Cover",
         "page_start": 1, "page_end": 1, "is_original": False},
        {"category": "electrical_drawings", "other_type": None, "name": "E",
         "page_start": 2, "page_end": 6, "is_original": False},
    ]
    bid_split_training.capture_correction(db, before, segs_before, after, segs_after, "u1")
    diff = db.tables["bid_split_training_examples"][0]["diff_json"]
    # Model: 1-2 general, 3-6 electrical. User: 1 general, 2-6 electrical.
    # Only page 2 changed.
    assert diff["pages"]["changed_runs"] == [
        {"page_start": 2, "page_end": 2,
         "model_category": "general_drawings", "user_category": "electrical_drawings"},
    ]
    assert diff["counts"]["pages_changed"] == 1
    assert diff["kind"]["changed"] is False
    assert db.tables["bid_split_training_examples"][0]["modified"] is True


def test_capture_failure_never_fails_the_endpoint(monkeypatch):
    db = FakeDB(_tables())
    calls = _install(monkeypatch, db)

    def _boom(*a, **k):
        raise RuntimeError("capture bug")

    monkeypatch.setattr(bs.bid_split_training, "capture_correction", _boom)
    out = bs.correct_file_kind(
        "f1", bs.BidSplitFileKindIn(file_kind="specifications"), user=_WRITER
    )
    assert out["file_kind"] == "specifications"  # the correction stands
    assert db.tables["bid_split_training_examples"] == []
    assert calls.audits  # audited despite the capture failure


def test_endpoints_capture_through_the_real_service(monkeypatch):
    """End to end over the fakes: a PATCH lands one example row."""
    db = FakeDB(_tables())
    _install(monkeypatch, db)
    bs.correct_file_kind("f1", bs.BidSplitFileKindIn(file_kind="mixed"), user=_WRITER)
    examples = db.tables["bid_split_training_examples"]
    assert len(examples) == 1
    assert examples[0]["model_output"]["file_kind"] == "drawing_set"
    assert examples[0]["user_output"]["file_kind"] == "mixed"
    assert examples[0]["diff_json"]["counts"]["pages_changed"] == 0


# ── /training/bid-split routes ───────────────────────────────────────────


def _example(eid="ex1", corrected_at="2026-08-26T01:00:00Z", **over):
    row = {
        "id": eid,
        "file_id": "f1",
        "job_id": "j1",
        "source_filename": "SET.pdf",
        "page_count": 6,
        "model": "claude-opus-5",
        "input_snapshot": {"triage": {"system": "S"}},
        "model_output": {"file_kind": "drawing_set", "segments": []},
        "user_output": {"file_kind": "rfp", "segments": []},
        "diff_json": {
            "kind": {"model": "drawing_set", "user": "rfp", "changed": True},
            "pages": {"changed_runs": [{"page_start": 1, "page_end": 6}]},
            "counts": {"kind_changed": 1, "pages_changed": 6},
            "flags": ["no_raw_model_output"],
        },
        "modified": True,
        "training_source_path": "bid-splits/training/f1/SET.pdf",
        "corrected_by": "u1",
        "corrected_at": corrected_at,
        "reviewed_by": None,
        "reviewed_at": None,
        "review_note": None,
    }
    row.update(over)
    return row


_DEV = SimpleNamespace(id="dev1")


def test_bid_split_training_routes_are_dev_gated():
    for route in training_router.router.routes:
        if not route.path.startswith("/training/bid-split"):
            continue
        assert any(
            d.call is require_dev for d in route.dependant.dependencies
        ), f"{route.path} missing require_dev"


def test_bid_split_training_list_is_light_and_newest_first(monkeypatch):
    db = FakeDB(
        _tables(
            bid_split_training_examples=[
                _example("ex1", corrected_at="2026-08-25T00:00:00Z"),
                _example("ex2", corrected_at="2026-08-26T00:00:00Z", reviewed_by="dev1"),
            ]
        )
    )
    monkeypatch.setattr(training_router, "get_supabase", lambda: db)
    out = training_router.list_bid_split_examples(limit=50, offset=0, user=_DEV)
    assert out["total"] == 2
    assert [r["id"] for r in out["rows"]] == ["ex2", "ex1"]
    row = out["rows"][1]
    # Heavy jsonbs stay off the list payload; diff collapses to counts + flags
    # plus the tiny kind verdict for the list's "model -> user" cell.
    for heavy in ("input_snapshot", "model_output", "user_output"):
        assert heavy not in row
    assert row["counts"] == {"kind_changed": 1, "pages_changed": 6}
    assert row["flags"] == ["no_raw_model_output"]
    assert row["diff_json"] == {
        "kind": {"model": "drawing_set", "user": "rfp", "changed": True}
    }
    assert row["source_filename"] == "SET.pdf" and row["model"] == "claude-opus-5"
    assert row["corrected_by_profile"] == {"full_name": "Tom Writer"}
    assert out["rows"][0]["reviewed_by_profile"] == {"full_name": "Dev One"}


def test_bid_split_training_detail_serves_the_full_row(monkeypatch):
    db = FakeDB(_tables(bid_split_training_examples=[_example("ex1")]))
    monkeypatch.setattr(training_router, "get_supabase", lambda: db)
    out = training_router.bid_split_example_detail("ex1", user=_DEV)
    assert out["input_snapshot"] == {"triage": {"system": "S"}}
    assert out["model_output"]["file_kind"] == "drawing_set"
    assert out["user_output"]["file_kind"] == "rfp"
    assert out["diff_json"]["pages"]["changed_runs"]
    with pytest.raises(HTTPException) as exc:
        training_router.bid_split_example_detail("nope", user=_DEV)
    assert exc.value.status_code == 404


def test_bid_split_training_review_toggles(monkeypatch):
    db = FakeDB(_tables(bid_split_training_examples=[_example("ex1")]))
    monkeypatch.setattr(training_router, "get_supabase", lambda: db)
    training_router.review_bid_split_example(
        "ex1", TrainingReviewIn(reviewed=True, note="looks right"), user=_DEV
    )
    row = db.tables["bid_split_training_examples"][0]
    assert row["reviewed_by"] == "dev1" and row["reviewed_at"]
    assert row["review_note"] == "looks right"
    training_router.review_bid_split_example("ex1", TrainingReviewIn(reviewed=False), user=_DEV)
    row = db.tables["bid_split_training_examples"][0]
    assert row["reviewed_by"] is None and row["reviewed_at"] is None and row["review_note"] is None
    with pytest.raises(HTTPException) as exc:
        training_router.review_bid_split_example("nope", TrainingReviewIn(reviewed=True), user=_DEV)
    assert exc.value.status_code == 404


def test_bid_split_list_precedes_the_dynamic_detail_route():
    paths = [r.path for r in training_router.router.routes]
    assert paths.index("/training/bid-split") < paths.index("/training/bid-split/{example_id}")


# ── Snapshot persistence helpers (pipeline side) ─────────────────────────


def test_persist_training_io_writes_both_columns(monkeypatch):
    db = FakeDB(_tables())
    snapshot = {"triage": {"system": "S", "sample_pages": [1, 2]}}
    bid_split._persist_training_io(db, "f1", snapshot, {})
    row = db.tables["bid_split_files"][0]
    # Before any model output exists the column is nulled (a re-run clears
    # the previous attempt's output while keeping the fresh snapshot).
    assert row["input_snapshot"] == snapshot and row["model_output"] is None
    bid_split._persist_training_io(db, "f1", snapshot, {"triage": {"kind": "rfp"}})
    row = db.tables["bid_split_files"][0]
    assert row["model_output"] == {"triage": {"kind": "rfp"}}


def test_snapshot_pages_stores_a_deep_copy():
    pages = [{"page": 1, "category": "specifications", "other_type": None}]
    output = {"triage": {"kind": "mixed"}}
    bid_split._snapshot_pages(output, pages)
    # repair_islands mutates pages_meta in place after the snapshot; the
    # stored copy must keep what the model actually said.
    pages[0]["category"] = "electrical_drawings"
    assert output["pages"][0]["category"] == "specifications"
    assert output["triage"] == {"kind": "mixed"}


def test_prompt_builders_pin_the_exact_wording():
    prompt = bid_split._triage_prompt([1, 2, 9], 9, "SET.pdf")
    assert prompt == (
        'The pages above are a sample (3 of 9 pages, at the labeled positions) '
        'from the file "SET.pdf". Identify what the file as a whole is.'
    )
    prompt = bid_split._classify_prompt("CTX.", [3, 4], 9, "SET.pdf")
    assert prompt == (
        "CTX. Classify each of the 2 pages above (pages 3-4 of the 9-page "
        'file "SET.pdf"). Return one entry per page.'
    )

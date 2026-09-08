"""Bid File Splitter — flag gating, routing, triage/merge logic, PDF mechanics.

What these tests pin:
- the env flag contract: default OFF, routes 404 while off, every route rides
  the router-level dependency (no route can be added without the gate);
- the env-selectable LLM routing (provider + model per vendor, self-hosted
  master switch still wins);
- the pure triage/split logic the correctness hangs on: _sample_pages,
  _validate_triage, repair_islands (spec-book figures absorbed back into
  their section), merge_pages, _derive_kind, _validate_batch;
- _delete_segments never deletes the source object an is_original row
  points at;
- cover-sheet prepending: which pages count as General / Cover Sheets, and
  which segment categories get them prepended (trades only);
- pdf_split against real PDFs (count, range extraction, ordered page-list
  extraction, rejection of encrypted/broken files, page rendering to JPEG).

The LLM pipeline itself (bid_split.execute) is exercised against live models
from the dev environment, not stubbed here — the tool exists to evaluate the
model, so a mocked verdict would test nothing.
"""

import asyncio
import io
import zipfile

import pytest
from fastapi import HTTPException, UploadFile

from app.core import features
from app.core.config import Settings
from app.core.deps import require_internal, require_writer
from app.core.features import require_bid_file_splitter
from app.routers import bid_splitter as bs
from app.services import bid_split, llm, pdf_split
from app.services.llm import LlmBadOutput


# ── Test PDFs ────────────────────────────────────────────────────────────


def _blank_pdf(pages: int) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _encrypted_pdf() -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(_blank_pdf(1))))
    writer.encrypt("owner-and-user-pw")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ── Flag gating ──────────────────────────────────────────────────────────


def test_flag_defaults_off(monkeypatch):
    # conftest pins the flag ON for the suite; drop the pin to see the default.
    monkeypatch.delenv("BID_FILE_SPLITTER_ENABLED", raising=False)
    assert Settings(_env_file=None).bid_file_splitter_enabled is False


def test_routes_404_while_flag_is_off(monkeypatch):
    monkeypatch.delenv("BID_FILE_SPLITTER_ENABLED", raising=False)
    monkeypatch.setattr(
        features, "get_settings", lambda: Settings(_env_file=None)
    )
    with pytest.raises(HTTPException) as exc:
        require_bid_file_splitter()
    assert exc.value.status_code == 404
    assert exc.value.detail == "Not Found"  # not enumerable while off


def test_flag_on_admits(monkeypatch):
    monkeypatch.setattr(
        features,
        "get_settings",
        lambda: Settings(_env_file=None, bid_file_splitter_enabled=True),
    )
    assert require_bid_file_splitter() is None


def test_flag_rides_in_the_features_map(monkeypatch):
    monkeypatch.setattr(
        features,
        "get_settings",
        lambda: Settings(_env_file=None, bid_file_splitter_enabled=True),
    )
    assert features.enabled_map()["bid_file_splitter"] is True


def test_every_route_carries_the_flag_gate_and_an_auth_dep():
    for route in bs.router.routes:
        deps = {d.call for d in route.dependant.dependencies}
        assert require_bid_file_splitter in deps, f"{route.path} missing the flag gate"
        assert route.dependant.dependencies, route.path
    # Mutating routes are writer-gated; reads admit all internal roles.
    writer_paths = {
        "/bid-splitter/jobs",
        "/bid-splitter/jobs/{job_id}/files",
        "/bid-splitter/files/{file_id}/retry",
        "/bid-splitter/files/{file_id}",
        "/bid-splitter/files/{file_id}/segments",
    }
    for route in bs.router.routes:
        calls = {d.call for d in route.dependant.dependencies}
        if route.methods & {"DELETE", "PATCH", "PUT"} or (
            "POST" in route.methods and route.path in writer_paths
        ):
            assert require_writer in calls, f"{route.path} should be writer-only"
        elif "GET" in route.methods:
            assert require_internal in calls, f"{route.path} should be internal-read"


# ── LLM routing: env-selectable vendor ───────────────────────────────────


def test_bid_split_routes_to_anthropic_by_default():
    s = Settings(_env_file=None, anthropic_api_key="k")
    route = llm.resolve("bid_split", s)
    assert route.provider == "anthropic"
    assert route.model == s.claude_bid_split_model


def test_bid_split_provider_is_env_selectable():
    s = Settings(
        _env_file=None,
        bid_split_llm_provider="openai",
        openai_api_key="k",
        openai_bid_split_model="gpt-test",
    )
    route = llm.resolve("bid_split", s)
    assert route.provider == "openai"
    assert route.model == "gpt-test"


def test_bid_split_can_opt_into_self_hosted_without_the_master_switch():
    """provider=self_hosted routes ONLY bid_split to the box; every other
    feature stays on its 3rd-party vendor."""
    s = Settings(
        _env_file=None,
        bid_split_llm_provider="self_hosted",
        self_hosted_llm_local_base_url="http://localhost:11434/v1",
        self_hosted_bid_split_model="qwen-vl-test",
        anthropic_api_key="k",
    )
    route = llm.resolve("bid_split", s)
    assert route.provider == "self_hosted"
    assert route.model == "qwen-vl-test"
    assert route.base_url == "http://localhost:11434/v1"
    assert llm.is_configured("bid_split", s)
    assert llm.resolve("boq", s).provider == "anthropic"


def test_self_hosted_master_switch_still_wins():
    """The master switch stays one-way: while true, even an explicit 3rd-party
    provider selection is forced onto the self-hosted pool."""
    s = Settings(
        _env_file=None,
        full_self_hosted_llms_enabled=True,
        bid_split_llm_provider="openai",
        self_hosted_llm_local_base_url="http://localhost:11434/v1",
        self_hosted_bid_split_model="",
    )
    route = llm.resolve("bid_split", s)
    assert route.provider == "self_hosted"
    # Empty model = feature off in self-hosted mode, same contract as the rest.
    assert not llm.is_configured("bid_split", s)


def test_provider_typo_refuses_to_boot():
    with pytest.raises(ValueError):
        Settings(_env_file=None, bid_split_llm_provider="gemini")


# ── merge_pages ──────────────────────────────────────────────────────────


def _page(page, category, confidence=0.9, other_type=None, title=None, reason=None):
    entry = {
        "page": page,
        "category": category,
        "other_type": other_type,
        "title": title,
        "confidence": confidence,
    }
    if reason is not None:
        entry["confidence_reason"] = reason
    return entry


def test_merge_folds_contiguous_runs_and_covers_every_page():
    pages = [
        _page(1, "rfp", 0.8),
        _page(2, "rfp", 0.6),
        _page(3, "electrical_drawings"),
        _page(4, "electrical_drawings"),
        _page(5, "rfp"),
    ]
    segs = bid_split.merge_pages(pages)
    assert [(s["category"], s["page_start"], s["page_end"]) for s in segs] == [
        ("rfp", 1, 2),
        ("electrical_drawings", 3, 4),
        ("rfp", 5, 5),
    ]
    # Full coverage, no gaps or overlaps — the property the split relies on.
    covered = [p for s in segs for p in range(s["page_start"], s["page_end"] + 1)]
    assert covered == [1, 2, 3, 4, 5]
    assert segs[0]["confidences"] == [0.8, 0.6]


def test_other_pages_merge_only_within_the_same_label():
    pages = [
        _page(1, "other", other_type="Structural Drawings"),
        _page(2, "other", other_type="structural drawings"),  # case-insensitive
        _page(3, "other", other_type="Geotechnical Report"),
    ]
    segs = bid_split.merge_pages(pages)
    assert [(s["page_start"], s["page_end"], s["other_type"]) for s in segs] == [
        (1, 2, "Structural Drawings"),
        (3, 3, "Geotechnical Report"),
    ]


# ── Triage: page sampling + verdict validation ───────────────────────────


def test_sample_pages_small_files_take_every_page():
    assert bid_split._sample_pages(5, 12) == [1, 2, 3, 4, 5]
    assert bid_split._sample_pages(12, 12) == list(range(1, 13))


def test_sample_pages_anchors_ends_and_spreads_interior():
    sample = bid_split._sample_pages(600, 12)
    assert sample == sorted(set(sample))  # unique, ordered
    assert len(sample) == 12
    assert {1, 2, 3, 600} <= set(sample)
    # The interior picks actually reach deep into the file.
    assert any(200 < p < 500 for p in sample)


def test_validate_triage_accepts_and_normalizes():
    verdict = bid_split._validate_triage(
        {
            "kind": "specifications",
            "kind_label": "ignored",  # label only survives on 'other'
            "title": "  Project Manual Vol. 1  ",
            "description": "Spec book.",
            "confidence": 7,
        }
    )
    assert verdict["kind"] == "specifications"
    assert verdict["kind_label"] is None
    assert verdict["title"] == "Project Manual Vol. 1"
    assert verdict["confidence"] == 1.0


def test_validate_triage_rejects_unknown_kinds():
    with pytest.raises(LlmBadOutput):
        bid_split._validate_triage({"kind": "spec_book", "confidence": 0.9})
    with pytest.raises(LlmBadOutput):
        bid_split._validate_triage("not a dict")


def test_validate_triage_keeps_label_on_other():
    verdict = bid_split._validate_triage(
        {"kind": "other", "kind_label": "Geotechnical Report", "title": None,
         "description": None, "confidence": 0.8}
    )
    assert verdict["kind_label"] == "Geotechnical Report"


# ── repair_islands ───────────────────────────────────────────────────────


def _pages(*runs):
    """Build pages_meta from (count, category[, other_type]) runs."""
    out = []
    page = 1
    for run in runs:
        count, category = run[0], run[1]
        other_type = run[2] if len(run) > 2 else None
        for _ in range(count):
            out.append(_page(page, category, other_type=other_type))
            page += 1
    return out


def _cats(pages):
    return [p["category"] for p in pages]


def test_island_inside_a_spec_section_is_absorbed():
    """The core fix: figures inside a spec book read as drawings page-by-page
    but must stay in the specifications."""
    pages = _pages((15, "specifications"), (3, "electrical_drawings"), (15, "specifications"))
    repaired = bid_split.repair_islands(pages, max_island=10)
    assert set(_cats(repaired)) == {"specifications"}


def test_island_between_different_neighbors_survives():
    pages = _pages((15, "specifications"), (3, "electrical_drawings"), (15, "rfp"))
    assert _cats(bid_split.repair_islands(pages, max_island=10)) == _cats(pages)


def test_island_longer_than_the_cap_survives():
    pages = _pages((15, "specifications"), (11, "electrical_drawings"), (15, "specifications"))
    assert "electrical_drawings" in _cats(bid_split.repair_islands(pages, max_island=10))


def test_island_longer_than_its_neighbors_survives():
    """The tail must not wag the dog: 2+2 pages of specs cannot swallow a
    6-page drawing run between them."""
    pages = _pages((2, "specifications"), (6, "electrical_drawings"), (2, "specifications"))
    assert "electrical_drawings" in _cats(bid_split.repair_islands(pages, max_island=10))


def test_edge_island_absorbs_into_a_document_category_only():
    # A spec book's cover pages reading as drawings get absorbed...
    pages = _pages((2, "architectural_drawings"), (30, "specifications"))
    assert set(_cats(bid_split.repair_islands(pages, max_island=10))) == {"specifications"}
    # ...but a short spec-looking tail on a drawing set is left alone (edges
    # never absorb INTO a trade), and doc categories are never absorbed out.
    pages = _pages((30, "electrical_drawings"), (2, "specifications"))
    assert _cats(bid_split.repair_islands(pages, max_island=10)) == _cats(pages)


def test_nested_islands_converge():
    """Absorbing the inner island exposes the outer one; the fixpoint loop
    must finish the job."""
    pages = _pages(
        (20, "specifications"),
        (2, "electrical_drawings"),
        (1, "mechanical_drawings"),
        (2, "electrical_drawings"),
        (20, "specifications"),
    )
    assert set(_cats(bid_split.repair_islands(pages, max_island=10))) == {"specifications"}


def test_other_islands_absorb_with_their_label():
    pages = _pages(
        (12, "other", "Geotechnical Report"),
        (2, "structural_drawings"),
        (12, "other", "Geotechnical Report"),
    )
    repaired = bid_split.repair_islands(pages, max_island=10)
    assert all(p["category"] == "other" for p in repaired)
    assert all(p["other_type"] == "Geotechnical Report" for p in repaired)


# ── _derive_kind ─────────────────────────────────────────────────────────


def _seg(category, other_type=None):
    return {"category": category, "other_type": other_type}


def test_derive_kind_single_segment_maps_to_its_category():
    assert bid_split._derive_kind([_seg("specifications")]) == ("specifications", None)
    assert bid_split._derive_kind([_seg("rfp")]) == ("rfp", None)
    assert bid_split._derive_kind([_seg("addenda")]) == ("addendum", None)
    assert bid_split._derive_kind([_seg("electrical_drawings")]) == ("drawing_set", None)
    assert bid_split._derive_kind([_seg("other", "Geotechnical Report")]) == (
        "other",
        "Geotechnical Report",
    )


def test_derive_kind_trades_only_is_a_drawing_set():
    segs = [_seg("general_drawings"), _seg("electrical_drawings"), _seg("plumbing_drawings")]
    assert bid_split._derive_kind(segs) == ("drawing_set", None)
    # An unusual labeled trade rides along without flipping the verdict.
    segs.append(_seg("other", "Marine Drawings"))
    assert bid_split._derive_kind(segs) == ("drawing_set", None)


def test_derive_kind_document_sections_make_it_mixed():
    segs = [_seg("rfp"), _seg("specifications"), _seg("electrical_drawings")]
    assert bid_split._derive_kind(segs) == ("mixed", None)


# ── _delete_segments: the source object is sacred ────────────────────────


class _SegmentSweepSb:
    """Fake supabase: bid_split_segments select returns the given rows."""

    def __init__(self, rows):
        self.rows = rows

    def table(self, _name):
        return self

    def select(self, *_a, **_k):
        return self

    def delete(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def execute(self):
        import types

        return types.SimpleNamespace(data=self.rows)


def test_delete_segments_never_deletes_the_source_object(monkeypatch):
    rows = [
        {"id": "s1", "storage_path": "bid-splits/j/output/cut.pdf", "is_original": False},
        {"id": "s2", "storage_path": "bid-splits/j/source/original.pdf", "is_original": True},
    ]
    deleted = []
    monkeypatch.setattr(bid_split, "get_supabase", lambda: _SegmentSweepSb(rows))
    monkeypatch.setattr(bid_split.storage, "delete_file", deleted.append)
    bid_split._delete_segments("f1")
    assert deleted == ["bid-splits/j/output/cut.pdf"]


# ── _validate_batch ──────────────────────────────────────────────────────


def test_validate_batch_requires_every_requested_page():
    good = {"pages": [_page(1, "rfp"), _page(2, "rfp")]}
    assert len(bid_split._validate_batch(good, [1, 2])) == 2
    with pytest.raises(LlmBadOutput):
        bid_split._validate_batch({"pages": [_page(1, "rfp")]}, [1, 2])


def test_validate_batch_rejects_unknown_categories_and_clamps():
    with pytest.raises(LlmBadOutput):
        bid_split._validate_batch({"pages": [_page(1, "plumbing")]}, [1])
    out = bid_split._validate_batch({"pages": [_page(1, "rfp", confidence=7)]}, [1])
    assert out[0]["confidence"] == 1.0
    # other_type only survives on 'other'.
    out = bid_split._validate_batch(
        {"pages": [_page(1, "rfp", other_type="Nope")]}, [1]
    )
    assert out[0]["other_type"] is None


# ── Confidence reasoning (what the hover panel shows) ────────────────────


def test_page_reason_is_trimmed_collapsed_and_capped():
    out = bid_split._validate_batch(
        {"pages": [_page(1, "rfp", reason="  bid form\n  with a signature block  ")]},
        [1],
    )
    assert out[0]["confidence_reason"] == "bid form with a signature block"
    # Overrunning the cap never cuts a word in half: the last whole sentence
    # inside the limit wins, else the last whole word plus an ellipsis.
    sentences = ("A legible E-series title block. " * 20).strip()
    capped = bid_split._validate_batch(
        {"pages": [_page(1, "rfp", reason=sentences)]}, [1]
    )[0]["confidence_reason"]
    assert len(capped) <= bid_split._PAGE_REASON_MAX_CHARS
    assert capped.endswith("title block.")
    words = bid_split._validate_batch(
        {"pages": [_page(1, "rfp", reason="unbroken " * 200)]}, [1]
    )[0]["confidence_reason"]
    assert len(words) <= bid_split._PAGE_REASON_MAX_CHARS
    assert words.endswith("unbroken...")


def test_missing_page_reason_stays_none_not_invented():
    out = bid_split._validate_batch({"pages": [_page(1, "rfp")]}, [1])
    assert out[0]["confidence_reason"] is None
    blank = bid_split._validate_batch({"pages": [_page(1, "rfp", reason="   ")]}, [1])
    assert blank[0]["confidence_reason"] is None


def test_triage_reason_leads_with_the_sample_size():
    verdict = bid_split._validate_triage(
        {
            "kind": "specifications",
            "kind_label": None,
            "title": None,
            "description": None,
            "confidence": 0.8,
            "confidence_reason": "CSI section numbers on every sampled page",
        }
    )
    reason = bid_split.triage_confidence_reason(verdict, 12, 340)
    assert "12-page sample of the 340-page file" in reason
    assert "CSI section numbers on every sampled page." in reason
    # A file small enough to be read whole says so instead.
    assert "all 6 pages" in bid_split.triage_confidence_reason(verdict, 6, 6)


def test_segment_reason_reports_the_spread_and_the_weakest_page():
    seg = bid_split.merge_pages(
        [
            _page(1, "electrical_drawings", 0.95, reason="E1.0 in a clean title block"),
            _page(2, "electrical_drawings", 0.55, reason="no title block; read from the panel schedule"),
            _page(3, "electrical_drawings", 0.9, reason="E1.2 in a clean title block"),
        ]
    )[0]
    reason = bid_split.build_segment_reason(seg)
    assert "Average of 3 pages, 55% to 95%." in reason
    assert "Least sure on page 2 at 55%: no title block" in reason
    # The spread is wide, so the strong end is quoted too.
    assert "Most sure on page 1 at 95%" in reason


def test_segment_reason_on_a_flat_run_quotes_one_end_only():
    seg = bid_split.merge_pages(
        [
            _page(1, "rfp", 0.9, reason="instructions to bidders"),
            _page(2, "rfp", 0.9, reason="instructions to bidders"),
        ]
    )[0]
    reason = bid_split.build_segment_reason(seg)
    assert "Every one of the 2 pages scored 90%." in reason
    # Nothing is least or most sure when the pages agree.
    assert "Least sure" not in reason and "Most sure" not in reason
    assert "Page 1: instructions to bidders." in reason


def test_single_page_segment_reason_is_the_page_reason():
    seg = bid_split.merge_pages(
        [_page(7, "other", 0.4, other_type="Geotechnical Report", reason="boring logs")]
    )[0]
    reason = bid_split.build_segment_reason(seg)
    assert reason == "Page 7 scored 40%. boring logs."


def test_segment_reason_names_the_pages_island_repair_folded_in():
    """The one thing worth checking by hand: pages the model read as something
    else, moved here on document flow."""
    pages = bid_split.repair_islands(
        _pages((12, "specifications"), (2, "electrical_drawings"), (12, "specifications")),
        max_island=10,
    )
    seg = bid_split.merge_pages(pages)[0]
    reason = bid_split.build_segment_reason(seg)
    assert "Pages 13-14 read as Electrical Drawings alone" in reason
    assert "were folded in from the pages around them." in reason


def test_one_folded_page_reads_in_the_singular():
    pages = bid_split.repair_islands(
        _pages((12, "specifications"), (1, "electrical_drawings"), (12, "specifications")),
        max_island=10,
    )
    reason = bid_split.build_segment_reason(bid_split.merge_pages(pages)[0])
    assert "Page 13 read as Electrical Drawings alone and was folded in from the pages around it." in reason


def test_reason_is_none_when_there_are_no_pages():
    assert bid_split.build_segment_reason({"pages": []}) is None
    assert bid_split.pages_confidence_reason([]) is None


# ── Cover-sheet prepending ───────────────────────────────────────────────


def _range_seg(category, page_start, page_end, other_type=None):
    return {
        "category": category,
        "other_type": other_type,
        "page_start": page_start,
        "page_end": page_end,
    }


def test_general_cover_pages_collects_every_general_run_in_order():
    segs = [
        _range_seg("rfp", 1, 4),
        _range_seg("general_drawings", 5, 7),
        _range_seg("electrical_drawings", 8, 20),
        _range_seg("general_drawings", 21, 21),
    ]
    assert bid_split.general_cover_pages(segs) == [5, 6, 7, 21]
    assert bid_split.general_cover_pages([_range_seg("electrical_drawings", 1, 9)]) == []


def test_cover_prefix_targets_are_the_trades_only():
    """Covers ride ahead of trade sheets; they are not stapled onto the
    general segment itself, document categories, or 'other' grab-bag docs."""
    assert bid_split.COVER_PREFIX_CATEGORIES == {
        "civil_drawings",
        "structural_drawings",
        "architectural_drawings",
        "mechanical_drawings",
        "plumbing_drawings",
        "electrical_drawings",
        "fire_protection_drawings",
        "low_voltage_drawings",
    }
    assert "general_drawings" not in bid_split.COVER_PREFIX_CATEGORIES
    assert "specifications" not in bid_split.COVER_PREFIX_CATEGORIES
    assert "other" not in bid_split.COVER_PREFIX_CATEGORIES


def test_pages_label_folds_runs():
    assert bid_split._pages_label([1, 2, 3]) == "1-3"
    assert bid_split._pages_label([1, 2, 3, 17]) == "1-3, 17"
    assert bid_split._pages_label([4]) == "4"
    assert bid_split._pages_label([2, 3, 8, 9, 12]) == "2-3, 8-9, 12"


# ── pdf_split ────────────────────────────────────────────────────────────


def test_extract_pages_takes_an_ordered_page_list():
    pdf = _blank_pdf(6)
    part = pdf_split.extract_pages(pdf, [5, 6, 2, 3])
    assert pdf_split.page_count(part) == 4
    with pytest.raises(ValueError):
        pdf_split.extract_pages(pdf, [1, 7])
    with pytest.raises(ValueError):
        pdf_split.extract_pages(pdf, [0])
    with pytest.raises(ValueError):
        pdf_split.extract_pages(pdf, [])


def test_page_count_and_extract_range():
    pdf = _blank_pdf(5)
    assert pdf_split.page_count(pdf) == 5
    part = pdf_split.extract_range(pdf, 2, 4)
    assert pdf_split.page_count(part) == 3
    with pytest.raises(ValueError):
        pdf_split.extract_range(pdf, 4, 6)
    with pytest.raises(ValueError):
        pdf_split.extract_range(pdf, 0, 2)


def test_unreadable_pdfs_are_value_errors():
    with pytest.raises(ValueError):
        pdf_split.page_count(b"%PDF-not really a pdf")
    with pytest.raises(ValueError):
        pdf_split.page_count(_encrypted_pdf())


def test_render_pages_produces_jpegs():
    images = pdf_split.render_pages(
        _blank_pdf(2), [0, 1], long_side=200, jpeg_quality=60
    )
    assert len(images) == 2
    assert all(img[:2] == b"\xff\xd8" for img in images)  # JPEG magic


# ── Upload validation (no write happens for a rejected file) ────────────


class _Boom:
    def table(self, *_a, **_k):  # pragma: no cover - reaching here is the failure
        raise AssertionError("the DB must not be touched for a rejected request")


def _upload(name: str, content: bytes) -> UploadFile:
    return UploadFile(io.BytesIO(content), filename=name, size=len(content))


def test_upload_rejects_non_pdfs_before_any_write(monkeypatch):
    """Bad bytes 415 after only the job lookup — no storage, no rows."""
    from fastapi import BackgroundTasks

    monkeypatch.setattr(bs, "get_supabase", lambda: _Boom())
    monkeypatch.setattr(bs, "_job_or_404", lambda _sb, job_id: {"id": job_id})
    # Wrong extension, and right extension with wrong bytes: magic wins.
    for name, content in (("notes.txt", b"hello"), ("fake.pdf", b"MZ not a pdf")):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                bs.upload_job_file(
                    "j1", BackgroundTasks(), file=_upload(name, content), user=None
                )
            )
        assert exc.value.status_code == 415


def test_create_job_refuses_when_the_model_is_not_configured(monkeypatch):
    monkeypatch.setattr(bs.llm, "is_configured", lambda *_a, **_k: False)
    monkeypatch.setattr(bs, "get_supabase", lambda: _Boom())
    with pytest.raises(HTTPException) as exc:
        bs.create_job(bs.BidSplitJobIn(file_count=1), user=None)
    assert exc.value.status_code == 400


def test_create_job_rejects_oversized_batch_plans(monkeypatch):
    monkeypatch.setattr(bs.llm, "is_configured", lambda *_a, **_k: True)
    monkeypatch.setattr(bs, "get_supabase", lambda: _Boom())
    with pytest.raises(HTTPException) as exc:
        bs.create_job(bs.BidSplitJobIn(file_count=999), user=None)
    assert exc.value.status_code == 400


# ── Nested folder export: the tree the ZIP hands back ────────────────────


def _export_seg(category: str, filename: str, **extra) -> dict:
    seg = {
        "category": category,
        "other_type": None,
        "filename": filename,
        "storage_path": f"bid-splits/j/output/{filename}",
        "size_bytes": 10,
    }
    seg.update(extra)
    return seg


def _export_file(filename: str, segments: list[dict], status: str = "done", error=None) -> dict:
    return {
        "id": filename,
        "filename": filename,
        "status": status,
        "error": error,
        "segments": segments,
    }


def test_tree_rows_nest_source_file_then_category():
    rows, notes = bs._tree_rows(
        [
            _export_file(
                "BID SET.pdf",
                [
                    _export_seg("electrical_drawings", "E-Sheets (pages 12-40).pdf"),
                    _export_seg("specifications", "Div 26 (pages 41-90).pdf"),
                ],
            )
        ]
    )
    assert notes == []
    assert [r["folders"] for r in rows] == [
        ["BID SET", "Electrical Drawings"],
        ["BID SET", "Specifications"],
    ]
    # The extension belongs to the file, not to the folder standing in for it.
    assert all(not r["folders"][0].endswith(".pdf") for r in rows)


def test_tree_rows_add_a_label_folder_for_other_sections():
    rows, _notes = bs._tree_rows(
        [
            _export_file(
                "SET.pdf",
                [
                    _export_seg("other", "Soils.pdf", other_type="Geotechnical Report"),
                    _export_seg("other", "Misc.pdf"),  # no label: stops at Other/
                ],
            )
        ]
    )
    assert rows[0]["folders"] == ["SET", "Other", "Geotechnical Report"]
    assert rows[1]["folders"] == ["SET", "Other"]


def test_tree_rows_keep_same_named_sources_in_separate_folders():
    rows, _notes = bs._tree_rows(
        [
            _export_file("Plans.pdf", [_export_seg("civil_drawings", "C (pages 1-5).pdf")]),
            _export_file("plans.pdf", [_export_seg("civil_drawings", "C (pages 1-5).pdf")]),
        ]
    )
    # Case-insensitive: Windows/macOS would merge "Plans" and "plans" on extract.
    assert rows[0]["folders"][0] == "Plans"
    assert rows[1]["folders"][0] == "plans (2)"


def test_tree_rows_carry_the_intact_original_under_its_category():
    """A file left intact by triage contributes its own source object — that IS
    the document the user wants out of it."""
    rows, notes = bs._tree_rows(
        [
            _export_file(
                "SPEC BOOK.pdf",
                [
                    _export_seg(
                        "specifications",
                        "SPEC BOOK.pdf",
                        is_original=True,
                        storage_path="bid-splits/j/source/SPEC BOOK.pdf",
                    )
                ],
            )
        ]
    )
    assert notes == []
    assert rows[0]["folders"] == ["SPEC BOOK", "Specifications"]
    assert rows[0]["storage_path"] == "bid-splits/j/source/SPEC BOOK.pdf"


def test_tree_rows_note_files_with_nothing_to_export():
    rows, notes = bs._tree_rows(
        [
            _export_file("broken.pdf", [], status="failed", error="model refused"),
            _export_file("waiting.pdf", [], status="running"),
            _export_file("ok.pdf", [_export_seg("rfp", "RFP.pdf")]),
        ]
    )
    assert [r["folders"] for r in rows] == [["ok", "RFP"]]
    assert notes == [
        "broken.pdf: failed, no sections to export (model refused)",
        "waiting.pdf: running, no sections to export",
    ]


def test_export_404s_when_there_is_nothing_to_hand_back():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            bs._stream_tree_zip(
                [], [], filename="x.zip", user=None, target="bid_split_job", target_id="j"
            )
        )
    assert exc.value.status_code == 404


def test_export_refuses_a_batch_over_the_size_cap(monkeypatch):
    """Over the cap the build never starts — the caller is pointed at the
    per-file export instead."""
    rows = [
        {
            "folders": ["Set", "Specifications"],
            "filename": "big.pdf",
            "storage_path": "p",
            "size_bytes": Settings(_env_file=None).export_max_total_bytes + 1,
        }
    ]
    monkeypatch.setattr(
        bs.file_export,
        "build_tree_export_spooled",
        lambda *_a, **_k: pytest.fail("the archive must not be built over the cap"),
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            bs._stream_tree_zip(
                rows, [], filename="x.zip", user=None, target="bid_split_job", target_id="j"
            )
        )
    assert exc.value.status_code == 413


class _ExportSb:
    """Fake supabase for the export path: one table's rows per name."""

    def __init__(self, job: dict, files: list[dict], segments: list[dict]):
        self._rows = {
            "bid_split_jobs": [job],
            "bid_split_files": files,
            "bid_split_segments": segments,
        }
        self._table = ""

    def table(self, name):
        self._table = name
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def in_(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        import types

        return types.SimpleNamespace(data=self._rows[self._table])


def _zip_names(response) -> set[str]:
    async def collect():
        return b"".join([chunk async for chunk in response.body_iterator])

    with zipfile.ZipFile(io.BytesIO(asyncio.run(collect()))) as zf:
        return set(zf.namelist())


def test_export_job_streams_the_whole_nested_tree(monkeypatch):
    """End to end over fakes: two source files, one split by trade and one left
    intact, come back as one ZIP already foldered."""
    from types import SimpleNamespace

    files = [
        {"id": "f1", "job_id": "j1", "filename": "BID SET.pdf", "status": "done", "error": None},
        {"id": "f2", "job_id": "j1", "filename": "SPECS.pdf", "status": "done", "error": None},
        {"id": "f3", "job_id": "j1", "filename": "late.pdf", "status": "running", "error": None},
    ]
    segments = [
        {
            "file_id": "f1",
            "category": "electrical_drawings",
            "other_type": None,
            "filename": "E-Sheets (pages 1-4).pdf",
            "storage_path": "bid-splits/j1/output/e.pdf",
            "size_bytes": 4,
        },
        {
            "file_id": "f1",
            "category": "other",
            "other_type": "Geotechnical Report",
            "filename": "Soils (pages 5-6).pdf",
            "storage_path": "bid-splits/j1/output/g.pdf",
            "size_bytes": 4,
        },
        {
            "file_id": "f2",
            "category": "specifications",
            "other_type": None,
            "filename": "SPECS.pdf",
            "storage_path": "bid-splits/j1/source/SPECS.pdf",
            "size_bytes": 4,
            "is_original": True,
        },
    ]
    monkeypatch.setattr(
        bs, "get_supabase", lambda: _ExportSb({"id": "j1"}, files, segments)
    )
    monkeypatch.setattr(bs.file_export.storage, "download_file", lambda p: b"pdf!")
    monkeypatch.setattr(bs, "audit", lambda *_a, **_k: None)

    response = asyncio.run(bs.export_job("j1", user=SimpleNamespace(id="u1")))
    assert response.headers["X-Export-File-Count"] == "3"
    assert "attachment" in response.headers["content-disposition"]
    assert _zip_names(response) == {
        "BID SET/Electrical Drawings/E-Sheets (pages 1-4).pdf",
        "BID SET/Other/Geotechnical Report/Soils (pages 5-6).pdf",
        "SPECS/Specifications/SPECS.pdf",
        "MANIFEST.txt",
    }

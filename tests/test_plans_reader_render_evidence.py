"""plans_reader_render_evidence (WS8 wave 3): the annotated plans PDF and the
marked plans spec from evidence.

Synthetic evidence on generated PDFs (a /Rotate 90 page included): one
/Circle per dot mark, one /PolyLine per route polyline (dashed for assumed
routes), one /FreeText legend per sheet, a /VP measure viewport when the
scale is known; the file opens in pypdf and in PDFium with the same
annotation count; coordinates honor the page rotation. The storage path
(render_annotated_plans) is run on a fake client. Golden: the 7154 evidence
built from the backup PDFs renders under 6 MB with counts equal to the
evidence (marked `golden`, skipped without the backup). $0, no network.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject

from app.core.config import Settings
from app.services import plans_reader_render_evidence as RE
from tests import ai_pricing_fakes as fx
from tests.test_plans_reader_evidence import ELEC_ID, ELEC_PDF, MECH_ID, MECH_PDF, _fixture_inputs

golden = pytest.mark.golden
needs_backup = pytest.mark.skipif(not (ELEC_PDF.exists() and MECH_PDF.exists()), reason="7154 backup not on this machine")
SIX_MB = 6 * 1024 * 1024
RUN_ID = "11111111-1111-4111-8111-111111111111"


def _pdf(pages: list[tuple[float, float, int]]) -> bytes:
    """A PDF of blank pages (width in, height in, /Rotate)."""
    w = PdfWriter()
    for wi, hi, rot in pages:
        w.add_blank_page(width=wi * 72, height=hi * 72)
        if rot:
            w.pages[-1][NameObject("/Rotate")] = NumberObject(rot)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _evidence() -> dict:
    return {
        "schema": "bdr.evidence/1",
        "run_id": RUN_ID,
        "main_version": 1,
        "boq_version": 1,
        "inputs_hash": "abc",
        "lines": [
            {"line_id": "B1", "description": "L1, 2X4 TROFFER", "unit": "EA", "quantity": 3,
             "qty_chip": {"kind": "counted", "text": "Counted 3, marked 2, 1 not located"}},
            {"line_id": "B2", "description": "#12 THHN/THWN Cu", "unit": "FT", "quantity": 40,
             "qty_chip": {"kind": "assumed_route", "text": "Assumed route 40 FT, 1 circuits"}},
        ],
        "sheets": [
            {
                "sheet_key": "D1P1", "document_id": "d1", "page": 1, "sheet_number": "E2.01", "w_in": 42, "h_in": 30,
                "scale": {"ft_per_in": 4.0},
                "legend": [
                    {"line_id": "B1", "prefix": "L1", "description": "L1, 2X4 TROFFER", "quantity": 3, "unit": "EA",
                     "marked": 2, "not_located": 1},
                    {"line_id": "B2", "prefix": "", "description": "#12 THHN/THWN Cu", "quantity": 40, "unit": "FT",
                     "marked": 1, "not_located": 0},
                ],
                "marks": [
                    {"mark_id": "K1", "kind": "dot", "style": "solid", "method": "anchor", "legend_index": 0,
                     "line_id": "B1", "points": [[0.25, 0.5]], "count": 1},
                    {"mark_id": "K2", "kind": "dot", "style": "ring", "method": "approx", "legend_index": 0,
                     "line_id": "B1", "points": [[0.5, 0.5]], "count": 2},
                    {"mark_id": "P1", "kind": "polyline", "style": "dashed", "legend_index": 1, "line_id": "B2",
                     "points": [[0.1, 0.1], [0.2, 0.2], [0.3, 0.2]], "label": "40 FT", "basis": "assumed_route"},
                    {"mark_id": "K9", "kind": "dot", "style": "solid", "method": "anchor", "legend_index": 0,
                     "line_id": "B1", "points": []},
                ],
            },
            {
                "sheet_key": "D1P2", "document_id": "d1", "page": 2, "sheet_number": "E3.01", "w_in": 42, "h_in": 30,
                "scale": {"ft_per_in": None},
                "legend": [{"line_id": None, "prefix": "NC, X1", "description": "Tag X1 not counted", "quantity": 1,
                            "unit": "EA", "marked": 1, "not_located": 0}],
                "marks": [{"mark_id": "K3", "kind": "dot", "style": "unclaimed", "method": "unclaimed",
                           "legend_index": 0, "line_id": None, "tag": "X1", "points": [[0.9, 0.1]], "count": 1}],
            },
            {"sheet_key": "D1P3", "document_id": "d1", "page": 3, "sheet_number": "E4.01", "legend": [], "marks": []},
        ],
        "stats": {},
        "notes": [],
    }


@pytest.fixture
def two_page_pdf() -> bytes:
    # Page 2 is portrait with /Rotate 90, displayed landscape 42 x 30.
    return _pdf([(42, 30, 0), (30, 42, 90), (42, 30, 0)])


def test_expected_counts_skip_empty_points_and_sheets_without_marks():
    counts = RE.expected_counts(_evidence())
    assert counts == {"Circle": 3, "PolyLine": 1, "FreeText": 2, "pages": 2}


def test_annotated_pdf_counts_match_evidence_in_pypdf_and_pdfium(two_page_pdf):
    out = RE.render_annotated_plans_pdf({"d1": two_page_pdf}, _evidence())
    expected = RE.expected_counts(_evidence())
    got = RE.count_annotations(out)
    assert got == expected
    total = RE.count_annotations_pdfium(out)
    assert total == expected["Circle"] + expected["PolyLine"] + expected["FreeText"]
    reader = PdfReader(io.BytesIO(out))
    assert len(reader.pages) == 2
    # Every annotation carries its own appearance stream and a legend index.
    for page in reader.pages:
        for ref in page["/Annots"]:
            a = ref.get_object()
            assert "/AP" in a
            if a["/Subtype"] != "/FreeText":
                assert str(a["/Contents"]).startswith("[")
    # Dashed polyline, measure viewport only where the scale is known.
    polys = [r.get_object() for r in reader.pages[0]["/Annots"] if r.get_object()["/Subtype"] == "/PolyLine"]
    assert polys and polys[0]["/BS"]["/S"] == "/D" and list(polys[0]["/BS"]["/D"]) == list(RE.DASH_PATTERN)
    assert "/VP" in reader.pages[0] and "/VP" not in reader.pages[1]
    measure = reader.pages[0]["/VP"][0].get_object()["/Measure"]
    assert measure["/Subtype"] == "/RL" and float(measure["/X"][0]["/C"]) == pytest.approx(4.0 / 72)
    # The legend names the chip text and the unclaimed tag.
    legends = [r.get_object() for p in reader.pages for r in p["/Annots"] if r.get_object()["/Subtype"] == "/FreeText"]
    assert "Counted 3, marked 2, 1 not located" in str(legends[0]["/Contents"])
    assert "not counted" in str(legends[1]["/Contents"]).lower()


def test_rotated_page_marks_land_in_user_space_through_the_rotation(two_page_pdf):
    out = RE.render_annotated_plans_pdf({"d1": two_page_pdf}, _evidence())
    reader = PdfReader(io.BytesIO(out))
    page = reader.pages[1]
    assert int(page.rotation) == 90
    circle = next(r.get_object() for r in page["/Annots"] if r.get_object()["/Subtype"] == "/Circle")
    x0, y0, x1, y1 = (float(v) for v in circle["/Rect"])
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    # Displayed (0.9, 0.1) on a 42 x 30 landscape view of a 30 x 42 portrait
    # page rotated 90: user x = 0.1 * 30 in, user y = 0.9 * 42 in.
    assert cx == pytest.approx(0.1 * 30 * 72, abs=0.5)
    assert cy == pytest.approx(0.9 * 42 * 72, abs=0.5)
    # The unrotated page keeps the plain mapping (origin top-left to bottom-left).
    first = next(r.get_object() for r in reader.pages[0]["/Annots"] if r.get_object()["/Subtype"] == "/Circle")
    fx0, fy0, fx1, fy1 = (float(v) for v in first["/Rect"])
    assert (fx0 + fx1) / 2 == pytest.approx(0.25 * 42 * 72, abs=0.5)
    assert (fy0 + fy1) / 2 == pytest.approx((1 - 0.5) * 30 * 72, abs=0.5)


def test_pdfium_renders_the_annotated_page(two_page_pdf):
    pdfium = pytest.importorskip("pypdfium2")
    out = RE.render_annotated_plans_pdf({"d1": two_page_pdf}, _evidence())
    doc = pdfium.PdfDocument(out)
    img = doc[0].render(scale=0.25, may_draw_forms=True).to_pil().convert("RGB")
    # The red anchor circle at (0.25, 0.5) leaves non-white pixels there.
    px = img.getpixel((int(0.25 * img.width), int(0.5 * img.height)))
    assert px != (255, 255, 255)
    doc.close()


def test_missing_documents_are_skipped_and_nothing_left_raises(two_page_pdf):
    ev = _evidence()
    ev["sheets"][1]["document_id"] = "other"
    out = RE.render_annotated_plans_pdf({"d1": two_page_pdf}, ev)
    assert RE.count_annotations(out)["pages"] == 1
    with pytest.raises(ValueError, match="could be found"):
        RE.render_annotated_plans_pdf({"zzz": two_page_pdf}, ev)
    with pytest.raises(ValueError, match="no marked sheet"):
        RE.render_annotated_plans_pdf({"d1": two_page_pdf}, {**ev, "sheets": []})
    with pytest.raises(ValueError, match="too large"):
        RE.render_annotated_plans_pdf({"d1": two_page_pdf}, ev, max_bytes=10)


def test_marked_plans_spec_from_evidence_and_the_deliverable_spec_extension(monkeypatch):
    spec = RE.marked_plans_spec_from_evidence(_evidence())
    assert spec is not None and [s["sheet_number"] for s in spec["sheets"]] == ["E2.01", "E3.01"]
    assert spec["sheets"][0]["marks"][1]["label"] == "x2"
    assert RE.marked_plans_spec_from_evidence({**_evidence(), "sheets": []}) is None
    db = fx.FakeDB({"plans_reader_outputs": [
        {"id": "o1", "run_id": RUN_ID, "scope": "evidence", "version": 1, "output": {**_evidence(), "sheets": []}},
        {"id": "o2", "run_id": RUN_ID, "scope": "evidence", "version": 2, "output": _evidence()},
    ]})
    assert RE.latest_evidence(db, RUN_ID)["sheets"]
    from app.core import config

    monkeypatch.setattr(config, "get_settings", lambda: Settings(_env_file=None, plans_reader_marked_plans_mode="model"))
    assert RE.deliverable_spec_from_evidence("marked_plans", db, {"id": RUN_ID}) is None
    monkeypatch.setattr(config, "get_settings", lambda: Settings(_env_file=None, plans_reader_marked_plans_mode="code"))
    assert RE.deliverable_spec_from_evidence("marked_plans", db, {"id": RUN_ID})["sheets"][0]["sheet_number"] == "E2.01"
    assert RE.deliverable_spec_from_evidence("boq", db, {"id": RUN_ID}) is None
    assert RE.deliverable_spec_from_evidence("marked_plans", None, {"id": RUN_ID}) is None


def test_render_annotated_plans_stores_a_versioned_row_and_rerenders_from_evidence(two_page_pdf):
    db = fx.FakeDB(
        {
            "plans_reader_documents": [{"id": "d1", "run_id": RUN_ID, "storage_path": "plans-reader/x/source/d1.pdf"}],
            "plans_reader_outputs": [{"id": "o1", "run_id": RUN_ID, "scope": "evidence", "version": 1, "output": _evidence()}],
            "plans_reader_deliverables": [],
        },
        unique={"plans_reader_deliverables": (("run_id", "kind", "version"),)},
    )
    uploads: list[tuple[str, int, str]] = []
    row = RE.render_annotated_plans(
        db, {"id": RUN_ID}, evidence=_evidence(), version=1, user={"id": "u1"},
        upload=lambda p, d, ct: uploads.append((p, len(d), ct)), download=lambda p: two_page_pdf,
    )
    assert row["kind"] == "annotated_plans" and row["version"] == 1 and row["filename"] == "Annotated Plans.pdf"
    assert row["spec_json"]["counts"] == {"Circle": 3, "PolyLine": 1, "FreeText": 2, "pages": 2}
    assert row["created_by"] == "u1" and uploads[0][2] == "application/pdf" and uploads[0][1] == row["size_bytes"]
    assert uploads[0][0].startswith(f"plans-reader/{RUN_ID}/deliverables/v1/")
    # A taken version moves to the next number; the re-render reads the stored evidence.
    again = RE.render_annotated_plans(db, {"id": RUN_ID}, evidence=_evidence(), version=1,
                                      upload=lambda p, d, ct: None, download=lambda p: two_page_pdf)
    assert again["version"] == 2
    third = RE.rerender_from_evidence(db, {"id": RUN_ID}, upload=lambda p, d, ct: None, download=lambda p: two_page_pdf)
    assert third["version"] == 3 and RE.latest_annotated(db, RUN_ID)["version"] == 3
    db.tables["plans_reader_outputs"].clear()
    with pytest.raises(LookupError):
        RE.rerender_from_evidence(db, {"id": RUN_ID}, upload=lambda p, d, ct: None, download=lambda p: two_page_pdf)


# ---------------------------------------------------------------------------
# Storage failures (first live WS4 iteration: storage3 raised
# json.JSONDecodeError "Expecting value" on a non-JSON error page and the run
# ended done_with_errors)
# ---------------------------------------------------------------------------


def _store_db(evidence: dict | None = None) -> fx.FakeDB:
    return fx.FakeDB(
        {
            "plans_reader_documents": [{"id": "d1", "run_id": RUN_ID, "storage_path": "plans-reader/x/source/d1.pdf"}],
            "plans_reader_outputs": [
                {"id": "o1", "run_id": RUN_ID, "scope": "evidence", "version": 1, "output": evidence or _evidence()}
            ],
            "plans_reader_deliverables": [],
        },
        unique={"plans_reader_deliverables": (("run_id", "kind", "version"),)},
    )


def _flaky_upload(failures: list[BaseException]):
    calls: list[str] = []

    def put(path, data, content_type):
        calls.append(path)
        if failures:
            raise failures.pop(0)

    return put, calls


def _non_json_error() -> json.JSONDecodeError:
    try:
        json.loads("")
    except json.JSONDecodeError as exc:
        return exc
    raise AssertionError("unreachable")


def test_a_non_json_storage_error_is_retried_once_after_a_pause(two_page_pdf):
    db = _store_db()
    put, calls = _flaky_upload([_non_json_error()])
    pauses: list[float] = []
    row = RE.render_annotated_plans(
        db, {"id": RUN_ID}, evidence=_evidence(), version=1, upload=put, download=lambda p: two_page_pdf,
        sleep=pauses.append,
    )
    assert len(calls) == 2 and pauses == [RE.UPLOAD_RETRY_PAUSE_S]
    # The retry mints a fresh object path, so a landed first try never 409s.
    assert calls[0] != calls[1] and all(c.startswith(f"plans-reader/{RUN_ID}/deliverables/v1/") for c in calls)
    assert row["storage_path"] == calls[1] and row["version"] == 1


def test_a_transport_error_is_retried_and_a_persisting_failure_raises_an_app_message(two_page_pdf):
    import httpx

    db = _store_db()
    put, calls = _flaky_upload([httpx.ConnectError("nodename nor servname provided"), _non_json_error()])
    pauses: list[float] = []
    with pytest.raises(RE.AnnotatedPlansStoreError) as info:
        RE.render_annotated_plans(
            db, {"id": RUN_ID}, evidence=_evidence(), version=1, upload=put, download=lambda p: two_page_pdf,
            sleep=pauses.append,
        )
    assert isinstance(info.value, ValueError) and str(info.value) == RE.MSG_STORE_FAILED
    assert "Expecting value" not in str(info.value)
    assert len(calls) == RE.UPLOAD_ATTEMPTS == 2 and pauses == [RE.UPLOAD_RETRY_PAUSE_S]
    assert db.tables["plans_reader_deliverables"] == []


def test_a_client_side_storage_error_is_not_retried(two_page_pdf):
    from storage3.exceptions import StorageApiError

    db = _store_db()
    put, calls = _flaky_upload([StorageApiError("Invalid key", "InvalidKey", 400)])
    pauses: list[float] = []
    with pytest.raises(RE.AnnotatedPlansStoreError):
        RE.render_annotated_plans(
            db, {"id": RUN_ID}, evidence=_evidence(), version=1, upload=put, download=lambda p: two_page_pdf,
            sleep=pauses.append,
        )
    assert len(calls) == 1 and pauses == []
    assert RE._is_retryable_store_error(StorageApiError("Bad gateway", "InternalError", 502))
    assert RE._is_retryable_store_error(StorageApiError("Unable to parse error message: x", "InternalError", 400))
    assert not RE._is_retryable_store_error(ValueError("no marked sheet"))


def test_a_source_download_failure_raises_an_app_message(two_page_pdf):
    def download(path):
        raise _non_json_error()

    with pytest.raises(RE.AnnotatedPlansStoreError) as info:
        RE.render_annotated_plans(_store_db(), {"id": RUN_ID}, evidence=_evidence(), version=1,
                                  upload=lambda p, d, ct: None, download=download, sleep=lambda s: None)
    assert str(info.value) == RE.MSG_SOURCES_FAILED


def test_the_failure_note_is_recorded_once_and_cleared_by_the_next_render(two_page_pdf):
    db = _store_db()
    assert RE.record_annotated_failure(db, RUN_ID, RE.AnnotatedPlansStoreError(RE.MSG_STORE_FAILED)) is True
    assert RE.record_annotated_failure(db, RUN_ID, RE.AnnotatedPlansStoreError(RE.MSG_STORE_FAILED)) is False
    notes = RE.latest_evidence(db, RUN_ID)["notes"]
    assert notes == [f"{RE.FAILURE_NOTE_HEAD}: {RE.MSG_STORE_FAILED} Re-render makes a new copy."]
    # Library text never reaches the note; the renderer's own ValueError does.
    assert RE.MSG_RENDER_FAILED in RE.failure_note(_non_json_error())
    assert RE.MSG_RENDER_FAILED in RE.failure_note(RuntimeError("boom"))
    assert "no marked sheet" in RE.failure_note(ValueError("The evidence has no marked sheet to annotate."))
    # A missing evidence row is not an error.
    assert RE.record_annotated_failure(fx.FakeDB({"plans_reader_outputs": []}), RUN_ID, RuntimeError("x")) is False
    row = RE.rerender_from_evidence(db, {"id": RUN_ID}, upload=lambda p, d, ct: None, download=lambda p: two_page_pdf)
    assert row["version"] == 1
    assert not RE.latest_evidence(db, RUN_ID).get("notes")


def test_the_spine_seam_module_resolves_the_renderer():
    import importlib

    from app.services import plans_reader_runs as runs

    module = importlib.import_module(runs.ANNOTATED_PLANS_SEAM[0])
    assert getattr(module, runs.ANNOTATED_PLANS_SEAM[1]) is RE.render_annotated_plans
    assert RE.CODE_KINDS == ("annotated_plans",)


def test_module_has_no_dashes():
    for path in (Path(RE.__file__), Path(__file__)):
        text = path.read_text(encoding="utf-8")
        assert chr(0x2013) not in text and chr(0x2014) not in text


# Golden 7154 ($0, the backup on disk) ----------------------------------------------


@pytest.fixture(scope="module")
def annotated_7154() -> tuple[bytes, dict]:
    if not (ELEC_PDF.exists() and MECH_PDF.exists()):
        pytest.skip("7154 backup not on this machine")
    from app.services import plans_reader_evidence as E
    from app.services.plans_reader_evidence_inputs import LocalPdfInputs

    pdfs = {ELEC_ID: ELEC_PDF.read_bytes(), MECH_ID: MECH_PDF.read_bytes()}
    evidence = E.build(None, "ff8fad25", main_version=1, boq_version=1, inputs=_fixture_inputs(), sheets=LocalPdfInputs(pdfs))
    data = evidence.to_dict()
    return RE.render_annotated_plans_pdf(pdfs, data), data


@golden
@needs_backup
def test_7154_annotated_plans_under_6mb_with_annotation_counts_equal_to_evidence(annotated_7154):
    pdf, data = annotated_7154
    expected = RE.expected_counts(data)
    got = RE.count_annotations(pdf)
    assert got == expected
    assert RE.count_annotations_pdfium(pdf) == expected["Circle"] + expected["PolyLine"] + expected["FreeText"]
    assert len(pdf) < SIX_MB
    print("7154 annotated plans:", json.dumps({"bytes": len(pdf), **expected}))

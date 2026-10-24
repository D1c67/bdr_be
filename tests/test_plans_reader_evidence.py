"""WS8 evidence (spec section 6 WS8 wave 2: placement, registration,
polylines, chips, storage, replay stage; section 5.4 outputs scope
`evidence`).

Synthetic sheets (canned SheetInputs) cover each placement method;
synthetic PDFs from tests/pagefacts_fixtures.py run through the real WS1
extraction for a /Rotate 90 page, an SHX-only page and a two-scale sheet.
The stored 7154 run fixtures (no prices) check that every BOQ line gets a
quantity chip. Golden tests (marked `golden`, skipped without the backup)
read the 7154 backup: E3.01 anchors and registration, and the ground-truth
mark accuracy against the estimator's flattened Marked Plans.pdf (dots
detected by color at $0, registered to the source sheets). No network, no
database, no model.
"""

from __future__ import annotations

import json
import math
from collections import Counter, deque
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.services import plans_reader_evidence as E
from app.services import plans_reader_replay as RP
from app.services.pagefacts.contracts import Word
from app.services.plans_reader_evidence_inputs import (
    FakeEvidenceInputs,
    LocalPdfInputs,
    SheetInputs,
    UnavailableEvidenceInputs,
)
from app.services.pricing_types import PricedLine, PriceRow, ValueStat
from tests.pagefacts_fixtures import Shx, make_pdf, sheet, title_block_texts

DOC = "d0c5e7a1-1111-4222-8333-944455556666"
PAGE = 3
FIX = Path(__file__).parent / "fixtures" / "plans_reader" / "run_7154"
BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
PROJ = BACKUP / "storage/project-files/7dfc64e6-36ef-4c67-9d02-0d7942015011"
ELEC_PDF = PROJ / "drawing/c85a4720840d49a288d9d0c7d3c87fed-Reference Material - Architectural - ELEC_RESPONSE_TO_CLV_COMMENTS D1.pdf"
MECH_PDF = PROJ / "drawing/9466b0a7b03b42c4bd9bf8feac10c9e0-Reference Material - Architectural - MECH_RESPONSE_TO_CLV_COMMENTS D1.pdf"
MARKED_PDF = PROJ / "marked_plans/b805d44f07d947c1857d6d9c4d41d9f1-Marked Plans.pdf"
ELEC_ID = "538c95b5-3df0-4d7e-b18b-c01135083d20"
MECH_ID = "a9720d0e-6c74-4d06-b23f-3071a6f661a1"
golden = pytest.mark.golden
needs_backup = pytest.mark.skipif(
    not (ELEC_PDF.exists() and MECH_PDF.exists() and MARKED_PDF.exists()), reason="7154 backup not on this machine"
)


# Builders ---------------------------------------------------------------------------


def _word(t: str, x_in: float, y_in: float, *, w_in: float = 42.0, h_in: float = 30.0) -> Word:
    width = 0.1 * len(t)
    return Word(t=t, b=[x_in / w_in, (y_in - 0.1) / h_in, (x_in + width) / w_in, y_in / h_in], h=7.0)


def _center(t: str, x_in: float, y_in: float) -> tuple[float, float]:
    w = _word(t, x_in, y_in)
    return (round(w.cx, 4), round(w.cy, 4))


def _sheet(words: list[Word], **kw: Any) -> SheetInputs:
    base: dict[str, Any] = {
        "document_id": DOC,
        "page": PAGE,
        "w_in": 42.0,
        "h_in": 30.0,
        "rotation": 0,
        "text_class": "cad_text",
        "words": words,
        "primary_ft_per_in": 4.0,
        "sheet_number": "E3.01",
        "roles": ["plan"],
        "page_role": "plan",
    }
    base.update(kw)
    return SheetInputs(**base)


def _row(rid: str, desc: str, qty: float, points: list[tuple[float, float]] | None = None, **kw: Any) -> dict:
    row = {
        "row_id": rid,
        "description": desc,
        "quantity": qty,
        "unit": "EA",
        "sheet": {"document_id": DOC, "page": PAGE, "sheet_number": "E3.01", "sheet_key": "D1P3"},
        "positions": [list(p) for p in points or []],
        "coverage": "sheet_total",
        "status": "active",
    }
    row.update(kw)
    return row


def _category(rows: list[dict], schedules: list[dict] | None = None) -> dict:
    return {
        "category": "electrical_drawings",
        "rows_version": 2,
        "counts": rows,
        "runs": [],
        "schedules": schedules or [],
        "specs": [],
        "scope": [],
        "details": [],
        "questions": [],
        "pages": [{"document_id": DOC, "page": PAGE, "sheet_number": "E3.01"}],
    }


def _line(lid: str, desc: str, qty: float, unit: str = "EA", refs: list[str] | None = None, **kw: Any) -> dict:
    item = {
        "line_id": lid,
        "description": desc,
        "quantity": qty,
        "unit": unit,
        "sheet_refs": ["E3.01"],
        "source_refs": [{"ref": r, "multiplier": 1} for r in refs or []],
        "is_accessory": False,
        "wastage_applies": unit == "FT",
    }
    item.update(kw)
    return item


def _boq(groups: dict[str, list[dict]]) -> dict:
    return {
        "areas": [
            {
                "area": "Synthetic Area",
                "sections": [
                    {
                        "heading": "NEW WORK",
                        "groups": [
                            {"name": name, "subgroups": [{"name": "", "items": items}]}
                            for name, items in groups.items()
                        ],
                    }
                ],
            }
        ]
    }


DOCS = [{"id": DOC, "category": "electrical_drawings", "sheet_width_in": 42.0, "sheet_height_in": 30.0,
         "created_at": "2026-10-21T10:00:00+00:00"}]


def _build(boq: dict, rows: list[dict], sheet_in: SheetInputs | None, *, measure: dict | None = None,
           writer: Any = None, schedules: list[dict] | None = None) -> E.EvidenceOutput:
    fake = E.FakeRunInputs(documents=DOCS, boq=boq, categories=[_category(rows, schedules)], measure=measure)
    inputs = FakeEvidenceInputs({(DOC, PAGE): sheet_in}) if sheet_in is not None else UnavailableEvidenceInputs()
    return E.build(None, "run-1", main_version=1, boq_version=1, inputs=fake, sheets=inputs, writer=writer)


def _marks(out: E.EvidenceOutput, line_id: str | None = None, method: str | None = None) -> list[dict]:
    return [m for m in out.marks() if (line_id is None or m["line_id"] == line_id) and (method is None or m["method"] == method)]


# Placement --------------------------------------------------------------------------


def _grid_l1(n: int = 21) -> list[tuple[float, float]]:
    return [(4.0 + (i % 7) * 4.0, 6.0 + (i // 7) * 5.0) for i in range(n)]


def test_shared_tag_own_tag_anchor_and_variant_assigned_by_distance():
    spots = _grid_l1()
    words = [_word("L1L1" if i % 3 == 0 else "L1", x, y) for i, (x, y) in enumerate(spots)]
    centers = [_center("L1L1" if i % 3 == 0 else "L1", x, y) for i, (x, y) in enumerate(spots)]
    em_idx = [2, 6, 10, 14, 19]
    em_points = [(centers[i][0] + 0.002, centers[i][1] - 0.002) for i in em_idx]
    rows = [_row("C1", "L1 TROFFER", 16), _row("C2", "L1 TROFFER LIFE SAFETY", 5, em_points)]
    boq = _boq({"LIGHTING FIXTURES": [
        _line("B1", "L1, 2X4 RECESSED LED TROFFER", 16, refs=["C1"]),
        _line("B2", "L1, 2X4 RECESSED LED TROFFER, LIFE SAFETY", 5, refs=["C2"]),
    ]})
    out = _build(boq, rows, _sheet(words))
    b1, b2 = _marks(out, "B1"), _marks(out, "B2")
    assert len(b1) == 16 and {m["method"] for m in b1} == {"anchor"}
    assert len(b2) == 5 and {m["method"] for m in b2} == {"assigned"}
    got = sorted(tuple(m["points"][0]) for m in b2)
    assert got == sorted(centers[i] for i in em_idx)
    assert all(m["shared_tag"] for m in b1 + b2)
    assert not _marks(out, method="unclaimed")
    lines = {ln["line_id"]: ln for ln in out.lines}
    assert lines["B1"]["qty_chip"]["kind"] == "counted" and lines["B1"]["qty_chip"]["tone"] == "ok"
    assert lines["B1"]["not_located"] == 0 and lines["B2"]["not_located"] == 0
    assert out.stats["marks_by_method"] == {"anchor": 16, "assigned": 5}
    assert out.stats["anchor_share"] == 1.0  # assigned marks are excluded from the metric
    sheet = out.sheets[0]
    assert sheet["sheet_key"] == "D1P3" and sheet["sheet_number"] == "E3.01"
    assert sheet["anchor_counts"]["L1"] == 21


def test_registration_recovers_a_frame_error_and_registers_untagged_points():
    sx, dx, sy, dy = 0.95, 0.01, 1.10, -0.02
    spots = [(5.0 + (i % 4) * 7.0, 6.0 + (i // 4) * 6.0) for i in range(12)]
    words = [_word("L2", x, y) for x, y in spots]
    centers = [_center("L2", x, y) for x, y in spots]
    model = [((cx - dx) / sx, (cy - dy) / sy) for cx, cy in centers]
    rec_true = [(0.30, 0.40), (0.62, 0.55)]
    rec_model = [((x - dx) / sx, (y - dy) / sy) for x, y in rec_true]
    rows = [_row("C1", "L2 TROFFER", 12, model), _row("C2", "DUPLEX RECEPTACLE", 2, rec_model)]
    boq = _boq({
        "LIGHTING FIXTURES": [_line("B1", "L2, 2X2 RECESSED LED TROFFER", 12, refs=["C1"])],
        "OTHER ITEMS": [_line("B2", "DUPLEX RECEPTACLE", 2, refs=["C2"])],
    })
    out = _build(boq, rows, _sheet(words))
    reg = out.sheets[0]["registration"]
    assert reg["ok"] and abs(reg["sx"] - sx) < 0.01 and abs(reg["sy"] - sy) < 0.01
    assert reg["median_residual_ft"] is not None and reg["median_residual_ft"] < 0.2
    b2 = _marks(out, "B2")
    assert [m["method"] for m in b2] == ["registered", "registered"]
    for m, truth in zip(sorted(b2, key=lambda m: m["points"][0][0]), rec_true, strict=True):
        assert abs(m["points"][0][0] - truth[0]) < 0.002 and abs(m["points"][0][1] - truth[1]) < 0.002
    assert {m["method"] for m in _marks(out, "B1")} == {"anchor"}


def test_registration_refuses_few_pairs_and_out_of_range_scales():
    anchors = [(0.1 + 0.1 * i, 0.2 + 0.05 * i) for i in range(8)]
    few = E.fit_registration([(anchors[:3], anchors)], 42.0, 30.0, 4.0)
    assert not few.ok and few.reason == "too_few_pairs"
    squashed = [(0.5 + (x - 0.5) * 0.5, y) for x, y in anchors]  # true sx = 2.0
    bad = E.fit_registration([(squashed, anchors)], 42.0, 30.0, 4.0)
    assert not bad.ok and bad.reason == "scale_out_of_range"
    good = E.fit_registration([(anchors, anchors)], 42.0, 30.0, 4.0)
    assert good.ok and abs(good.sx - 1) < 1e-6 and good.median_residual_ft == 0.0


def test_sheet_without_tag_pairs_borrows_the_document_registration():
    """A sheet with no tag-aware pairs (untagged receptacles) is placed
    through the accepted registration of another sheet of the same document
    and size: the model saw both pages through the same frame."""
    sx, dx, sy, dy = 0.95, 0.01, 1.10, -0.02
    spots = [(5.0 + (i % 4) * 7.0, 6.0 + (i // 4) * 6.0) for i in range(12)]
    words = [_word("L2", x, y) for x, y in spots]
    centers = [_center("L2", x, y) for x, y in spots]
    model = [((cx - dx) / sx, (cy - dy) / sy) for cx, cy in centers]
    page2 = PAGE + 1
    rec_true = [(0.30, 0.40), (0.62, 0.55), (0.45, 0.70)]
    rec_model = [((x - dx) / sx, (y - dy) / sy) for x, y in rec_true]
    rows = [_row("C1", "L2 TROFFER", 12, model),
            _row("C2", "DUPLEX RECEPTACLE", 3, rec_model,
                 sheet={"document_id": DOC, "page": page2, "sheet_number": "E2.01", "sheet_key": "D1P4"})]
    boq = _boq({"LIGHTING FIXTURES": [_line("B1", "L2, 2X2 RECESSED LED TROFFER", 12, refs=["C1"])],
                "OTHER ITEMS": [_line("B2", "DUPLEX RECEPTACLE", 3, refs=["C2"], sheet_refs=["E2.01"])]})
    cat = _category(rows)
    cat["pages"].append({"document_id": DOC, "page": page2, "sheet_number": "E2.01"})
    fake = E.FakeRunInputs(documents=DOCS, boq=boq, categories=[cat])
    sheets = FakeEvidenceInputs({(DOC, PAGE): _sheet(words),
                                 (DOC, page2): _sheet([_word("NOTE", 1.0, 1.0)], page=page2, sheet_number="E2.01")})
    out = E.build(None, "run-1", main_version=1, boq_version=1, inputs=fake, sheets=sheets)
    by_page = {s["page"]: s for s in out.sheets}
    own = by_page[PAGE]["registration"]
    assert own["ok"] and abs(own["sy"] - sy) < 0.01 and own["prior_from"] is None
    borrowed = by_page[page2]["registration"]
    assert borrowed["ok"] and borrowed["reason"] == "document_prior" and borrowed["prior_from"] == "D1P3"
    assert borrowed["pairs"] == 0 and abs(borrowed["sx"] - sx) < 0.01
    marks = sorted((m for m in by_page[page2]["marks"] if m["line_id"] == "B2"), key=lambda m: m["points"][0][1])
    assert [m["method"] for m in marks] == ["registered"] * 3
    for m, truth in zip(marks, sorted(rec_true, key=lambda t: t[1]), strict=True):
        assert abs(m["points"][0][0] - truth[0]) < 0.002 and abs(m["points"][0][1] - truth[1]) < 0.002
    # Without a same-document donor the sheet keeps the raw model points.
    only_b2 = _boq({"OTHER ITEMS": [_line("B2", "DUPLEX RECEPTACLE", 3, refs=["C2"], sheet_refs=["E2.01"])]})
    alone = E.build(None, "run-1", main_version=1, boq_version=1,
                    inputs=E.FakeRunInputs(documents=DOCS, boq=only_b2, categories=[cat]), sheets=sheets)
    assert {m["method"] for m in alone.marks() if m["line_id"] == "B2"} == {"approx"}


def test_a_duplicate_model_point_near_a_held_anchor_stacks_on_it():
    words = [_word("D", 10.0, 10.0), _word("D", 20.0, 10.0)]
    cd = [_center("D", 10.0, 10.0), _center("D", 20.0, 10.0)]
    # Four model points: each dimmer read twice, the twin 0.4 in (1.6 ft) away.
    pts = [cd[0], (cd[0][0] + 0.4 / 42, cd[0][1]), cd[1], (cd[1][0] + 0.4 / 42, cd[1][1])]
    rows = [_row("C1", "WALL DIMMER", 4, pts)]
    boq = _boq({"LIGHTING CONTROLS": [_line("B1", "WALL DIMMER", 4, refs=["C1"])]})
    out = _build(boq, rows, _sheet(words))
    marks = _marks(out, "B1")
    assert [m["method"] for m in marks] == ["snapped", "snapped"]
    assert sorted(m["count"] for m in marks) == [2, 2]
    assert out.line("B1")["not_located"] == 2


def test_snap_within_radius_only_and_each_label_once():
    d1 = _center("D", 10.0, 10.0)
    words = [_word("D", 10.0, 10.0), _word("D", 15.0, 10.0)]
    near = [(d1[0] + 0.5 / 42, d1[1]), (d1[0] - 0.5 / 42, d1[1] + 0.2 / 30)]  # both within 3 ft of D1
    rows = [_row("C1", "WALL DIMMER", 2, near)]
    boq = _boq({"LIGHTING CONTROLS": [_line("B1", "WALL DIMMER", 2, refs=["C1"])]})
    out = _build(boq, rows, _sheet(words))
    methods = Counter(m["method"] for m in _marks(out, "B1"))
    assert methods == {"snapped": 1, "approx": 1}
    snapped = _marks(out, "B1", "snapped")[0]
    assert tuple(snapped["points"][0]) == d1 and snapped["ref"].startswith("t:")
    assert not _marks(out, method="unclaimed")  # hint labels are never "tag not counted"


def test_stacked_model_points_merge_with_a_count_and_the_rest_is_not_located():
    p = (0.4, 0.4)
    far = (0.4 + 0.5 / 42, 0.4)  # 0.5 in = 2 ft at 1/4": a separate device
    rows = [_row("C1", "DUPLEX RECEPTACLE", 4, [p, p, p, far])]
    boq = _boq({"OTHER ITEMS": [_line("B1", "DUPLEX RECEPTACLE", 4, refs=["C1"])]})
    out = _build(boq, rows, _sheet([_word("NOTE", 1.0, 1.0)]))
    marks = _marks(out, "B1")
    assert len(marks) == 2 and sorted(m["count"] for m in marks) == [1, 3]
    assert {m["method"] for m in marks} == {"approx"} and {m["style"] for m in marks} == {"ring"}
    line = out.line("B1")
    assert line["not_located"] == 2 and out.stats["stacked_merged"] == 2
    assert line["qty_chip"]["tone"] == "warn" and line["qty_chip"]["text"] == "Counted 4, marked 2, 2 not located"


def test_fewer_points_than_quantity_leaves_the_rest_not_located():
    rows = [_row("C1", "DUPLEX RECEPTACLE", 5, [(0.2, 0.2), (0.5, 0.6)])]
    boq = _boq({"OTHER ITEMS": [_line("B1", "DUPLEX RECEPTACLE", 5, refs=["C1"])]})
    out = _build(boq, rows, _sheet([_word("NOTE", 1.0, 1.0)]))
    assert len(_marks(out, "B1")) == 2 and out.line("B1")["not_located"] == 3


def test_demo_x_family_is_assigned_and_spare_tags_are_not_counted_marks():
    spots = [(5.0 + 4 * i, 8.0) for i in range(5)]
    words = [_word("(X)", x, y) for x, y in spots]
    rows = [_row("C1", "REMOVE DUPLEX RECEPTACLE (X)", 2, family_tags=["demo_x"]),
            _row("C2", "REMOVE LIGHT FIXTURE (X)", 1, family_tags=["demo_x"])]
    boq = _boq({"DEMOLITION": [_line("B1", "REMOVE DUPLEX RECEPTACLE", 2, refs=["C1"]),
                               _line("B2", "REMOVE LIGHT FIXTURE", 1, refs=["C2"])]})
    out = _build(boq, rows, _sheet(words))
    assert len(_marks(out, "B1", "assigned")) == 2 and len(_marks(out, "B2", "assigned")) == 1
    spare = _marks(out, method="unclaimed")
    assert len(spare) == 2 and all(m["line_id"] is None and m["tag"] == "(X)" for m in spare)
    legend = out.sheets[0]["legend"]
    nc = [r for r in legend if r["line_id"] is None]
    assert nc == [{"prefix": "NC, (X)", "description": "Tag (X) not counted", "quantity": 2, "unit": "EA",
                   "line_id": None, "marked": 2, "not_located": 0}]
    assert out.stats["unclaimed_anchors"] == 2 and out.stats["anchor_share"] is None


def test_unusable_text_layer_falls_back_to_model_points_and_unknown_counts():
    rows = [_row("C1", "L1 TROFFER", 2, [(0.3, 0.3), (0.6, 0.3)])]
    boq = _boq({"LIGHTING FIXTURES": [_line("B1", "L1, 2X4 TROFFER", 2, refs=["C1"])]})
    out = _build(boq, rows, _sheet([], text_class="scanned"))
    sheet = out.sheets[0]
    assert sheet["anchor_method"] == "unavailable" and sheet["anchor_counts"] == {"L1": None}
    assert Counter(m["method"] for m in _marks(out, "B1")) == {"approx": 2}
    # No page facts at all: same, the sheet still resolves from the documents.
    out2 = _build(boq, rows, None)
    assert out2.sheets[0]["anchor_method"] == "unavailable" and len(_marks(out2, "B1")) == 2


# Synthetic PDFs through WS1 (rotated page, SHX-only page, two-scale sheet) --------


def _pdf_build(pdf: bytes, boq: dict, rows: list[dict]) -> E.EvidenceOutput:
    fake = E.FakeRunInputs(documents=DOCS, boq=boq, categories=[_category(rows)])
    inputs = LocalPdfInputs({DOC: pdf}, page_budget_s=8.0, doc_budget_s=120.0)
    return E.build(None, "run-1", main_version=1, boq_version=1, inputs=fake, sheets=inputs)


def _rows_page1(rows: list[dict]) -> list[dict]:
    for r in rows:
        r["sheet"] = {"document_id": DOC, "page": 1, "sheet_number": None}
    return rows


TAGS = [(6.0, 8.0, 7, "L1"), (12.0, 9.0, 7, "L1L1"), (18.0, 14.0, 7, "L5/1LH"), (24.0, 20.0, 7, "X1")]


def _tag_sheet(rotate: int):
    texts = list(TAGS) + title_block_texts("E3.01", "LIGHTING PLAN")
    texts += [(3.0, 27.6, 18, "LIGHTING PLAN - NEW WORK"), (3.0, 28.0, 7, '1/4" = 1\'-0"')]
    return sheet(*texts, rotate=rotate)


def test_rotate_90_page_marks_match_the_unrotated_page():
    def run(rotate: int) -> E.EvidenceOutput:
        rows = _rows_page1([_row("C1", "L1", 2), _row("C2", "X1", 1)])
        boq = _boq({"LIGHTING FIXTURES": [_line("B1", "L1, 2X4 TROFFER", 2, refs=["C1"]),
                                          _line("B2", "X1, EXIT SIGN", 1, refs=["C2"])]})
        return _pdf_build(make_pdf([_tag_sheet(rotate)]), boq, rows)

    flat, rot = run(0), run(90)
    pf = sorted((m["line_id"], tuple(m["points"][0])) for m in flat.marks())
    pr = sorted((m["line_id"], tuple(m["points"][0])) for m in rot.marks())
    assert [p[0] for p in pf] == ["B1", "B1", "B2"] == [p[0] for p in pr]
    for (_, a), (_, b) in zip(pf, pr, strict=True):
        assert abs(a[0] - b[0]) < 0.004 and abs(a[1] - b[1]) < 0.004
    assert {m["method"] for m in rot.marks()} == {"anchor"}
    assert rot.sheets[0]["w_in"] == 42.0 and rot.sheets[0]["scale"]["ft_per_in"] == 4.0
    # The X1 mark sits on its drawn tag (left edge 24 in, baseline 20 in).
    x1 = [m for m in rot.marks() if m["line_id"] == "B2"][0]["points"][0]
    assert abs(x1[0] * 42 - 24.1) < 0.2 and abs(x1[1] * 30 - 19.96) < 0.2


def test_shx_only_page_places_marks_on_shx_anchors():
    shx = [
        Shx(6.0, 8.0, 0.25, 0.12, "L3"),
        Shx(12.0, 9.0, 0.25, 0.12, "L3"),
        Shx(2.0, 1.0, 30.0, 0.2, "GENERAL NOTE " * 45),
    ]
    rows = _rows_page1([_row("C1", "L3 DOWNLIGHT", 2)])
    boq = _boq({"LIGHTING FIXTURES": [_line("B1", "L3, 6-INCH DOWNLIGHT", 2, refs=["C1"])]})
    out = _pdf_build(make_pdf([sheet(shx=shx)]), boq, rows)
    marks = _marks(out, "B1")
    assert len(marks) == 2 and {m["method"] for m in marks} == {"anchor"}
    xs = sorted(round(m["points"][0][0] * 42, 2) for m in marks)
    assert abs(xs[0] - 6.125) < 0.05 and abs(xs[1] - 12.125) < 0.05


def test_two_scale_sheet_stacks_by_feet_in_each_view():
    texts = [
        (2.0, 27.6, 18, "FIRST FLOOR POWER PLAN"),
        (2.0, 28.0, 7, '1/4" = 1\'-0"'),
        (17.0, 27.6, 18, "ENLARGED ELECTRICAL ROOM PLAN"),
        (17.0, 28.0, 7, '1/2" = 1\'-0"'),
        (4.0, 4.0, 7, "NOTE"),
    ] + title_block_texts("E2.02", "POWER PLANS")
    # Two model points 0.4 in apart in each view: 1.6 ft at 1/4" (two devices),
    # 0.8 ft at 1/2" (one stacked device).
    plan = [(8.0 / 42, 10.0 / 30), (8.4 / 42, 10.0 / 30)]
    enlarged = [(20.0 / 42, 10.0 / 30), (20.4 / 42, 10.0 / 30)]
    rows = _rows_page1([_row("C1", "DUPLEX RECEPTACLE", 4, plan + enlarged)])
    boq = _boq({"OTHER ITEMS": [_line("B1", "DUPLEX RECEPTACLE", 4, refs=["C1"])]})
    out = _pdf_build(make_pdf([sheet(*texts)]), boq, rows)
    marks = sorted(_marks(out, "B1"), key=lambda m: m["points"][0][0])
    assert [m["count"] for m in marks] == [1, 1, 2]
    assert out.line("B1")["not_located"] == 1 and out.stats["stacked_merged"] == 1


# Polylines ---------------------------------------------------------------------------


def _measure() -> dict:
    """A MeasureOutput in the exact shape WS3 writes: route segments with
    `conductors_parsed` and `drawn: False` (synthesized), one derivation term
    per segment `{circuit_id, route_id, segment: <kind>, length_ft, count,
    basis}`, linear items with `measure_id` and `instances[]`, M rows that
    reach them through `refs: ["m:<measure_id>"]`."""
    def seg(kind: str, ft: float, basis: str, poly: list | None = None) -> dict:
        return {"kind": kind, "length_ft": ft, "conductors": "3#12", "conductors_parsed": {"#12": 3},
                "raceway": '3/4"C EMT', "cable": None, "document_id": DOC if poly else None, "page": PAGE if poly else None,
                "sheet_key": "D1P3", "polyline": poly[0] if poly and len(poly) == 1 else [], "polylines": poly or [],
                "ft_per_in": 4.0, "drawn": False, "basis": basis}

    def term(cid: str, rid: str, kind: str, ft: float, count: int, basis: str) -> dict:
        return {"circuit_id": cid, "route_id": rid, "segment": kind, "length_ft": ft, "count": count, "basis": basis}

    return {
        "version": 1,
        "routes": [
            {"route_id": "RT1", "circuit_id": "1LH-11", "basis": "measured_route", "total_ft": 40.0,
             "segments": [seg("collector", 30.0, "measured_route", [[[0.1, 0.5], [0.3, 0.5]]]),
                          seg("drop", 10.0, "assumed_route")]},
            {"route_id": "RT2", "circuit_id": "1LH-13", "basis": "assumed_route", "total_ft": 50.0,
             "segments": [seg("homerun", 50.0, "assumed_route", [[[0.3, 0.5], [0.3, 0.8]]])]},
        ],
        "linear_items": [{"measure_id": "lin1", "ref": "m:lin1", "kind": "linear_fixture", "tag": "L5", "document_id": DOC,
                          "page": PAGE, "sheet_key": "D1P3", "basis": "measured_route", "total_ft": 16.8,
                          "instances": [{"candidate_id": "c1", "bbox": [0.5, 0.2, 0.6, 0.21], "length_ft": 16.8,
                                         "method": "vector_rect"}]}],
        "quantities": [
            {"row_id": "M1", "description": "#12 THHN/THWN Cu", "quantity": 270.0, "unit": "FT", "basis": "measured_route",
             "refs": ["r:RT1", "r:RT2"],
             "derivation": {"terms": [term("1LH-11", "RT1", "collector", 30.0, 3, "measured_route"),
                                      term("1LH-11", "RT1", "drop", 10.0, 3, "assumed_route"),
                                      term("1LH-13", "RT2", "homerun", 50.0, 3, "assumed_route")]}},
            {"row_id": "M2", "description": '3/4"C EMT', "quantity": 90.0, "unit": "FT", "basis": "assumed_route",
             "refs": ["r:RT1", "r:RT2"],
             "derivation": {"terms": [term("1LH-11", "RT1", "collector", 30.0, 1, "measured_route"),
                                      term("1LH-11", "RT1", "drop", 10.0, 1, "assumed_route"),
                                      term("1LH-13", "RT2", "homerun", 50.0, 1, "assumed_route")]}},
            {"row_id": "M3", "description": "L5 UNDERCABINET", "quantity": 16.8, "unit": "FT", "basis": "measured_route",
             "refs": ["m:lin1"], "sheet_refs": ["D1P3"],
             "derivation": {"terms": [{"instance": 1, "length_ft": 16.8, "method": "vector_rect"}], "tag": "L5"}},
            {"row_id": "M4", "description": "#10 THHN/THWN Cu", "quantity": 300.0, "unit": "FT", "basis": "measured_route",
             "refs": ["r:RT1"], "derivation": {"terms": [term("1LH-11", "RT1", "collector", 30.0, 1, "measured_route")]}},
        ],
    }


def test_polylines_from_route_segments_with_length_checks_and_centerlines():
    boq = _boq({
        "WIRING": [_line("B1", "#12 THHN/THWN Cu", 270.0, "FT", refs=["M1"]),
                   _line("B2", '3/4"C EMT', 90.0, "FT", refs=["M2"]),
                   _line("B4", "#10 THHN/THWN Cu", 300.0, "FT", refs=["M4"])],
        "LIGHTING FIXTURES": [_line("B3", "L5, UNDERCABINET LED LIGHTING", 16.8, "FT", refs=["M3"])],
    })
    out = _build(boq, [], _sheet([_word("NOTE", 1.0, 1.0)]), measure=_measure())
    polys = out.marks(kind="polyline")
    by_line: dict[str, list[dict]] = {}
    for p in polys:
        by_line.setdefault(p["line_id"], []).append(p)
    wire = {p["ref"]: p for p in by_line["B1"]}
    assert set(wire) == {"r:RT1", "r:RT2"}
    assert wire["r:RT1"]["style"] == "solid" and wire["r:RT2"]["style"] == "dashed"
    assert wire["r:RT1"]["conductors"] == 3 and wire["r:RT1"]["segment_kind"] == "collector"
    b1 = out.line("B1")["length_check"]
    assert b1["drawn_ft"] == 240.0 and b1["expected_ft"] == 270.0 and b1["ok"] is True
    assert [(n["kind"], n["ft"]) for n in b1["not_drawn"]] == [("drop", 30.0)]
    assert out.line("B1")["qty_chip"]["kind"] == "measured" and out.line("B1")["qty_chip"]["tone"] == "ok"
    b2 = out.line("B2")
    assert b2["length_check"]["expected_ft"] == 90.0 and b2["length_check"]["ok"] is True
    assert b2["qty_chip"]["kind"] == "assumed_route" and b2["qty_chip"]["params"]["c"] == 2
    l5 = by_line["B3"][0]
    assert l5["points"] == [[0.5, 0.205], [0.6, 0.205]] and l5["segment_kind"] == "linear_item"
    assert l5["ref"] == "m:lin1"
    assert out.line("B3")["length_check"]["ok"] is True
    assert out.line("B4")["length_check"]["ok"] is False  # 30 drawn x 1 vs 300
    assert out.stats["ft_lines"] == 4 and out.stats["ft_lines_with_polyline"] == 4
    assert out.stats["length_mismatch"] == 1
    spec = E.evidence_to_marked_spec(out)
    kinds = Counter((m["kind"], m["style"]) for s in spec["sheets"] for m in s["marks"])
    assert kinds[("polyline", "dashed")] == 2 and kinds[("polyline", "solid")] >= 3


def test_measure_view_reads_the_ws3_term_shape():
    view = E.MeasureView(_measure())
    t = view.terms(view.quantities["M1"])[0]
    assert (t.route, t.segment_kind, t.kind, t.count, t.segments) == ("RT1", "collector", "collector", 3, None)
    lin = view.terms(view.quantities["M3"])[0]
    assert (lin.linear_item, lin.instance, lin.kind) == ("lin1", 1, "linear_item")
    # The documented WS3 term (plans_reader_measure.linear_derivation) names the item itself.
    from app.services.plans_reader_measure import linear_derivation

    deriv = linear_derivation(_measure()["linear_items"][0])
    named = view.terms({"row_id": "M8", "refs": [], "derivation": deriv})
    assert [(t.linear_item, t.instance, t.kind) for t in named] == [("lin1", 1, "linear_item")]
    # A linear-item row without the m: ref still links through the derivation tag.
    q = dict(view.quantities["M3"], refs=[])
    assert view.terms(q)[0].linear_item == "lin1"
    cc = view.terms({"row_id": "M9", "derivation": {"terms": [{"devices": 5, "ft_per_device": 25.0}]}})[0]
    assert (cc.kind, cc.length_ft, cc.route) == ("allowance", 125.0, None)


def test_multi_piece_collector_draws_every_piece_with_the_length_split():
    m = _measure()
    m["routes"][0]["segments"][0]["polylines"] = [[[0.1, 0.5], [0.2, 0.5]], [[0.2, 0.5], [0.2, 0.6], [0.3, 0.6]]]
    m["routes"][0]["segments"][0]["polyline"] = []
    boq = _boq({"WIRING": [_line("B2", '3/4"C EMT', 90.0, "FT", refs=["M2"])]})
    out = _build(boq, [], _sheet([_word("NOTE", 1.0, 1.0)]), measure=m)
    pieces = [p for p in out.marks(kind="polyline") if p["ref"] == "r:RT1"]
    assert len(pieces) == 2
    assert math.isclose(sum(p["length_ft"] for p in pieces), 30.0, abs_tol=0.02)
    assert out.line("B2")["length_check"]["drawn_ft"] == 80.0 and out.line("B2")["length_check"]["ok"] is True


def test_polylines_from_a_real_build_measure_output():
    """Fed by plans_reader_measure.build_measure on synthetic WS1 facts (one
    sheet, two circuits): every M row's FT line gets polylines, its length
    check closes, and the wire polylines carry the term's conductor count."""
    from app.services import plans_reader_measure as M
    from tests.test_plans_reader_measure import CONV, _single_sheet_inputs

    m = M.build_measure(_single_sheet_inputs(), CONV)
    lines = [_line(f"B{i}", q["description"], q["quantity"], "FT", refs=[q["row_id"]]) for i, q in enumerate(m["quantities"], start=1)]
    assert lines, "build_measure produced no M rows"
    boq = _boq({"WIRING": lines})
    docs = [{"id": "doc1", "category": "electrical_drawings", "sheet_width_in": 42.0, "sheet_height_in": 30.0,
             "created_at": "2026-10-21T10:00:00+00:00"}]
    fake = E.FakeRunInputs(documents=docs, boq=boq, categories=[_category([])], measure=m)
    out = E.build(None, "run-1", main_version=1, boq_version=1, inputs=fake, sheets=UnavailableEvidenceInputs())
    assert out.stats["ft_lines"] == len(lines) and out.stats["ft_lines_with_polyline"] == len(lines)
    view = E.MeasureView(m)
    for ln, q in zip(lines, m["quantities"]):
        check = out.line(ln["line_id"])["length_check"]
        assert check["ok"] is True, (q["description"], check)
        assert check["sources"] == [q["row_id"]]
        terms = view.terms(q)
        assert terms and all(t.segment_kind for t in terms)
        drawn_kinds = {p["segment_kind"] for p in out.marks(kind="polyline") if p["line_id"] == ln["line_id"]}
        assert drawn_kinds and drawn_kinds <= {"collector", "homerun", "switch_leg"}
        assert all(n["kind"] in E.NOT_DRAWN_TERM_KINDS for n in check["not_drawn"])
        if q["description"].startswith("#12"):
            counts = {t.count for t in terms}
            assert {p["conductors"] for p in out.marks(kind="polyline") if p["line_id"] == ln["line_id"]} <= counts


def test_centerline_variants():
    assert E.centerline({"rect": [0.1, 0.1, 0.11, 0.5]}, 42, 30) == [(0.105, 0.1), (0.105, 0.5)]
    poly = E.centerline({"polygon": [[0.1, 0.1], [0.3, 0.1], [0.3, 0.12], [0.1, 0.12]]}, 42, 30)
    assert [(round(x, 3), round(y, 3)) for x, y in poly] == [(0.3, 0.11), (0.1, 0.11)]
    assert E.centerline({"endpoints": [[0.1, 0.2], [0.4, 0.2]]}, 42, 30) == [(0.1, 0.2), (0.4, 0.2)]
    assert E.centerline({}, 42, 30) == []


# Chips -------------------------------------------------------------------------------


def test_quantity_chip_precedence_one_case_per_kind():
    rows = {
        "C1": {"row_id": "C1", "kind": "C", "coverage": "sheet_total"},
        "C2": {"row_id": "C2", "kind": "C", "coverage": "tile_subtotal"},
        "S1": {"row_id": "S1", "kind": "S"},
    }

    def chip(line: dict, **kw: Any) -> E.Chip:
        return E.quantity_chip(line, rows_by_id=rows, **kw)

    def ln(desc: str, unit: str = "EA", refs: list[str] | None = None, **kw: Any) -> dict:
        return _line("B1", desc, kw.pop("qty", 5), unit, refs=refs, **kw)

    cases = [
        (ln("#12 THHN/THWN Cu", refs=["C1"]), {}, "unit_error", "danger"),
        (ln("DUPLEX RECEPTACLE", refs=["C1"], combine="manual"), {}, "manual", "neutral"),
        (ln("PERMIT ALLOWANCE", "LS", refs=["p:R7"]), {}, "policy", "neutral"),
        (ln("DUPLEX RECEPTACLE"), {}, "unsourced", "danger"),
        (ln("#12 THHN/THWN Cu", "FT", caveat="(ASSUMED)"), {}, "assumed_route", "warn"),
        (ln("DUPLEX RECEPTACLE", refs=["C2"]), {}, "partial", "danger"),
        (ln("L5 UNDERCABINET LED", refs=["C1"]), {}, "linear_luminaire_ea", "warn"),
        (ln("DUPLEX RECEPTACLE", refs=["C1"], combine="floor"), {}, "census_floor", "warn"),
        (ln("DUPLEX RECEPTACLE", refs=["C1"]), {"marks": 5}, "counted", "ok"),
        (ln("DUPLEX RECEPTACLE", refs=["C1"]), {"marks": 3, "not_located": 2}, "counted", "warn"),
        (ln("#12 THHN", "FT", refs=["M1"], basis="measured"), {"poly": {"ok": True, "drawn_ft": 5}}, "measured", "ok"),
        (ln("#12 THHN", "FT", refs=["M1"], basis="assumed_route"), {"poly": {"circuits": 3}}, "assumed_route", "warn"),
        (ln("PANEL 1LH", refs=["S1"]), {}, "schedule", "ok"),
        (ln("SERVICE", refs=["C1"], basis="stated"), {}, "stated", "ok"),
        (ln("DUPLEX RECEPTACLE", refs=["C1", "S1"]), {}, "mixed", "warn"),
    ]
    for line, kw, kind, tone in cases:
        c = chip(line, **kw)
        assert (c.kind, c.tone) == (kind, tone), (line["description"], c)
        assert c.kind in E.QTY_CHIP_KINDS and c.tone in E.CHIP_TONES
        assert chr(0x2013) not in c.text() and chr(0x2014) not in c.text()
    counted = chip(ln("DUPLEX RECEPTACLE", refs=["C1"], qty=16), marks=16)
    assert counted.text() == "Counted 16, marked 16"
    assert chip(ln("#12 THHN", "FT", refs=["M1"], basis="assumed_route", qty=1453.8),
                poly={"circuits": 4}).text() == "Assumed route 1,453.8 FT, 4 circuits"


def _priced(lid: str, *, quality: str = "strong", method: str = "exact", n: int = 33,
            source: str = "library", flags: tuple[str, ...] = (), value: str | None = "0.5") -> PricedLine:
    row = PriceRow(line_id=lid, description="DUPLEX RECEPTACLE", group="OTHER ITEMS", subgroup=None, section=None,
                   unit="EA", quantity=Decimal(3), qty_base=Decimal(3), wastage_applies=False)
    stat = ValueStat(value=Decimal(value) if value is not None else None, n_obs=n if value else 0,
                     n_projects=26 if value else 0, quality=quality if value else "missing",
                     method=method if value else "none")
    return PricedLine(line_id=lid, row=row, item_key="receptacle:duplex", family="receptacle", uom="EA",
                      qty_eff=Decimal(3), labor=stat, material=stat, material_source=source, flags=flags)


def test_price_chips_for_every_priced_line_and_never_in_stored_evidence():
    assert E.price_chip(_priced("B1")).to_dict()["text"] == "Library median n=33"
    assert E.price_chip(_priced("B1", quality="good")).tone == "neutral"
    assert E.price_chip(_priced("B1", quality="weak")).kind == "library_weak"
    assert E.price_chip(_priced("B1", method="seed", quality="policy")).kind == "price_policy"
    assert E.price_chip(_priced("B1", source="quote:Q9", quality="quote", method="quote")).to_dict()["text"] == "Quote Q9"
    assert E.price_chip(_priced("B1", value=None, source="none")).kind == "missing"
    assert E.price_chip(_priced("B1", flags=("uom_conflict",))).kind == "uom_mismatch"
    assert E.price_chip(None).kind == "missing"
    assert E.price_chip(_priced("B1").to_dict()).kind == "library"  # dict form too

    rows = [_row("C1", "DUPLEX RECEPTACLE", 2, [(0.2, 0.2), (0.4, 0.4)])]
    boq = _boq({"OTHER ITEMS": [_line("B1", "DUPLEX RECEPTACLE", 2, refs=["C1"]),
                                _line("B2", "GFCI RECEPTACLE", 1, refs=["C1"])]})
    out = _build(boq, rows, _sheet([_word("NOTE", 1.0, 1.0)]))
    stored = out.to_dict()
    assert E.money_keys(stored) == []
    assert all("price_chip" not in ln for ln in stored["lines"])
    priced = [_priced("B1")]
    view = E.attach_price_chips(stored, priced)
    chips = {ln["line_id"]: ln["price_chip"]["kind"] for ln in view["lines"]}
    assert chips == {"B1": "library", "B2": "missing"}
    assert all(c["axis"] == "price" and "value" not in c["params"] for c in (ln["price_chip"] for ln in view["lines"]))
    assert "price_chip" not in stored["lines"][0]  # the stored dict is untouched


def test_every_7154_boq_line_gets_a_quantity_chip_and_no_money():
    fake = _fixture_inputs()
    out = E.build(None, "ff8fad25", main_version=1, boq_version=1, inputs=fake, sheets=UnavailableEvidenceInputs())
    assert len(out.lines) == 41 == out.stats["lines_with_qty_chip"]
    assert all(ln["qty_chip"]["kind"] in E.QTY_CHIP_KINDS for ln in out.lines)
    kinds = Counter(ln["qty_chip"]["kind"] for ln in out.lines)
    assert kinds["unit_error"] == 1  # the lighting track row in EA (linear_in_ea)
    assert kinds["unsourced"] == 40  # legacy lines have no source refs
    assert out.stats["provenance_completeness"] == 0.0
    assert E.money_keys(out.to_dict()) == []
    assert "no measure output: FT lines have no polylines" not in out.notes  # 7154 v1 has no FT line


# Storage, determinism, replay -------------------------------------------------------


def test_upsert_by_versions_inputs_hash_skip_and_determinism():
    rows = [_row("C1", "L1 TROFFER", 2)]
    words = [_word("L1", 5.0, 5.0), _word("L1", 9.0, 5.0)]
    boq = _boq({"LIGHTING FIXTURES": [_line("B1", "L1, 2X4 TROFFER", 2, refs=["C1"])]})
    writer = E.MemoryEvidenceWriter()
    first = _build(boq, rows, _sheet(words), writer=writer)
    assert writer.writes == 1 and not first.reused
    again = _build(boq, rows, _sheet(words), writer=writer)
    assert again.reused and writer.writes == 1 and again.inputs_hash == first.inputs_hash
    boq2 = _boq({"LIGHTING FIXTURES": [_line("B1", "L1, 2X4 TROFFER", 3, refs=["C1"])]})
    changed = _build(boq2, rows, _sheet(words), writer=writer)
    assert not changed.reused and writer.writes == 2 and len(writer.rows) == 1  # same key, updated
    assert writer.rows[0]["version"] == 1 and writer.rows[0]["output"]["inputs_hash"] == changed.inputs_hash
    fake = E.FakeRunInputs(documents=DOCS, boq=boq, boq_version=2, categories=[_category(rows)])
    E.build(None, "run-1", main_version=1, boq_version=2, inputs=fake,
            sheets=FakeEvidenceInputs({(DOC, PAGE): _sheet(words)}), writer=writer)
    assert [r["version"] for r in writer.rows] == [1, 2]
    a = _build(boq, rows, _sheet(words)).to_dict()
    b = _build(boq, rows, _sheet(words)).to_dict()
    a.pop("built_at")
    b.pop("built_at")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


class _Query:
    def __init__(self, client: "_FakeSb", table: str) -> None:
        self.client, self.table, self.op, self.payload, self.filters = client, table, "select", None, []

    def select(self, _cols: str) -> "_Query":
        self.op = "select"
        return self

    def insert(self, payload: dict) -> "_Query":
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload: dict) -> "_Query":
        self.op, self.payload = "update", payload
        return self

    def eq(self, k: str, v: Any) -> "_Query":
        self.filters.append((k, v))
        return self

    def execute(self) -> Any:
        self.client.calls.append((self.table, self.op, self.payload, list(self.filters)))
        if self.client.fail:
            raise RuntimeError("relation does not exist")
        rows = self.client.rows.setdefault(self.table, [])
        if self.op == "insert":
            row = {"id": f"id{len(rows) + 1}", **self.payload}
            rows.append(row)
            return type("R", (), {"data": [row]})()
        match = [r for r in rows if all(r.get(k) == v for k, v in self.filters)]
        if self.op == "update":
            for r in match:
                r.update(self.payload)
            return type("R", (), {"data": match})()
        return type("R", (), {"data": [dict(r) for r in match]})()


class _FakeSb:
    def __init__(self, fail: bool = False) -> None:
        self.rows: dict[str, list[dict]] = {}
        self.calls: list[tuple] = []
        self.fail = fail

    def table(self, name: str) -> _Query:
        return _Query(self, name)


def test_supabase_writer_omits_unset_columns_and_updates_in_place():
    sb = _FakeSb()
    w = E.SupabaseEvidenceWriter()
    out = {"main_version": 2, "boq_version": 3, "inputs_hash": "h1"}
    row = w.write(sb, "run-1", out)
    assert set(sb.calls[-1][2]) == {"run_id", "scope", "version", "output"}
    assert row["scope"] == "evidence" and row["version"] == 1
    w.write(sb, "run-1", {**out, "inputs_hash": "h2"})
    assert sb.calls[-1][1] == "update" and len(sb.rows["plans_reader_outputs"]) == 1
    assert w.find(sb, "run-1", 2, 3)["output"]["inputs_hash"] == "h2"
    assert w.find(sb, "run-1", 2, 4) is None
    assert w.find(_FakeSb(fail=True), "run-1", 2, 3) is None  # before migration 0165
    ri = E.SupabaseRunInputs(_FakeSb(fail=True), "run-1")
    assert ri.documents() == [] and ri.measure_output() is None and ri.boq_spec(1, None) == (1, None)


def test_build_with_a_client_stores_through_the_default_writer():
    sb = _FakeSb()
    rows = [_row("C1", "DUPLEX RECEPTACLE", 1, [(0.2, 0.2)])]
    boq = _boq({"OTHER ITEMS": [_line("B1", "DUPLEX RECEPTACLE", 1, refs=["C1"])]})
    fake = E.FakeRunInputs(documents=DOCS, boq=boq, categories=[_category(rows)])
    beats: list[int] = []
    out = E.build(sb, {"id": "run-9"}, main_version=1, boq_version=1, inputs=fake,
                  sheets=FakeEvidenceInputs({(DOC, PAGE): _sheet([_word("NOTE", 1, 1)])}),
                  heartbeat=lambda: beats.append(1))
    stored = sb.rows["plans_reader_outputs"]
    assert len(stored) == 1 and stored[0]["output"]["inputs_hash"] == out.inputs_hash
    assert stored[0]["run_id"] == "run-9" and beats


def test_sheet_bytes_are_capped():
    rows = [_row("C1", "DUPLEX RECEPTACLE", 300, [(0.001 + 0.003 * i, 0.5) for i in range(300)])]
    boq = _boq({"OTHER ITEMS": [_line("B1", "DUPLEX RECEPTACLE", 300, refs=["C1"])]})
    fake = E.FakeRunInputs(documents=DOCS, boq=boq, categories=[_category(rows)])
    out = E.build(None, "r", main_version=1, boq_version=1, inputs=fake,
                  sheets=FakeEvidenceInputs({(DOC, PAGE): _sheet([_word("NOTE", 1, 1)])}), max_sheet_bytes=20_000)
    assert out.sheets[0]["truncated"] is True and out.stats["truncated"] is True
    assert len(json.dumps(out.sheets[0], sort_keys=True, separators=(",", ":"))) <= 20_000


def _fixture_inputs() -> E.FakeRunInputs:
    docs = json.loads((FIX / "documents.json").read_text())
    cats = [json.loads((FIX / f"outputs/category_{c}_v1.json").read_text())
            for c in ("electrical_drawings", "rfp", "specifications")]
    return E.FakeRunInputs(
        documents=docs,
        main=json.loads((FIX / "outputs/main_none_v1.json").read_text()),
        boq=json.loads((FIX / "deliverables/boq_v1.json").read_text()),
        categories=cats,
        marked=json.loads((FIX / "deliverables/marked_plans_v1.json").read_text()),
    )


def test_replay_stage_registers_and_builds_from_bundle_artifacts():
    fake = _fixture_inputs()

    class _Docs:
        def available(self, _doc_id: str) -> bool:
            return False

    class _Bundle:
        run_id = "ff8fad25"

    class _Ctx:
        docs = _Docs()
        bundle = _Bundle()

    art = {
        "documents": fake.documents(),
        "main": fake.main_output(1)[1],
        "boq_spec": fake.boq_spec(1, None)[1],
        "marked_plans_spec": fake.model_marks(None),
        "category:electrical_drawings": fake.category_outputs()[0],
        "deliverable_versions": {"boq": 1},
    }
    previous = RP._STAGES.get("evidence")
    try:
        spec = E.register_replay_stage()
        assert spec.owner == "WS8" and not spec.legacy and RP.stage("evidence").fn is E._replay_evidence
        assert "measure" in spec.reads
        produced = RP.stage("evidence").fn(_Ctx(), art)
        ev = produced["evidence"]
        assert ev["schema"] == E.EVIDENCE_SCHEMA and len(ev["lines"]) == 41
        assert "no measure output: FT lines have no polylines" in ev["notes"] or ev["stats"].get("ft_lines", 0) == 0
        # A legacy registration never replaces the real one.
        RP.register_stage("evidence", lambda c, a: {}, version="legacy-9", legacy=True)
        assert RP.stage("evidence").owner == "WS8"
    finally:
        # Restore only this stage: reset_stages() would also drop the stages
        # other workstreams register at import.
        if previous is None:
            RP._STAGES.pop("evidence", None)
        else:
            RP._STAGES["evidence"] = previous
    assert RP._STAGES.get("evidence") is previous


def test_replay_reads_the_derive_stage_measure_artifact():
    class _Obj:
        def to_dict(self) -> dict:
            return {"version": 1, "quantities": []}

    m = _measure()
    assert E._replay_measure({"measure": m}) == m
    assert E._replay_measure({"measure": m, "measurements": {"version": 1}}) == m
    assert E._replay_measure({"measurements": m}) == m
    assert E._replay_measure({"measure": _Obj()}) == {"version": 1, "quantities": []}
    assert E._replay_measure({}) is None


def test_marked_spec_from_evidence_is_renderer_shaped():
    rows = [_row("C1", "L1 TROFFER", 3, [(0.5, 0.5), (0.5, 0.5), (0.5, 0.5)])]
    words = [_word("L1", 5.0, 5.0)]
    boq = _boq({"LIGHTING FIXTURES": [_line("B1", "L1, 2X4 TROFFER", 3, refs=["C1"])]})
    out = _build(boq, rows, _sheet(words))
    spec = E.evidence_to_marked_spec(out)
    s = spec["sheets"][0]
    assert s["doc_index"] == 1 and s["page"] == PAGE and s["sheet_number"] == "E3.01" and s["area"] == "Synthetic Area"
    row = s["legend"][0]
    assert row["prefix"] == "LF, L1" and row["quantity"] == 3 and row["marked"] == 2 and row["line_id"] == "B1"
    assert row["chip_text"] == "Counted 3, marked 2, 1 not located"
    styles = sorted(m["style"] for m in s["marks"])
    assert styles == ["ring", "solid"] and {m["kind"] for m in s["marks"]} == {"dot"}
    assert any(m["label"] == "x2" for m in s["marks"])  # a merged stack prints its count


def test_module_has_no_dashes():
    for path in (Path(E.__file__), Path(__file__)):
        text = path.read_text(encoding="utf-8")
        assert chr(0x2013) not in text and chr(0x2014) not in text


# Golden 7154 (the backup on disk; $0) -----------------------------------------------


def _golden_inputs() -> tuple[E.FakeRunInputs, LocalPdfInputs]:
    sheets = LocalPdfInputs({ELEC_ID: ELEC_PDF.read_bytes(), MECH_ID: MECH_PDF.read_bytes()})
    return _fixture_inputs(), sheets


@pytest.fixture(scope="module")
def evidence_7154() -> E.EvidenceOutput:
    if not (ELEC_PDF.exists() and MECH_PDF.exists()):
        pytest.skip("7154 backup not on this machine")
    fake, sheets = _golden_inputs()
    return E.build(None, "ff8fad25", main_version=1, boq_version=1, inputs=fake, sheets=sheets)


@golden
@needs_backup
def test_7154_e301_anchors_registration_and_fixture_marks(evidence_7154: E.EvidenceOutput):
    """E3.01 (ELEC page 10): anchors L1 21, L2 1, L3 16, L4 3, X1 2, L5 7;
    the stored model dots register with sy in [1.15, 1.30] and a median
    residual <= 1.5 ft (WS8 design 6.4); the 50 fixture marks sit at
    anchors (L1 16 own tag, L1 life safety 5 assigned)."""
    e301 = [s for s in evidence_7154.sheets if s["sheet_number"] == "E3.01"][0]
    counts = {k: e301["anchor_counts"][k] for k in ("L1", "L2", "L3", "L4", "L5", "X1")}
    assert counts == {"L1": 21, "L2": 1, "L3": 16, "L4": 3, "L5": 7, "X1": 2}
    reg = e301["registration"]
    assert reg["ok"] and 1.15 <= reg["sy"] <= 1.30 and reg["median_residual_ft"] <= 1.5
    lines = {ln["description"]: ln for ln in evidence_7154.lines}
    assert lines["L1, 2X4 RECESSED LED TROFFER"]["marks_by_method"] == {"anchor": 16}
    assert lines["L1, 2X4 RECESSED LED TROFFER, LIFE SAFETY"]["marks_by_method"] == {"assigned": 5}
    fixture_marks = [m for m in e301["marks"] if m["kind"] == "dot" and m["method"] in ("anchor", "assigned")]
    assert len(fixture_marks) == 50
    assert e301["scale"]["ft_per_in"] == 4.0


# Ground truth: the estimator's flattened Marked Plans.pdf --------------------------

GT_DPI = 100
GT_ID_DPI = 50
GT_WITHIN_FT = 1.5
GT_TARGET = 0.85


def _sheet_rgb(page: Any, dpi: int, np: Any) -> Any:
    """The page image cropped to the union of its image objects (the sheet
    as the estimator's tool flattened it)."""
    import pypdfium2 as pdfium

    width, height = page.get_size()
    boxes = [o.get_bounds() for o in page.get_objects() if o.type == pdfium.raw.FPDF_PAGEOBJ_IMAGE]
    x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    s = dpi / 72
    rgb = np.asarray(page.render(scale=s).to_pil().convert("RGB"), dtype=np.float32) / 255.0
    top, bot = int(round((height - y1) * s)), int(round((height - y0) * s))
    return rgb[top:bot, int(round(x0 * s)):int(round(x1 * s))]


def _identify(crop: Any, sources: list[tuple[tuple[str, int], Any]], np: Any) -> tuple[float, tuple[str, int], int, int]:
    """Register a flattened page to its source sheet: the source page and
    pixel offset (+/- 4 px at GT_ID_DPI) with the best Pearson correlation of
    ink."""
    a = 1.0 - crop.mean(axis=2)
    best: tuple[float, tuple[str, int], int, int] | None = None
    for key, page in sources:
        sw, _sh = page.get_size()
        src = 1.0 - np.asarray(page.render(scale=a.shape[1] / sw).to_pil().convert("L"), dtype=np.float32) / 255.0
        h, w = min(src.shape[0], a.shape[0]), min(src.shape[1], a.shape[1])
        for dy in range(-4, 5, 2):
            for dx in range(-4, 5, 2):
                aa = a[max(0, dy):h + min(0, dy), max(0, dx):w + min(0, dx)]
                bb = src[max(0, -dy):h + min(0, -dy), max(0, -dx):w + min(0, -dx)]
                aa, bb = aa - aa.mean(), bb - bb.mean()
                score = float((aa * bb).sum() / max(1e-9, math.sqrt(float((aa * aa).sum()) * float((bb * bb).sum()))))
                if best is None or score > best[0]:
                    best = (score, key, dx, dy)
    assert best is not None
    return best


def _detect_dots(rgb: Any, np: Any) -> list[tuple[float, float]]:
    """Translucent colored dots: saturated pixels split by hue bin into
    connected components; the single-dot area is the median of round
    components; a blob of k dot areas is split by greedy disk cover. Small
    legend swatches, text and route lines fail the size or fill tests."""
    mx, mn = rgb.max(axis=2), rgb.min(axis=2)
    sat = (mx - mn) / np.maximum(mx, 1e-6)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    d = np.maximum(mx - mn, 1e-6)
    hue = np.where(mx == r, ((g - b) / d) % 6, np.where(mx == g, (b - r) / d + 2, (r - g) / d + 4)) * 60
    mask = (sat > 0.18) & (mx > 0.25)
    nb = 18
    label = (hue // (360 / nb)).astype(int) % nb
    h, w = mask.shape
    seen = np.zeros_like(mask)
    comps = []
    for y, x in zip(*[a.tolist() for a in np.nonzero(mask)], strict=True):
        if seen[y, x]:
            continue
        bin0 = int(label[y, x])
        q = deque([(y, x)])
        seen[y, x] = True
        pts = []
        while q:
            cy, cx = q.popleft()
            pts.append((cy, cx))
            for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    if abs(((int(label[ny, nx]) - bin0 + nb // 2) % nb) - nb // 2) <= 1:
                        seen[ny, nx] = True
                        q.append((ny, nx))
        if len(pts) >= 20:
            comps.append(np.array(pts))
    info = []
    for c in comps:
        (y0, x0), (y1, x1) = c.min(axis=0), c.max(axis=0)
        bw, bh = x1 - x0 + 1, y1 - y0 + 1
        info.append((c, bw, bh, len(c) / (bw * bh)))
    singles = [len(c) for c, bw, bh, f in info if 14 <= bw <= 40 and 14 <= bh <= 40 and abs(bw - bh) <= 3 and f > 0.7]
    if not singles:
        return []
    a1 = float(np.median(singles))
    rad = math.sqrt(a1 / math.pi)
    dots: list[tuple[float, float]] = []
    for c, bw, bh, f in info:
        if max(bw, bh) < 1.4 * rad or min(bw, bh) < rad or f < 0.4:
            continue
        k = max(1, int(round(len(c) / a1)))
        if k == 1:
            dots.append((float(c[:, 1].mean()) / w, float(c[:, 0].mean()) / h))
            continue
        rem = c.astype(np.float32)
        for _ in range(k):
            if len(rem) < 0.4 * a1:
                break
            cand = rem[:: max(1, len(rem) // 400)]
            d2 = ((cand[:, None, :] - rem[None, :, :]) ** 2).sum(axis=2)
            ctr = cand[int((d2 <= rad * rad).sum(axis=1).argmax())]
            inside = ((rem - ctr) ** 2).sum(axis=1) <= rad * rad
            pts = rem[inside]
            dots.append((float(pts[:, 1].mean()) / w, float(pts[:, 0].mean()) / h))
            rem = rem[~inside]
    return dots


@pytest.fixture(scope="module")
def mark_accuracy(evidence_7154: E.EvidenceOutput) -> dict[str, Any]:
    np = pytest.importorskip("numpy")
    import pypdfium2 as pdfium

    marked = pdfium.PdfDocument(str(MARKED_PDF))
    elec = pdfium.PdfDocument(str(ELEC_PDF))
    mech = pdfium.PdfDocument(str(MECH_PDF))
    ids = {"elec": ELEC_ID, "mech": MECH_ID}
    sources = [(("elec", i + 1), elec[i]) for i in range(len(elec))] + [(("mech", i + 1), mech[i]) for i in range(len(mech))]
    per_sheet = []
    totals = Counter()
    by_method: dict[str, list[int]] = {}
    for i in range(len(marked)):
        score, (doc, page), dx, dy = _identify(_sheet_rgb(marked[i], GT_ID_DPI, np), sources, np)
        rgb = _sheet_rgb(marked[i], GT_DPI, np)
        # Offsets are in GT_ID_DPI pixels of the sheet image; apply as fractions.
        fx, fy = dx / (rgb.shape[1] * GT_ID_DPI / GT_DPI), dy / (rgb.shape[0] * GT_ID_DPI / GT_DPI)
        gt = [(x - fx, y - fy) for x, y in _detect_dots(rgb, np)]
        did = ids[doc]
        sheets = [s for s in evidence_7154.sheets if s["document_id"] == did and s["page"] == page]
        w_in = sheets[0]["w_in"] if sheets else 42.0
        h_in = sheets[0]["h_in"] if sheets else 30.0
        ft_per_in = (sheets[0]["scale"]["ft_per_in"] if sheets else None) or 4.0
        ours = [m for s in sheets for m in s["marks"] if m["kind"] == "dot" and m["method"] != "unclaimed"]

        def ft(a: tuple[float, float], b: tuple[float, float]) -> float:
            return math.hypot((a[0] - b[0]) * w_in, (a[1] - b[1]) * h_in) * ft_per_in

        hit_p = 0
        for m in ours:
            p = tuple(m["points"][0])
            ok = bool(gt) and min(ft(p, g) for g in gt) <= GT_WITHIN_FT
            hit_p += ok
            slot = by_method.setdefault(m["method"], [0, 0])
            slot[0] += int(ok)
            slot[1] += 1
        hit_r = sum(1 for g in gt if ours and min(ft(tuple(m["points"][0]), g) for m in ours) <= GT_WITHIN_FT)
        per_sheet.append({"marked_page": i + 1, "source": f"{doc} p{page}", "corr": round(score, 3),
                          "sheet": sheets[0]["sheet_number"] if sheets else None, "gt": len(gt), "ours": len(ours),
                          "precise": hit_p, "recalled": hit_r})
        totals.update({"gt": len(gt), "ours": len(ours), "precise": hit_p, "recalled": hit_r})
    result = {
        "per_sheet": per_sheet,
        "precision": round(totals["precise"] / totals["ours"], 4) if totals["ours"] else None,
        "recall": round(totals["recalled"] / totals["gt"], 4) if totals["gt"] else None,
        "by_method": {k: {"precise": v[0], "marks": v[1]} for k, v in sorted(by_method.items())},
        "totals": dict(totals),
    }
    print("7154 ground-truth mark accuracy:", json.dumps(result, indent=1))
    return result


@golden
@needs_backup
def test_7154_ground_truth_detector_registration_and_measured_floor(mark_accuracy: dict[str, Any]):
    """The detector and registration work (each flattened page maps to its
    source sheet; 5 thermostat dots on MD2.01) and the measured values do
    not regress below what wave 2 measured (ED2.01 and E3.01 recall, anchor
    and snapped precision)."""
    pages = {p["marked_page"]: p for p in mark_accuracy["per_sheet"]}
    assert [pages[i]["source"] for i in range(1, 8)] == [
        "elec p6", "elec p7", "elec p8", "elec p9", "elec p10", "mech p5", "mech p7",
    ]
    assert pages[6]["gt"] == 5 and pages[1]["gt"] == 0 and pages[7]["gt"] == 0
    assert pages[2]["recalled"] >= 0.9 * pages[2]["gt"] and pages[2]["precise"] >= 0.9 * pages[2]["ours"]
    assert pages[5]["recalled"] >= 0.9 * pages[5]["gt"]
    m = mark_accuracy["by_method"]
    assert m["snapped"]["precise"] == m["snapped"]["marks"]
    assert m["anchor"]["precise"] >= 0.75 * m["anchor"]["marks"]
    # Re-measured 2026-10-22 after the document-prior registration (E2.01
    # borrows E3.01's tag-aware fit) and the duplicate stacking near held
    # anchors: precision 125 / 164 = 0.762, recall 121 / 183 = 0.661 (wave 2:
    # 0.633 and 0.568). E2.01 went from 1 to 20 precise of 30.
    assert pages[4]["precise"] >= 18 and mark_accuracy["by_method"]["registered"]["precise"] >= 18
    assert mark_accuracy["precision"] >= 0.75 and mark_accuracy["recall"] >= 0.65


@golden
@needs_backup
@pytest.mark.xfail(strict=True, reason=(
    "WS8 acceptance target (precision and recall >= 0.85 within 1.5 ft) not met on the stored v1 run "
    "(2026-10-22: precision 0.762, recall 0.661): E2.01 lines without model points (41 devices) cannot be "
    "placed, coarse model points stay 1.5 to 2.9 ft off after registration, and ED3.01 (X) tags sit at "
    "fixture corners about 1.9 ft from the symbol centers the estimator dotted (needs fixture geometry)"
))
def test_7154_ground_truth_mark_precision_and_recall_meet_the_target(mark_accuracy: dict[str, Any]):
    assert mark_accuracy["precision"] >= GT_TARGET
    assert mark_accuracy["recall"] >= GT_TARGET


# Row lookup through the session's collision aliases (V1 hook 2) ----------------------------------


def test_a_line_citing_a_session_id_renumbered_after_a_collision_is_placed_from_its_row():
    """Two stored outputs merged from 1 (every category of the 7172 run):
    the general C1 and the electrical C1 collide. The main session indexes
    the outputs in vocabulary order (general first), keeps the general C1
    and re-numbers the electrical row to C2 (index_sources, original_row_id
    C1), so the BOQ line cites C2. Evidence builds the same index (whatever
    order the run inputs list the outputs in), so the line is placed from
    its row's points instead of falling back to the legacy placement."""
    spots = _grid_l1(6)
    words = [_word("L1", x, y) for x, y in spots]
    centers = [_center("L1", x, y) for x, y in spots]
    elec = _category([_row("C1", "L1 TROFFER", 6, centers)])
    general = dict(_category([]), category="general_drawings", counts=[{
        "row_id": "C1", "description": "Fire extinguisher cabinet", "quantity": 3, "unit": "EA",
        "sheet": {"document_id": "d2", "page": 1, "sheet_number": "G0.01", "sheet_key": "D2P1"},
        "positions": [], "coverage": "sheet_total", "status": "active",
    }])
    boq = _boq({"LIGHTING FIXTURES": [_line("B1", "L1, 2X4 TROFFER", 6, refs=["C2"])]})
    sheets = FakeEvidenceInputs({(DOC, PAGE): _sheet(words)})
    # the run inputs list electrical first (SupabaseRunInputs sorts by name); the index still ranks general first
    fake = E.FakeRunInputs(documents=DOCS, boq=boq, categories=[elec, general])
    out = E.build(None, "run-1", main_version=1, boq_version=1, inputs=fake, sheets=sheets)
    b1 = _marks(out, "B1")
    assert len(b1) == 6 and {m["method"] for m in b1} == {"anchor"}
    lines = {ln["line_id"]: ln for ln in out.lines}
    assert lines["B1"]["qty_chip"]["kind"] == "counted" and lines["B1"]["not_located"] == 0
    assert any("1 category rows re-numbered after row id collisions" in n for n in out.notes)
    # the same build with the general output handed first gives the same placement
    swapped = E.build(None, "run-1", main_version=1, boq_version=1, sheets=sheets,
                      inputs=E.FakeRunInputs(documents=DOCS, boq=boq, categories=[general, elec]))
    assert [m["points"] for m in _marks(swapped, "B1")] == [m["points"] for m in b1]
    # without the collision the index is the plain one and nothing is noted
    plain = _build(boq, [_row("C2", "L1 TROFFER", 6, centers)], _sheet(words))
    assert len(_marks(plain, "B1")) == 6 and not any("re-numbered" in n for n in plain.notes)

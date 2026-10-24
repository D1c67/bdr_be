"""pagefacts.titleblock, scales, roles, category, regions and extract on
synthetic PDFs, plus the golden-local 7154 checks PF1 to PF4 on the backup
ELEC and MECH sets (marked `golden`; skipped when the backup is absent).

The 7154 fixture `tests/fixtures/plans_reader/run_7154/page_facts_elec.json`
(final schema, no dollars) is regenerated with
`uv run python -m tests.test_pagefacts_titleblock_scales_roles --regen`
and the golden test asserts a fresh extraction still matches it.

No network, no database, no model.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pytest

from app.services.pagefacts import (
    category,
    contracts,
    extract,
    regions,
    roles,
    scales,
    store,
    titleblock,
)
from app.services.pagefacts.contracts import DocFacts
from tests.pagefacts_fixtures import Shx, make_pdf, plan_sheet, sheet, title_block_texts

BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
PROJECT_DIR = BACKUP / "storage/project-files/7dfc64e6-36ef-4c67-9d02-0d7942015011"
ELEC_PDF = (
    PROJECT_DIR
    / "drawing/c85a4720840d49a288d9d0c7d3c87fed-Reference Material - Architectural - ELEC_RESPONSE_TO_CLV_COMMENTS D1.pdf"
)
MECH_PDF = (
    PROJECT_DIR
    / "drawing/9466b0a7b03b42c4bd9bf8feac10c9e0-Reference Material - Architectural - MECH_RESPONSE_TO_CLV_COMMENTS D1.pdf"
)
FIXTURE = Path(__file__).parent / "fixtures/plans_reader/run_7154/page_facts_elec.json"

golden = pytest.mark.golden
needs_backup = pytest.mark.skipif(
    not ELEC_PDF.exists() or not MECH_PDF.exists(), reason="7154 backup PDFs not on this machine"
)

BUDGETS = {"page_budget_s": 4.0, "doc_budget_s": 240.0}


def _extract(pdf: bytes, label: str = "electrical_drawings", **kw) -> DocFacts:
    return extract.extract_document(pdf, label=label, **BUDGETS, **kw)


# Title block ----------------------------------------------------------------------


def test_slot_learning_rejects_a_repeating_stamp():
    """Four sheets with distinct numbers at one spot and a stamp ("R25") at
    another: the slot is the numbers, the stamp fails the distinctness rule."""
    sheets = []
    for i in range(1, 5):
        extra = [(2.0, 1.0, 20, "R25"), (2.0, 1.6, 20, "NV5")]
        sheets.append(plan_sheet(f"E1.0{i}", f"PLAN AREA {i}", extra=extra))
    df = _extract(make_pdf(sheets))
    assert [p.sheet_number for p in df.pages] == ["E1.01", "E1.02", "E1.03", "E1.04"]
    assert all(p.sheet_number_source == "slot" for p in df.pages)
    assert len(df.title_block_slots) == 1
    slot = df.title_block_slots[0]
    assert (
        slot.x > 0.9
        and slot.y > 0.9
        and slot.cover == 1.0
        and slot.distinct == 1.0
        and slot.pages == [1, 2, 3, 4]
    )
    assert df.problems == []


def test_label_anchor_fallback_on_a_single_page():
    pdf = make_pdf(
        [
            sheet(
                (30.0, 28.0, 7, "SHEET NO."),
                (30.0, 29.0, 24, "E-101"),
                (2.0, 2.0, 10, "R25"),
                (3.0, 3.0, 10, "SOME PLAN NOTES"),
            )
        ]
    )
    df = _extract(pdf)
    page = df.pages[0]
    assert (page.sheet_number, page.sheet_number_source) == ("E-101", "label")


def test_single_page_corner_guess_is_reported_as_corner_not_slot():
    """One page, no SHEET NO. label, no slot to learn: the tallest sheet-like
    token in the bottom-right corner is a heuristic guess and says so."""
    pdf = make_pdf(
        [
            sheet(
                (39.5, 29.2, 36, "E-301"),
                (2.0, 2.0, 10, "R25"),
                (3.0, 3.0, 10, "SOME PLAN NOTES"),
            )
        ]
    )
    df = _extract(pdf)
    page = df.pages[0]
    assert (page.sheet_number, page.sheet_number_source) == ("E-301", "corner")
    assert "corner" in contracts.SOURCE_KINDS and df.title_block_slots == []
    # Two such pages learn a slot instead, and the slot is what gets reported.
    two = _extract(
        make_pdf(
            [
                sheet((39.5, 29.2, 36, "E-301"), (2.0, 2.0, 10, "R25")),
                sheet((39.5, 29.2, 36, "E-302"), (2.0, 2.0, 10, "R25")),
            ]
        )
    )
    assert [(p.sheet_number, p.sheet_number_source) for p in two.pages] == [
        ("E-301", "slot"),
        ("E-302", "slot"),
    ]


def test_sheet_number_is_never_invented():
    pdf = make_pdf(
        [
            sheet(
                (3.0, 3.0, 10, "GENERAL NOTES"),
                (3.0, 3.4, 8, "1. DO THE WORK."),
                (3.0, 3.8, 8, "2. DO IT WELL."),
            )
        ]
    )
    df = _extract(pdf)
    assert df.pages[0].sheet_number is None and df.pages[0].sheet_number_source == "none"
    assert df.pages[0].sheet_title is None
    # The splitter title is the last resort, and only its leading token.
    df2 = _extract(pdf, splitter_titles={1: "E0.01 ELECTRICAL NOTES"})
    assert (df2.pages[0].sheet_number, df2.pages[0].sheet_number_source) == ("E0.01", "splitter")
    assert (df2.pages[0].sheet_title, df2.pages[0].title_source) == (
        "E0.01 ELECTRICAL NOTES",
        "splitter",
    )
    assert titleblock.splitter_sheet_number("POWER PLAN") is None
    assert titleblock.splitter_sheet_number("e2.01 power plan") == "E2.01"
    assert (
        not titleblock.is_sheet_token("NO")
        and not titleblock.is_sheet_token("ABC")
        and titleblock.is_sheet_token("ED3.01")
    )


def test_shx_sheet_number_fallback():
    pdf = make_pdf(
        [
            sheet(
                (3.0, 3.0, 10, "VECTOR SHEET WITH ONLY AN SHX NUMBER"),
                shx=[Shx(38.0, 28.5, 1.0, 0.4, "SHEET NO."), Shx(38.0, 29.0, 2.0, 0.6, "E-201")],
            )
        ]
    )
    df = _extract(pdf)
    assert (df.pages[0].sheet_number, df.pages[0].sheet_number_source) == ("E-201", "shx")


def test_title_joins_two_lines_and_strips_boilerplate():
    texts = title_block_texts(
        "E0.04", "LIGHTING FIXTURE SCHEDULE", title2="AND COMPLIANCE CERTIFICATE"
    )
    texts += [(20.0, 5.0, 12, "LUMINAIRE SCHEDULE")]
    texts += [
        (20.0, 6.0 + i * 0.3, 7, f"L{i} 2X4 LED TROFFER 277 35 0-10V 120 40 1 2 3")
        for i in range(1, 25)
    ]
    df = _extract(make_pdf([sheet(*texts)]))
    page = df.pages[0]
    assert page.sheet_number == "E0.04"
    assert page.sheet_title == "LIGHTING FIXTURE SCHEDULE AND COMPLIANCE CERTIFICATE"
    assert (
        page.title_source == page.sheet_number_source == "label"
    )  # one page: the SHEET NO. label anchors it
    assert "SHEET" not in page.sheet_title and "KEY PLAN" not in page.sheet_title
    assert page.page_role == "schedule" and "schedule" in page.roles
    tb = page.region("title_block")
    assert tb is not None and tb.bbox[0] > 0.85 and page.region("plan_area").bbox[2] == tb.bbox[0]


def test_revision_latest_date_wins():
    texts = title_block_texts("E2.01", "POWER PLAN")
    x = 42.0 - 3.5
    texts += [(x, 22.0, 7, "1 Response to CLV 01/15/2026"), (x, 22.3, 7, "2 Permit set 06/15/2026")]
    df = _extract(make_pdf([sheet(*texts)]))
    rev = df.pages[0].revision
    assert rev is not None and (rev.number, rev.description, rev.date) == (
        "2",
        "Permit set",
        "06/15/2026",
    )


# Scales ---------------------------------------------------------------------------


def test_scale_view_bubble_is_not_a_whole_inch():
    """A view-number bubble "1" (18 pt) before "1/4" = 1'-0"" (7 pt) is not
    "1 1/4""; the same-size "1 1/2" = 1'-0"" is 1.5 in per ft."""
    one = plan_sheet("E2.01", "OVERALL POWER PLAN", '1/4" = 1\'-0"', bubble="1")
    half = plan_sheet("E2.02", "ENLARGED POWER PLAN", '1 1/2" = 1\'-0"', bubble=None)
    df = _extract(make_pdf([one, half]))
    p1, p2 = df.pages
    assert (
        p1.primary_ft_per_in == 4.0
        and p1.views[0].scale_text == '1/4" = 1\'-0"'
        and p1.views[0].number == "1"
    )
    assert p1.views[0].title == "OVERALL POWER PLAN" and p1.views[0].kind == "plan"
    assert (
        p2.primary_ft_per_in == 0.6667
        and p2.views[0].scale_text == '1 1/2" = 1\'-0"'
        and p2.views[0].kind == "enlarged_plan"
    )
    assert not p1.scale_conflict and not p2.scale_conflict
    assert p1.views[0].region == p1.region("plan_area").bbox  # one plan view: region = plan area


def test_scale_legend_example_is_flagged_and_ignored():
    texts = [
        (2.0, 2.0, 14, "VIEW KEY"),
        (2.0, 3.0, 18, "VIEW NAME"),
        (2.0, 3.3, 7, '1/8" = 1\'-0"'),
        (15.0, 28.6, 18, "OVERALL POWER PLAN"),
        (15.0, 29.0, 7, '1/4" = 1\'-0"'),
    ] + title_block_texts("E0.00", "ELECTRICAL COVERSHEET")
    df = _extract(make_pdf([sheet(*texts)]))
    page = df.pages[0]
    kinds = {(v.scale_text, v.example) for v in page.views}
    assert ('1/8" = 1\'-0"', True) in kinds and ('1/4" = 1\'-0"', False) in kinds
    assert page.primary_ft_per_in == 4.0 and not page.scale_conflict
    assert any("legend example" in n for n in page.scale_notes)
    only_example = _extract(
        make_pdf([sheet((2.0, 3.0, 18, "VIEW NAME"), (2.0, 3.3, 7, '1/8" = 1\'-0"'))])
    )
    assert only_example.pages[0].primary_ft_per_in is None


def test_scale_conflict_between_two_plan_views():
    texts = [
        (6.0, 20.0, 18, "OVERALL POWER PLAN"),
        (6.0, 20.4, 7, '1/8" = 1\'-0"'),
        (26.0, 20.0, 14, "ENLARGED PHARMACY PLAN"),
        (26.0, 20.4, 7, '1/4" = 1\'-0"'),
    ] + title_block_texts("E2.01", "POWER PLANS")
    df = _extract(make_pdf([sheet(*texts)]))
    page = df.pages[0]
    assert page.scale_conflict is True
    assert page.primary_ft_per_in == 8.0  # the taller title wins the primary
    assert {v.ft_per_in for v in page.views} == {8.0, 4.0}
    assert all(v.region is None for v in page.views)  # consumers must not measure across views
    assert any(n.startswith("scale_conflict") for n in page.scale_notes)


def test_plan_beside_details_at_another_scale_has_no_region_and_reports_conflict():
    """No frame rectangles are detected, so one plan view owns the plan area
    only when nothing else on the sheet is drawn at another scale; a plan
    beside a differently scaled detail leaves every region None and sets
    scale_conflict (consumers must not measure across views). Details at
    the plan's own scale change nothing."""
    mixed = [
        (6.0, 20.0, 18, "OVERALL POWER PLAN"),
        (6.0, 20.4, 7, '1/8" = 1\'-0"'),
        (30.0, 20.0, 12, "TRENCH DETAIL"),
        (30.0, 20.4, 7, '1 1/2" = 1\'-0"'),
    ] + title_block_texts("E2.01", "POWER PLAN")
    page = _extract(make_pdf([sheet(*mixed)])).pages[0]
    assert {v.kind for v in page.views} == {"plan", "detail"}
    assert page.primary_ft_per_in == 8.0  # the plan view is still the page scale
    assert page.scale_conflict is True
    assert all(v.region is None for v in page.views)
    assert any(
        n.startswith("scale_conflict") and "do not measure across views" in n
        for n in page.scale_notes
    )
    same = [
        (6.0, 20.0, 18, "OVERALL POWER PLAN"),
        (6.0, 20.4, 7, '1/8" = 1\'-0"'),
        (30.0, 20.0, 12, "TRENCH DETAIL"),
        (30.0, 20.4, 7, '1/8" = 1\'-0"'),
        (30.0, 24.0, 12, "RISER DIAGRAM"),
        (30.0, 24.4, 7, "N.T.S."),
    ] + title_block_texts("E2.02", "POWER PLAN")
    page2 = _extract(make_pdf([sheet(*same)])).pages[0]
    assert page2.scale_conflict is False and page2.primary_ft_per_in == 8.0
    plan = next(v for v in page2.views if v.kind == "plan")
    assert plan.region == page2.region("plan_area").bbox


def test_engineering_and_nts_scales():
    texts = [
        (6.0, 20.0, 18, "SITE ELECTRICAL PLAN"),
        (6.0, 20.4, 7, "1\" = 30'"),
        (26.0, 20.0, 18, "ONE LINE DIAGRAM"),
        (26.0, 20.4, 7, "NO SCALE"),
    ]
    df = _extract(make_pdf([sheet(*texts)]))
    views = {v.title: v for v in df.pages[0].views}
    assert (
        views["SITE ELECTRICAL PLAN"].ft_per_in == 30.0
        and views["SITE ELECTRICAL PLAN"].kind == "plan"
    )
    assert (
        views["ONE LINE DIAGRAM"].nts
        and views["ONE LINE DIAGRAM"].ft_per_in is None
        and views["ONE LINE DIAGRAM"].kind == "diagram"
    )
    assert df.pages[0].primary_ft_per_in == 30.0
    assert scales.parse_scale_text('3/16" = 1\'-0"') == (5.3333, False)
    assert scales.parse_scale_text("N.T.S.") == (None, True)
    assert scales.parse_scale_text("nothing here") == (None, False)
    assert scales.parse_scale_text('1" = 1\'-0"') == (1.0, False)


# Roles, regions ---------------------------------------------------------------


def test_roles_are_multi_label_on_a_coversheet():
    texts = [(5.0, 1.0, 14, "ELECTRICAL SYMBOL LIST")]
    texts += [(5.0, 1.5 + i * 0.3, 7, f"REC-{i:02d} DUPLEX RECEPTACLE TYPE {i}") for i in range(12)]
    texts += [(25.0, 1.0, 14, "ELECTRICAL INSTALLATION NOTES:")]
    texts += [
        (25.0, 1.5 + i * 0.3, 7, f"{i + 1}. NOTE NUMBER {i + 1} APPLIES TO ALL WORK.")
        for i in range(8)
    ]
    texts += title_block_texts("E0.00", "ELECTRICAL COVERSHEET")
    df = _extract(make_pdf([sheet(*texts)]))
    page = df.pages[0]
    assert {"legend_symbols", "general_notes", "cover_index"} <= set(page.roles)
    assert page.page_role == "legend_symbols"
    kinds = [r.kind for r in page.regions]
    assert (
        kinds[0] == "title_block"
        and kinds[-1] == "plan_area"
        and "legend" in kinds
        and "general_notes" in kinds
    )
    legend = next(r for r in page.regions if r.kind == "legend")
    assert legend.label == "ELECTRICAL SYMBOL LIST" and legend.bbox[3] >= round(
        1.5 / 30 + 11 * 0.3 / 30, 4
    )
    assert not regions.in_plan_area(page.regions, 5.5 / 42, 2.0 / 30)  # inside the legend
    assert regions.in_plan_area(page.regions, 0.5, 0.5)
    assert not regions.in_plan_area(page.regions, 0.98, 0.5)  # title block
    assert regions.region_at(page.regions, 5.5 / 42, 2.0 / 30) == "legend"


def test_roles_plan_schedule_diagram_and_text_documents():
    plan = plan_sheet("E2.01", "OVERALL POWER PLAN - NEW WORK")
    sched = sheet(
        *(
            title_block_texts("E0.03", "PANEL SCHEDULES")
            + [
                (10.0, 3.0 + i * 0.3, 7, f"{i} 20 1 1200 540 12 12 12 {i + 1}")
                for i in range(1, 43)
            ]
        )
    )
    diagram = sheet(
        *(
            title_block_texts("E0.02", "SINGLE LINE DIAGRAMS")
            + [(10.0, 20.0, 18, "SINGLE LINE DIAGRAM"), (10.0, 20.4, 7, "NO SCALE")]
        )
    )
    df = _extract(make_pdf([plan, sched, diagram]))
    p_plan, p_sched, p_diag = df.pages
    assert p_plan.page_role == "plan" and p_plan.roles == ["plan"]
    assert p_sched.page_role == "schedule" and p_sched.role_scores["schedule"] >= 7
    assert p_diag.page_role == "diagram"
    spec = _extract(
        make_pdf([sheet((1.0, 1.0, 10, "SECTION 26 05 19 CONDUCTORS"), w_in=8.5, h_in=11)]),
        label="specifications",
    )
    assert (
        spec.kind == "text"
        and spec.pages[0].roles == ["spec_text"]
        and spec.pages[0].page_role == "spec_text"
    )
    assert (
        spec.pages[0].sheet_number is None
        and spec.category_check is None
        and spec.title_block_slots == []
    )
    assert extract.kind_for_label(None) == "drawing" and extract.kind_for_label("rfp") == "text"


def test_role_evidence_from_part_b_takes_precedence():
    pdf = make_pdf([plan_sheet("E2.01", "POWER PLAN")])
    hooks = extract.ExtractHooks(
        role_evidence=lambda ctx: roles.RoleEvidence(legend_entries=20, legend_modifiers=3)
    )
    df = _extract(pdf, hooks=hooks)
    assert "legend_symbols" in df.pages[0].roles and df.pages[0].role_scores["legend_symbols"] == 6


# Category -------------------------------------------------------------------------


def test_discipline_map_and_category_check():
    assert category.discipline_of("E2.01") == ("E", "electrical_drawings")
    assert category.discipline_of("ED3.01") == ("ED", "electrical_drawings")
    assert category.discipline_of("MD2.01") == ("MD", "mechanical_drawings")
    assert category.discipline_of("FA-101") == ("FA", "low_voltage_drawings")
    assert category.discipline_of("T1.01") == ("T", "low_voltage_drawings")
    assert category.discipline_of("TS-1") == ("TS", "electrical_drawings")
    assert category.discipline_of("ZZ9") == ("ZZ", "unknown")
    assert category.discipline_of(None) == (None, "unknown")
    docp = Counter({"BE": 4, "BT": 2})
    assert category.discipline_of("BE0-00", doc_prefixes=docp) == ("E", "electrical_drawings")
    assert category.discipline_of("L1.01") == ("L", "civil_drawings")
    assert category.discipline_of("L1.01", doc_prefixes=Counter({"E": 3, "L": 2})) == (
        "L",
        "electrical_drawings",
    )
    assert category.discipline_of("L1.01", has_luminaires=True) == ("L", "electrical_drawings")

    all_m = [("M", "mechanical_drawings")] * 8
    check = category.category_check("electrical_drawings", all_m, name="MECH.pdf")
    assert (
        check.action == "relabel"
        and check.detected == "mechanical_drawings"
        and check.share == 1.0
        and check.numbered_pages == 8
    )
    assert "MECH.pdf" in check.message and "Mechanical Drawings" in check.message
    mixed = category.category_check(
        "electrical_drawings",
        [("M", "mechanical_drawings")] * 6 + [("E", "electrical_drawings")] * 4,
    )
    assert mixed.action == "warn_mixed" and mixed.share == 0.6
    fine = category.category_check(
        "electrical_drawings", [("E", "electrical_drawings")] * 9 + [("M", "mechanical_drawings")]
    )
    assert fine.action == "none" and fine.share == 0.9
    assert (
        category.category_check("electrical_drawings", [("M", "mechanical_drawings")]).action
        == "none"
    )  # one page is not enough
    unknown_only = category.category_check("electrical_drawings", [("ZZ", "unknown")] * 5)
    assert unknown_only.numbered_pages == 0 and unknown_only.action == "none"


def test_category_check_runs_in_extract():
    sheets = [plan_sheet(f"M2.0{i}", f"MECHANICAL PLAN {i}") for i in range(1, 4)]
    df = _extract(make_pdf(sheets), label="electrical_drawings", name="MECH.pdf")
    assert df.category_check.action == "relabel" and df.category_check.by_discipline == {"M": 3}
    assert all(
        p.discipline == "M" and p.discipline_category == "mechanical_drawings" for p in df.pages
    )


# Extract orchestration ------------------------------------------------------------


def test_document_budget_skips_remaining_pages():
    ticks = iter(range(0, 10_000, 100))
    df = extract.extract_document(
        make_pdf([plan_sheet("E1.01", "PLAN ONE"), plan_sheet("E1.02", "PLAN TWO")]),
        label="electrical_drawings",
        page_budget_s=4.0,
        doc_budget_s=150.0,
        clock=lambda: float(next(ticks)),
    )
    assert df.pages[0].text_class == "cad_text" or df.pages[0].text_class == "sparse"
    assert df.pages[1].text_class == "skipped_budget" and df.pages[1].problems == [
        "skipped: document time budget spent"
    ]
    assert df.pages[1].text_layer_quality == "none" and (df.pages[1].w_in, df.pages[1].h_in) == (
        42.0,
        30.0,
    )
    assert df.stats["budget_hit"] is True and df.stats["pages"] == 2
    assert any("budget" in p for p in df.pages[0].problems)  # the page overran its own budget too


def test_hooks_plug_in_and_a_failing_hook_becomes_a_problem():
    seen: list[int] = []

    def notes(ctx):
        seen.append(ctx.page)
        return [{"kind": "sheet_notes", "items": [{"number": "1", "text": "ALL WORK PER NEC."}]}]

    def tags(ctx, doc):
        raise RuntimeError("tag parser exploded")

    hooks = extract.ExtractHooks(
        notes=notes,
        tags=tags,
        legend=lambda doc: {"entries": [{"tag": "JB"}], "modifiers": [], "abbreviations": []},
        panels=lambda doc: ([{"name": "1LI"}], [], [{"tag": "L5", "linear_hint": True}]),
        facts_block_tokens=lambda ctx, doc: 123,
    )
    df = _extract(make_pdf([plan_sheet("E2.01", "POWER PLAN")]), hooks=hooks)
    page = df.pages[0]
    assert seen == [1] and page.notes[0]["kind"] == "sheet_notes"
    assert page.tags == contracts.empty_tags()
    assert any("tag parser exploded" in p for p in df.problems)
    assert (
        df.legend["entries"][0]["tag"] == "JB"
        and df.panels[0]["name"] == "1LI"
        and df.fixture_types[0]["linear_hint"]
    )
    assert page.facts_block_tokens == 123
    assert df.spec is None and df.schedules == [] and df.cross_trade == []


def test_rotated_page_fixture_through_extract():
    """The spec's rotated-page fixture: a /Rotate 270 sheet with a CropBox
    offset still yields its sheet number, title, scale and plan role."""
    s = plan_sheet("E2.01", "OVERALL POWER PLAN - NEW WORK")
    s.rotate = 270
    s.crop_offset_in = (1.5, 0.75)
    df = _extract(make_pdf([s, plan_sheet("E2.02", "SECOND PLAN")]))
    page = df.pages[0]
    assert page.rotation == 270 and (page.w_in, page.h_in) == (42.0, 30.0)
    assert page.sheet_number == "E2.01" and page.sheet_title == "OVERALL POWER PLAN - NEW WORK"
    assert page.primary_ft_per_in == 4.0 and page.page_role == "plan"
    assert page.region("title_block").bbox[0] > 0.85
    for v in page.views:
        assert all(0 <= c <= 1 for c in v.anchor)


def test_doc_facts_json_round_trip():
    df = _extract(
        make_pdf([plan_sheet("E2.01", "POWER PLAN"), plan_sheet("E3.01", "LIGHTING PLAN")])
    )
    payload = json.loads(json.dumps(df.to_dict()))
    back = DocFacts.from_dict(payload)
    assert back.to_dict() == df.to_dict()
    assert (
        payload["schema"] == contracts.FACTS_SCHEMA
        and payload["extractor_version"] == contracts.EXTRACTOR_VERSION
    )
    assert payload["pages"][0]["text_layer_quality"] in ("full", "sparse")
    assert set(payload["pages"][0]["tags"]) == {"circuit", "status", "type_marks", "panel_labels"}
    assert set(payload["legend"]) == {"entries", "modifiers", "abbreviations"}
    key = store.cache_key(df.sha256, df.kind)
    assert key[0] == extract.sha256_of(
        make_pdf([plan_sheet("E2.01", "POWER PLAN"), plan_sheet("E3.01", "LIGHTING PLAN")])
    )


# Golden-local 7154 (the backup PDFs, $0) ------------------------------------------

EXPECTED_ELEC = [
    ("E0.00", ["ELECTRICAL", "COVERSHEET"], None),
    ("E0.01", ["ELECTRICAL", "SPECIFICATIONS"], None),
    ("E0.02", ["SINGLE", "LINE"], None),
    ("E0.03", ["PANEL", "SCHEDULES"], None),
    ("E0.04", ["LIGHTING", "FIXTURE", "SCHEDULE"], None),
    ("E1.00", ["OVERALL", "PLAN"], 16.0),
    ("ED2.01", ["POWER", "PLAN", "DEMOLITION"], 4.0),
    ("ED3.01", ["LIGHTING", "PLAN", "DEMOLITION"], 4.0),
    ("E2.01", ["POWER", "PLAN", "NEW"], 4.0),
    ("E3.01", ["LIGHTING", "PLAN", "NEW"], 4.0),
]
_STABLE_PAGE_KEYS = (
    "page",
    "w_in",
    "h_in",
    "rotation",
    "text_class",
    "sheet_number",
    "sheet_number_source",
    "sheet_title",
    "discipline",
    "discipline_category",
    "roles",
    "page_role",
    "primary_ft_per_in",
    "scale_conflict",
    "views",
)


def _golden_docs() -> dict[str, DocFacts]:
    return {
        "elec": _extract(ELEC_PDF.read_bytes(), name=ELEC_PDF.name.split("-", 1)[1]),
        "mech": _extract(MECH_PDF.read_bytes(), name=MECH_PDF.name.split("-", 1)[1]),
    }


@pytest.fixture(scope="module")
def golden_docs() -> dict[str, DocFacts]:
    if not (ELEC_PDF.exists() and MECH_PDF.exists()):
        pytest.skip("7154 backup PDFs not on this machine")
    return _golden_docs()


@golden
@needs_backup
def test_pf1_elec_sheet_numbers_titles_scales(golden_docs):
    elec = golden_docs["elec"]
    assert elec.page_count == 10 and elec.kind == "drawing"
    assert [p.sheet_number for p in elec.pages] == [e[0] for e in EXPECTED_ELEC]
    assert all(p.sheet_number_source == "slot" for p in elec.pages)
    for page, (_number, words, ft) in zip(elec.pages, EXPECTED_ELEC, strict=True):
        assert page.sheet_title and all(w in page.sheet_title for w in words), (
            page.sheet_number,
            page.sheet_title,
        )
        assert page.primary_ft_per_in == ft, (page.sheet_number, page.primary_ft_per_in)
        assert (page.w_in, page.h_in) == (42.0, 30.0) and page.text_class == "cad_text"
        assert page.scale_conflict is False
    cover = elec.pages[0]
    examples = [v for v in cover.views if v.example]
    assert (
        len(examples) == 1
        and examples[0].scale_text == '1/8" = 1\'-0"'
        and examples[0].ft_per_in == 8.0
    )
    assert cover.primary_ft_per_in is None
    assert elec.category_check.action == "none" and elec.category_check.share == 1.0
    assert elec.problems == []
    rev = elec.pages[0].revision
    assert rev is not None and rev.date == "06/15/2026" and rev.number == "1"


@golden
@needs_backup
def test_pf2_elec_roles(golden_docs):
    elec = golden_docs["elec"]
    by = {p.sheet_number: p for p in elec.pages}
    assert {"legend_symbols", "general_notes"} <= set(by["E0.00"].roles)
    assert by["E0.03"].page_role == "schedule"
    assert "schedule" in by["E0.04"].roles
    for number in ("E1.00", "ED2.01", "ED3.01", "E2.01", "E3.01"):
        assert by[number].page_role == "plan", number
    assert by["E0.02"].page_role == "diagram" and by["E0.01"].page_role == "general_notes"
    legend_pages = [
        p.sheet_number for p in elec.pages if set(p.roles) & contracts.LEDGER_LEGEND_ROLES
    ]
    assert "E0.00" in legend_pages


@golden
@needs_backup
def test_pf3_extraction_timing(golden_docs):
    elec, mech = golden_docs["elec"], golden_docs["mech"]
    total_ms = elec.stats["ms_total"] + mech.stats["ms_total"]
    p90_ms = max(elec.stats["ms_p90_page"], mech.stats["ms_p90_page"])
    print(f"PF3 measured: {total_ms} ms for 18 drawing pages, p90 page {p90_ms} ms")
    assert elec.page_count + mech.page_count == 18
    assert total_ms < 3000, total_ms
    assert p90_ms < 500, p90_ms
    assert not elec.stats["budget_hit"] and not mech.stats["budget_hit"]


@golden
@needs_backup
def test_pf4_mech_relabel(golden_docs):
    mech = golden_docs["mech"]
    assert mech.page_count == 8
    numbers = [p.sheet_number for p in mech.pages]
    assert all(n and n.startswith("M") for n in numbers), numbers
    assert numbers[0] == "M0.00" and all(
        p.discipline_category == "mechanical_drawings" for p in mech.pages
    )
    check = mech.category_check
    assert (
        check.action == "relabel"
        and check.detected == "mechanical_drawings"
        and check.share == 1.0
        and check.numbered_pages == 8
    )
    assert "Mechanical Drawings" in check.message and "Electrical Drawings" in check.message


def _fixture_payload(docs: dict[str, DocFacts]) -> dict:
    out = {
        "schema": contracts.FACTS_SCHEMA,
        "extractor_version": contracts.EXTRACTOR_VERSION,
        "note": "Page facts of the 7154 ELEC and MECH drawing sets (spec 3.2), extracted from the local backup with no model call. Timing fields are zeroed. No pricing data.",
        "project_id": "7dfc64e6-36ef-4c67-9d02-0d7942015011",
        "documents": [],
    }
    for key, path in (("elec", ELEC_PDF), ("mech", MECH_PDF)):
        facts = docs[key].to_dict()
        facts["stats"] = {k: (0 if k.startswith("ms_") else v) for k, v in facts["stats"].items()}
        for page in facts["pages"]:
            page["timing_ms"] = 0
        out["documents"].append(
            {
                "key": key,
                "label": "electrical_drawings",
                "backup_path": str(path.relative_to(BACKUP)),
                "facts": facts,
            }
        )
    return out


@golden
@needs_backup
def test_fixture_matches_fresh_extraction(golden_docs):
    assert FIXTURE.exists(), (
        "regenerate with: uv run python -m tests.test_pagefacts_titleblock_scales_roles --regen"
    )
    stored = json.loads(FIXTURE.read_text())
    fresh = _fixture_payload(golden_docs)
    assert stored["extractor_version"] == contracts.EXTRACTOR_VERSION
    for s_doc, f_doc in zip(stored["documents"], fresh["documents"], strict=True):
        assert s_doc["facts"]["sha256"] == f_doc["facts"]["sha256"]
        assert s_doc["facts"]["category_check"] == f_doc["facts"]["category_check"]
        for s_page, f_page in zip(s_doc["facts"]["pages"], f_doc["facts"]["pages"], strict=True):
            for k in _STABLE_PAGE_KEYS:
                assert s_page[k] == f_page[k], (s_doc["key"], s_page["page"], k)
    assert not any("$" in json.dumps(d) for d in stored["documents"])


def test_fixture_file_has_the_final_schema():
    """The committed fixture is data for WS3, WS4 and WS8 even without the
    backup: 10 ELEC pages, 8 MECH pages, final keys, no dollars."""
    stored = json.loads(FIXTURE.read_text())
    assert stored["schema"] == contracts.FACTS_SCHEMA
    elec, mech = stored["documents"]
    assert [p["sheet_number"] for p in elec["facts"]["pages"]] == [e[0] for e in EXPECTED_ELEC]
    assert (
        len(mech["facts"]["pages"]) == 8 and mech["facts"]["category_check"]["action"] == "relabel"
    )
    page = elec["facts"]["pages"][8]
    assert set(page) >= set(_STABLE_PAGE_KEYS) | {
        "census",
        "regions",
        "notes",
        "keynote_markers",
        "schedule_ids",
        "tags",
        "facts_block_tokens",
        "problems",
        "text_layer_quality",
    }
    assert page["views"][0]["ft_per_in"] == 4.0 and page["views"][0]["region"] is not None
    assert DocFacts.from_dict(elec["facts"]).page(9).sheet_number == "E2.01"
    assert "$" not in json.dumps(stored)


def regenerate_fixture() -> Path:
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(
        json.dumps(_fixture_payload(_golden_docs()), indent=1, ensure_ascii=False) + "\n"
    )
    return FIXTURE


if __name__ == "__main__" and "--regen" in sys.argv:
    print(regenerate_fixture())

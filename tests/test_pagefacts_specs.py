"""pagefacts.specs on synthetic letter-size PDFs (section map by footer,
header and carry-forward; TOC rows, division notes and missing sections;
the spec-select/1 tiers; the small-document, unmapped and scanned
fallbacks; the E24 share rule) plus the golden-local 7154 check PF7 on the
backup SPECS.pdf (marked `golden`; skipped when the backup is absent).

No network, no database, no model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.pagefacts import extract, specs
from app.services.pagefacts.contracts import DocFacts
from tests.pagefacts_fixtures import make_pdf, sheet

BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
PROJECT_DIR = BACKUP / "storage/project-files/7dfc64e6-36ef-4c67-9d02-0d7942015011"
SPECS_PDF = (
    PROJECT_DIR
    / "specification/47924a195f3c4525b6e6abd57c17b8a6-Reference Material - Architectural - SPECS.pdf"
)
TECH_PDF = (
    PROJECT_DIR
    / "specification/aed667ca84774c63aeda6cb8ce4f40cb-Reference Material - Tech Specs 1.pdf"
)
DOOR_PDF = (
    PROJECT_DIR
    / "specification/92083b13b1a74e8da33e43a9b30e9308-Reference Material - UMC Door Spec Guide 2025.pdf"
)
ELEC_PDF = (
    PROJECT_DIR
    / "drawing/c85a4720840d49a288d9d0c7d3c87fed-Reference Material - Architectural - ELEC_RESPONSE_TO_CLV_COMMENTS D1.pdf"
)
golden = pytest.mark.golden
needs_backup = pytest.mark.skipif(
    not (SPECS_PDF.exists() and TECH_PDF.exists() and DOOR_PDF.exists() and ELEC_PDF.exists()),
    reason="7154 backup PDFs not on this machine",
)
BUDGETS = {"page_budget_s": 4.0, "doc_budget_s": 240.0}
LETTER = {"w_in": 8.5, "h_in": 11.0}

FILLER = (
    "The Contractor shall coordinate the Work with the Owner and shall submit the schedule "
    "for review before the preconstruction meeting takes place on site."
)


def _spec_page(
    section: str | None,
    *,
    page_no: int = 1,
    header: str | None = None,
    title: str | None = None,
    body: list[str] | None = None,
    footer_form: str = "{s} -{n}",
    toc_title: bool = False,
):
    texts: list[tuple] = []
    y = 1.0
    if toc_title:
        texts.append((3.0, y, 10, "TABLE OF CONTENTS"))
        y += 0.4
    if header:
        texts.append((3.0, y, 9, header))
        y += 0.25
        if title:
            texts.append((2.5, y, 9, title))
            y += 0.25
    for line in body or [FILLER, FILLER, FILLER]:
        texts.append((1.0, y + 0.3, 8, line))
        y += 0.2
    if section:
        texts.append((3.5, 10.4, 7, footer_form.format(s=section, n=page_no)))
    return sheet(*texts, **LETTER)


def _extract(pdf: bytes, label: str = "specifications") -> DocFacts:
    return extract.extract_document(pdf, label=label, **BUDGETS)


def _words(pdf: bytes):
    return extract.extract_words(pdf)


def test_section_map_footer_header_and_carry_forward():
    pages = [
        _spec_page(None, body=["PROJECT MANUAL", "Outpatient Pharmacy", "August 22, 2025"]),
        _spec_page(
            "01 31 00",
            page_no=1,
            header="SECTION 01 31 00",
            title="PROJECT MANAGEMENT AND COORDINATION",
        ),
        _spec_page("01 31 00", page_no=2, footer_form="{s}-{n}"),
        _spec_page(None, body=[FILLER]),  # no footer: carries 01 31 00
        _spec_page(None, header="SECTION 26 05 19", title="LOW-VOLTAGE CONDUCTORS", body=[FILLER]),
        _spec_page("26 05 19", page_no=2, footer_form="{s} - {n}"),
    ]
    spec = specs.section_map(_words(make_pdf(pages)))
    entries = [
        (e["section"], e["page_start"], e["page_end"], e["source"], e["title"])
        for e in spec["sections"]
    ]
    # Only numbered sections are sections; the cover run is an unmapped marker.
    assert entries == [
        ("01 31 00", 2, 4, "footer", "PROJECT MANAGEMENT AND COORDINATION"),
        ("26 05 19", 5, 6, "header", "LOW-VOLTAGE CONDUCTORS"),
    ]
    assert spec["unmapped"] == [{"page_start": 1, "page_end": 1}]
    assert spec["mapped_page_share"] == round(5 / 6, 4)
    assert spec["has_div"] == {"26": True, "27": False, "28": False}
    assert spec["toc"] == [] and spec["scanned_pages"] == [] and spec["page_count"] == 6
    assert [e["division"] for e in spec["sections"]] == ["01", "26"]


def test_toc_rows_divisions_and_missing_in_body():
    toc_page = sheet(
        (3.0, 0.7, 9, "DOCUMENT 00 01 10"),
        (3.0, 1.0, 10, "TABLE OF CONTENTS"),
        (1.0, 1.6, 8, "Section"),
        (2.0, 1.6, 8, "Title"),
        (1.0, 2.0, 8, "01 50 00"),
        (2.0, 2.0, 8, "Temporary Facilities and Controls"),
        (1.0, 2.3, 8, "01 73 29"),
        (2.0, 2.3, 8, "Cutting and Patching"),
        (1.0, 2.6, 8, "08 71 00 Door Hardware .......... 12"),
        (1.0, 3.0, 8, "DIVISION 25 - INTEGRATED AUTOMATION - NOT USED"),
        (1.0, 3.3, 8, "DIVISION 26 - ELECTRICAL - REFER TO ELECTRICAL DRAWINGS"),
        (1.0, 3.6, 8, "DIVISION 27 - COMMUNICATIONS - REFER TO TELECOMM DRAWINGS"),
        (1.0, 3.9, 8, "DIVISION 12 - 20 - NOT USED"),
        (1.0, 4.2, 8, "DIVISION 09 - FINISHES"),
        (3.5, 10.4, 7, "00 01 10-1"),
        **LETTER,
    )
    pages = [
        toc_page,
        _spec_page(
            "01 50 00", header="SECTION 01 50 00", title="TEMPORARY FACILITIES AND CONTROLS"
        ),
        _spec_page("01 50 00", page_no=2),
    ]
    spec = specs.section_map(_words(make_pdf(pages)))
    assert spec["toc_pages"] == [1]
    assert spec["toc"] == [
        {"section": "01 50 00", "title": "Temporary Facilities and Controls", "page": None},
        {"section": "01 73 29", "title": "Cutting and Patching", "page": None},
        {"section": "08 71 00", "title": "Door Hardware", "page": 12},
    ]
    divs = {d["division"]: d for d in spec["divisions"]}
    assert divs["25"]["status"] == "not_used" and divs["25"]["title"] == "INTEGRATED AUTOMATION"
    assert divs["26"] == {
        **divs["26"],
        "status": "refer_to_drawings",
        "note": "ELECTRICAL DRAWINGS",
        "page": 1,
    }
    assert divs["27"]["status"] == "refer_to_drawings" and divs["27"]["note"] == "TELECOMM DRAWINGS"
    assert divs["12"]["division_to"] == "20" and divs["12"]["status"] == "not_used"
    assert divs["09"]["status"] == "listed" and divs["09"]["title"] == "FINISHES"
    assert spec["missing_in_body"] == ["01 73 29", "08 71 00"]
    assert spec["has_div"]["26"] is False
    assert [e["section"] for e in spec["sections"]] == ["00 01 10", "01 50 00"]
    assert spec["sections"][0]["title"] == "TABLE OF CONTENTS"


def test_division_line_regex_accepts_document_dashes():
    m = specs.DIVISION_LINE_RE.match(
        "DIVISION 26 \u2013 ELECTRICAL \u2013 REFER TO ELECTRICAL DRAWINGS"
    )
    assert m and m.group("div") == "26" and "REFER TO" in m.group("rest")
    m2 = specs.FOOT_RE.search("26 05 19 \u2014 3")
    assert m2 and m2.group(5) == "3"
    assert specs.HEADER_RE.match("DOCUMENT 00 01 10") and specs.HEADER_RE.match(
        "  Section 26 05 19"
    )


def _ten_page_manual() -> list:
    """Twelve pages: a TOC, Division 26 (2 pages), 01 50 00 (2 pages), a
    Division 09 section with one electrical paragraph (tier B), a Division
    09 section without (tier C), and an 11 xx section with electrical hits
    (tier A by keyword)."""
    pages = [
        sheet(
            (3.0, 1.0, 10, "TABLE OF CONTENTS"),
            (1.0, 2.0, 8, "26 05 19"),
            (2.0, 2.0, 8, "Conductors"),
            (3.5, 10.4, 7, "00 01 10-1"),
            **LETTER,
        ),
        _spec_page("26 05 19", header="SECTION 26 05 19", title="LOW-VOLTAGE CONDUCTORS"),
        _spec_page("26 05 19", page_no=2),
        _spec_page(
            "01 50 00", header="SECTION 01 50 00", title="TEMPORARY FACILITIES AND CONTROLS"
        ),
        _spec_page("01 50 00", page_no=2),
        _spec_page(
            "09 29 00",
            header="SECTION 09 29 00",
            title="GYPSUM BOARD",
            body=[
                "A. Coordinate openings in gypsum board with the electrical boxes and conduit installed by Division 26.",
                FILLER,
                "B. Finish all surfaces to Level 4 before painting.",
            ],
        ),
        _spec_page("09 29 00", page_no=2),
        _spec_page(
            "09 65 00", header="SECTION 09 65 00", title="RESILIENT FLOORING", body=[FILLER, FILLER]
        ),
        _spec_page("09 65 00", page_no=2),
        _spec_page(
            "11 31 00",
            header="SECTION 11 31 00",
            title="APPLIANCES",
            body=[
                "Provide power connection and disconnect for each appliance by the electrical contractor.",
                FILLER,
            ],
        ),
        _spec_page("11 31 00", page_no=2),
        _spec_page("14 21 00", header="SECTION 14 21 00", title="ELEVATORS", body=[FILLER, FILLER]),
    ]
    return pages


def test_selection_tiers_div26_div01_keyword_snippets_and_titles_only():
    df = _extract(make_pdf(_ten_page_manual()))
    spec = df.spec
    assert spec is not None and df.kind == "text" and df.legend["entries"] == []
    tiers = {e["section"]: e["tier"] for e in spec["sections"]}
    assert tiers == {
        "00 01 10": "C",
        "26 05 19": "A",
        "01 50 00": "A",
        "09 29 00": "B",
        "09 65 00": "C",
        "11 31 00": "A",
        "14 21 00": "C",
    }
    sel = spec["selection"]
    assert (
        sel["pages"] == [2, 3, 4, 5, 10, 11]
        and sel["policy"] == "spec-select/1"
        and sel["flags"] == []
    )
    assert sel["est_tokens_before"] == 12 * specs.FILE_TOKENS_PER_PAGE
    assert sel["est_tokens_after"] < sel["est_tokens_before"]
    snippets = sel["tier_b_snippets"]
    assert len(snippets) == 1 and snippets[0]["section"] == "09 29 00" and snippets[0]["page"] == 6
    assert "Division 26" in snippets[0]["text"] and snippets[0]["keywords"] == [
        "CONDUIT",
        "DIVISION 26",
        "ELECTRICAL",
    ]
    assert sel["pages_share"] == 0.5
    assert specs.pages_sent_share([spec]) == 0.5
    assert specs.planned_spec_tokens([spec]) == sel["est_tokens_after"]
    hits = specs.lookup(spec, section="26")
    assert [h["section"] for h in hits] == ["26 05 19"]
    assert specs.lookup(spec, keyword="GYPSUM")[0]["section"] == "09 29 00"
    assert df.pages[0].page_role == "spec_text" and all(
        p.tags == {"circuit": [], "status": {}, "type_marks": {}, "panel_labels": []}
        for p in df.pages
    )


def test_small_document_is_sent_whole():
    df = _extract(make_pdf(_ten_page_manual()[:6]))
    sel = df.spec["selection"]
    assert sel["flags"] == ["small_doc"] and sel["pages"] == [1, 2, 3, 4, 5, 6]
    assert all(e["tier"] == "A" for e in df.spec["sections"])
    assert sel["est_tokens_after"] == 6 * specs.FILE_TOKENS_PER_PAGE


def test_unmapped_document_is_sent_whole_and_flagged():
    pages = [_spec_page(None, body=[FILLER, f"Paragraph {i}"]) for i in range(7)]
    pages.append(_spec_page("09 29 00", header="SECTION 09 29 00", title="GYPSUM BOARD"))
    df = _extract(make_pdf(pages))
    spec = df.spec
    assert spec["mapped_page_share"] == round(1 / 8, 4)
    assert spec["selection"]["flags"] == ["spec_unmapped"] and spec["selection"]["pages"] == list(
        range(1, 9)
    )


def test_scanned_pages_are_sent_whole_and_priced_by_area():
    pages = _ten_page_manual()[:5]
    pages.append(sheet(image_cover=1.0, **LETTER))  # a scanned page: no text at all
    pages.append(sheet(image_cover=1.0, w_in=11.0, h_in=17.0))
    pages.append(
        _spec_page("09 65 00", header="SECTION 09 65 00", title="RESILIENT FLOORING", body=[FILLER])
    )
    df = _extract(make_pdf(pages))
    spec = df.spec
    assert spec["scanned_pages"] == [6, 7]
    sel = spec["selection"]
    assert "scanned_pages" in sel["flags"] and {6, 7} <= set(sel["pages"])
    letter = round(specs.SCANNED_TOKENS_PER_LETTER_PAGE * 8.5 * 11.0 / specs.LETTER_PAGE_SQ_IN)
    tabloid = round(specs.SCANNED_TOKENS_PER_LETTER_PAGE * 11.0 * 17.0 / specs.LETTER_PAGE_SQ_IN)
    assert letter == 8400 and tabloid == 16800
    text_pages = len(sel["pages"]) - 2
    assert sel["est_tokens_after"] == text_pages * specs.FILE_TOKENS_PER_PAGE + letter + tabloid


def test_no_div26_note_points_to_the_drawing_sheet():
    from tests.pagefacts_fixtures import title_block_texts

    e001 = sheet(
        (10.0, 5.0, 14, "ELECTRICAL SPECIFICATIONS"),
        (10.0, 5.5, 7, "1. THE WORK SHALL COMPLY WITH NEC 2020."),
        *title_block_texts("E0.01", "ELECTRICAL SPECIFICATIONS"),
    )
    drawings = _extract(make_pdf([e001]), label="electrical_drawings")
    spec_no = {"has_div": {"26": False, "27": False, "28": False}}
    spec_yes = {"has_div": {"26": True, "27": False, "28": False}}
    note = specs.no_div26_note([spec_no], [drawings], doc_refs={drawings.sha256: '"ELEC"'})
    assert (
        note
        == 'Electrical specifications are on sheet E0.01 ("ELEC" page 1); the project manual has no Division 26.'
    )
    # No label known: the note never invents a D number.
    unlabeled = specs.no_div26_note([spec_no], [drawings])
    assert "(the drawings page 1)" in unlabeled and " D1" not in unlabeled
    assert specs.no_div26_note([spec_no, spec_yes], [drawings]) is None
    assert specs.no_div26_note([], [drawings]) is None
    assert "no drawing sheet titled" in specs.no_div26_note([spec_no], [])


def test_e24_share_rule_over_several_documents():
    docs = [
        {"page_count": 160, "selection": {"pages": list(range(1, 21))}},
        {"page_count": 10, "selection": {"pages": list(range(1, 11))}},
    ]
    assert specs.pages_sent_share(docs) == round(30 / 170, 4) <= specs.E24_MAX_SHARE
    assert specs.pages_sent_share([]) == 0.0


# Golden-local 7154 (the backup PDFs, $0) --------------------------------------------


@pytest.fixture(scope="module")
def spec_docs() -> dict[str, DocFacts]:
    if not (SPECS_PDF.exists() and TECH_PDF.exists() and DOOR_PDF.exists() and ELEC_PDF.exists()):
        pytest.skip("7154 backup PDFs not on this machine")
    return {
        "specs": _extract(SPECS_PDF.read_bytes()),
        "tech": _extract(TECH_PDF.read_bytes()),
        "door": _extract(DOOR_PDF.read_bytes()),
        "elec": _extract(ELEC_PDF.read_bytes(), label="electrical_drawings"),
    }


@golden
@needs_backup
def test_pf7_specs_sections_no_div26_and_selection(spec_docs):
    spec = spec_docs["specs"].spec
    numbered = [e for e in spec["sections"] if e["section"]]
    print(
        f"PF7 measured: {len(spec['sections'])} mapped sections (spec PF7 says 34: that count included "
        f"the unmapped cover run, now reported apart as unmapped {spec['unmapped']}), "
        f"mapped share {spec['mapped_page_share']}, toc rows {len(spec['toc'])}, "
        f"divisions {len(spec['divisions'])}, missing in body {spec['missing_in_body']}"
    )
    assert spec_docs["specs"].page_count == 166
    assert len(spec["sections"]) == 33 and len(numbered) == 33
    assert spec["unmapped"] == [{"page_start": 1, "page_end": 1}]
    assert spec["mapped_page_share"] >= 0.98
    assert spec["has_div"] == {"26": False, "27": False, "28": False}
    divs = {d["division"]: d for d in spec["divisions"]}
    assert (
        divs["26"]["status"] == "refer_to_drawings" and divs["26"]["note"] == "ELECTRICAL DRAWINGS"
    )
    assert divs["28"]["status"] == "not_used" and divs["25"]["status"] == "not_used"
    assert spec["missing_in_body"] == [
        "08 51 20"
    ]  # the TOC says 08 51 20, the body carries 08 51 13
    tier_a = {
        e["section"]: (e["page_start"], e["page_end"]) for e in spec["sections"] if e["tier"] == "A"
    }
    assert tier_a == {
        "01 50 00": (24, 30),
        "01 73 29": (34, 36),
        "02 41 19": (54, 57),
        "07 84 13": (75, 78),
    }
    sel = spec["selection"]
    expected_pages = [*range(24, 31), *range(34, 37), *range(54, 58), *range(75, 79)]
    assert sel["pages"] == expected_pages and sel["flags"] == []
    titles = {e["section"]: e["title"] for e in numbered}
    assert (
        titles["01 50 00"] == "TEMPORARY FACILITIES AND CONTROLS"
        and titles["02 41 19"] == "SELECTIVE DEMOLITION"
    )
    all_specs = [spec_docs[k].spec for k in ("specs", "tech", "door")]
    planned = specs.planned_spec_tokens(all_specs)
    share = specs.pages_sent_share(all_specs)
    print(
        f"PF7 measured: planned spec input {planned} tokens (SPECS {sel['est_tokens_before']} -> {sel['est_tokens_after']}, {len(sel['tier_b_snippets'])} tier B snippets); pages sent share {share}"
    )
    assert planned <= 58_000
    assert share <= specs.E24_MAX_SHARE
    for key in ("tech", "door"):
        assert spec_docs[key].spec["selection"]["flags"] == ["small_doc"]
    note = specs.no_div26_note(
        all_specs, [spec_docs["elec"]], doc_refs={spec_docs["elec"].sha256: '"ELEC"'}
    )
    assert (
        note
        == 'Electrical specifications are on sheet E0.01 ("ELEC" page 2); the project manual has no Division 26.'
    )
    assert "\u2013" not in str(spec) and "\u2014" not in str(spec)

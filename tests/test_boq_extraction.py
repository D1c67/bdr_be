"""Unit tests for BOQ extraction + RFQ Excel generation (pure, no DB / no LLM)."""

import io
from pathlib import Path

import pytest

from app.services import boq_extraction as bx
from app.services import rfq_excel as rx

EXCEL_DIR = Path(__file__).resolve().parents[1] / "excel_format"
BOQ = EXCEL_DIR / "BOQ - COUNTS.xlsx"
RFQ = EXCEL_DIR / "6954_Lighting_RFQ.xlsx"


@pytest.mark.skipif(not BOQ.exists(), reason="example BOQ not present")
def test_worksheets_to_text_renders_document_body():
    text = bx.worksheets_to_text(BOQ.read_bytes())
    # Each non-empty worksheet is labelled; empty Chart1/Sheet1 are skipped.
    assert "--- WORKSHEET: SHEET ---" in text
    assert "--- WORKSHEET: Chart1 ---" not in text
    # Tab-separated cells, real line items preserved verbatim.
    assert "DESCRIPTION\tQUANTITY\tUNIT" in text
    assert "#600 THHN/THWN" in text


def test_build_system_prompt_injects_dynamic_categories():
    sp = bx.build_system_prompt(["General material", "Lighting", "EV chargers"])
    assert "- General material" in sp
    assert "- EV chargers" in sp  # a category not in the original hardcoded list
    # Schema + anti-injection guard preserved.
    assert '"site_name"' in sp
    assert "<document>" in sp


def test_build_user_prompt_wraps_document():
    up = bx.build_user_prompt("--- WORKSHEET: A ---\nfoo\tbar")
    assert "<document>" in up and "</document>" in up
    assert "foo\tbar" in up


def test_parse_json_strips_code_fences():
    raw = '```json\n{"sites": [], "summary": "x", "total_material_count": 0}\n```'
    data = bx._parse_json(raw)
    assert data["sites"] == [] and data["total_material_count"] == 0


def test_parse_json_rejects_non_schema():
    with pytest.raises(ValueError):
        bx._parse_json('{"not": "a boq"}')


def test_refine_feature_is_removed():
    """The "ask model to fix" loop is gone — user corrections (drafts + the
    confirm diff capture) replaced it. No service entrypoint, no schema, no
    route may survive, or a stale frontend could resurrect a paid LLM call."""
    import app.models.schemas as schemas
    from app.routers.boq_analysis import router

    assert not hasattr(bx, "refine_extraction")
    assert not hasattr(schemas, "BoqRefineIn")
    assert not any("refine" in getattr(r, "path", "") for r in router.routes)


ITEMS = [
    {"sr_no": "51", "description": "BOH 2X4 LED PANEL", "quantity": 56, "unit": "EA", "notes": None},
    {"sr_no": "61", "description": "FLEX LED", "quantity": 1340.2, "unit": "FT", "notes": "3.7W/FT"},
]


def test_build_rfq_workbook_matches_reference_shape():
    rows = rx.rows_for_preview(rx.build_rfq_workbook("Lighting", ITEMS))
    # Letterhead, category banner, then header (SR.NO/DESCRIPTION/QUANTITY/UNIT)
    # and the items.
    flat = [c for r in rows for c in r]
    assert "REQUEST FOR QUOTE" in flat
    assert "LIGHTING" in flat  # the navy category banner
    header = next(r for r in rows if r and r[0] == "SR.NO")
    assert header[:4] == ["SR.NO", "DESCRIPTION", "QUANTITY", "UNIT"]
    assert "PRICE" not in [str(c).upper() for c in header]  # vendor fills pricing
    data_rows = [r for r in rows if r and r[0] in ("51", "61")]
    assert len(data_rows) == 2
    assert data_rows[0][1] == "BOH 2X4 LED PANEL" and data_rows[0][2] == 56


def test_build_rfq_workbook_letterhead_carries_project_context():
    """The project number/name and the vendor due date land on the letterhead —
    and the G3 logo is embedded, not just referenced."""
    project = {
        "number": "6954",
        "name": "Sunset Ridge",
        "due_from_vendors_at": "2026-08-11T21:00:00+00:00",
    }
    xlsx = rx.build_rfq_workbook("Lighting", ITEMS, project)
    flat = [str(c) for r in rx.rows_for_preview(xlsx) for c in r]
    assert "6954 - Sunset Ridge" in flat
    due = next(c for c in flat if c.startswith("QUOTES DUE"))
    assert "AUGUST 11TH" in due
    assert any(c.startswith("Issued ") for c in flat)
    assert any(c.startswith("G3 Electrical") for c in flat)

    import zipfile

    with zipfile.ZipFile(io.BytesIO(xlsx)) as z:
        assert [n for n in z.namelist() if n.startswith("xl/media/")]


def test_build_rfq_workbook_neutralizes_formula_injection():
    """A BOQ description lifted from the GC's sheet must not reach the vendor's
    workbook as a live formula (CWE-1236)."""
    items = [{"sr_no": "1", "description": '=cmd|/C calc!A0', "quantity": 1, "unit": "EA",
              "notes": "@SUM(1)"}]
    rows = rx.rows_for_preview(rx.build_rfq_workbook("Gear", items))
    row = next(r for r in rows if r and r[0] == "1")
    assert row[1].startswith("'=") and row[4].startswith("'@")


def test_build_rfq_workbook_without_project_omits_letterhead_lines():
    """No project context (or no due date) still yields a valid sheet."""
    flat = [str(c) for r in rx.rows_for_preview(rx.build_rfq_workbook("Gear", ITEMS)) for c in r]
    assert "REQUEST FOR QUOTE" in flat
    assert not any(c.startswith("QUOTES DUE") for c in flat)

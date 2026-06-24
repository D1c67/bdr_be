"""Unit tests for BOQ extraction + RFQ Excel generation (pure, no DB / no LLM)."""

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


def test_build_rfq_workbook_matches_reference_shape():
    items = [
        {"sr_no": "51", "description": "BOH 2X4 LED PANEL", "quantity": 56, "unit": "EA", "notes": None},
        {"sr_no": "61", "description": "FLEX LED", "quantity": 1340.2, "unit": "FT", "notes": "3.7W/FT"},
    ]
    rows = rx.rows_for_preview(rx.build_rfq_workbook("Lighting", items))
    # Title row, then header (SR.NO/DESCRIPTION/QUANTITY/UNIT), then the items.
    assert rows[0][1] == "LIGHTING"
    header = next(r for r in rows if r and r[0] == "SR.NO")
    assert header[:4] == ["SR.NO", "DESCRIPTION", "QUANTITY", "UNIT"]
    assert "PRICE" not in [str(c).upper() for c in header]  # vendor fills pricing
    data_rows = [r for r in rows if r and r[0] in ("51", "61")]
    assert len(data_rows) == 2
    assert data_rows[0][1] == "BOH 2X4 LED PANEL" and data_rows[0][2] == 56

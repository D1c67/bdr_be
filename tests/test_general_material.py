"""Unit tests for general-material extraction (pure, no DB / no LLM)."""

import io

import openpyxl
import pytest

from app.services import general_material as gm


def _workbook(sheets: dict[str, list[list]]) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_bid_recap_text_isolates_the_recap_sheet():
    xlsx = _workbook(
        {
            "Cover": [["ignore me"]],
            "Bid Recap and summary": [
                ["BID RECAP"],
                ["Description", "Material", "Labor"],
                ["Wiring", 12500.50, 9000],
                ["Gear", 4000, 1000],
            ],
        }
    )
    text = gm._bid_recap_text(xlsx)
    assert "--- WORKSHEET: Bid Recap and summary ---" in text
    assert "Wiring\t12500.5\t9000" in text
    assert "ignore me" not in text  # the cover sheet is excluded


def test_bid_recap_text_falls_back_to_whole_workbook():
    xlsx = _workbook({"Summary": [["Wiring", 100]]})  # no "recap" sheet
    text = gm._bid_recap_text(xlsx)
    assert "--- WORKSHEET: Summary ---" in text
    assert "Wiring\t100" in text


def test_build_system_prompt_targets_wiring_material():
    sp = gm.build_system_prompt()
    assert '"wiring_material_cost"' in sp
    assert "MATERIAL cost" in sp
    assert "<document>" in sp  # anti-injection guard preserved


def test_build_user_prompt_wraps_document():
    up = gm.build_user_prompt("--- WORKSHEET: Bid Recap and summary ---\nWiring\t100")
    assert "<document>" in up and "</document>" in up
    assert "Wiring\t100" in up


def test_parse_json_strips_fences_and_validates():
    raw = '```json\n{"wiring_material_cost": 12500.5, "found": true, "notes": "row 3"}\n```'
    data = gm._parse_json(raw)
    assert data["wiring_material_cost"] == 12500.5 and data["found"] is True


def test_parse_json_rejects_off_schema():
    with pytest.raises(ValueError):
        gm._parse_json('{"unexpected": 1}')

"""Unit tests for general-material extraction (pure, no DB / no LLM)."""

import io
from decimal import Decimal

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


def test_build_system_prompt_targets_both_material_rows():
    sp = gm.build_system_prompt()
    assert '"wiring_material_cost"' in sp
    assert '"other_items_material_cost"' in sp
    assert "MATERIAL cost" in sp
    assert "<document>" in sp  # anti-injection guard preserved


def test_build_user_prompt_wraps_document():
    up = gm.build_user_prompt("--- WORKSHEET: Bid Recap and summary ---\nWiring\t100")
    assert "<document>" in up and "</document>" in up
    assert "Wiring\t100" in up


def test_parse_json_strips_fences_and_validates():
    raw = (
        '```json\n{"wiring_material_cost": 12500.5, "other_items_material_cost": 2000,'
        ' "found": true, "notes": "rows 3 and 7"}\n```'
    )
    data = gm._parse_json(raw)
    assert data["wiring_material_cost"] == 12500.5
    assert data["other_items_material_cost"] == 2000
    assert data["found"] is True


def test_parse_json_rejects_off_schema():
    with pytest.raises(ValueError):
        gm._parse_json('{"unexpected": 1}')


def test_parse_json_rejects_missing_other_items():
    # Pre-combination shape (wiring only) is off-schema now: the job retries.
    with pytest.raises(ValueError):
        gm._parse_json('{"wiring_material_cost": 100, "found": true}')


# ── extracted_total: the figure is wiring + Other Items, summed in code ─────


def test_extracted_total_combines_both_components():
    total = gm.extracted_total(
        {"wiring_material_cost": 12500.5, "other_items_material_cost": 2000}
    )
    assert total == Decimal("14500.5")


def test_extracted_total_survives_a_missing_component():
    assert gm.extracted_total(
        {"wiring_material_cost": 100, "other_items_material_cost": None}
    ) == Decimal("100")
    assert gm.extracted_total(
        {"wiring_material_cost": None, "other_items_material_cost": 250}
    ) == Decimal("250")


def test_extracted_total_none_when_neither_found():
    assert (
        gm.extracted_total(
            {"wiring_material_cost": None, "other_items_material_cost": None}
        )
        is None
    )


def test_extracted_total_coerces_currency_strings():
    # The model is told to send plain numbers; tolerate it not listening.
    total = gm.extracted_total(
        {
            "wiring_material_cost": "$12,500.50",
            "other_items_material_cost": "2,000",
        }
    )
    assert total == Decimal("14500.50")


def test_extracted_total_ignores_unparseable_values():
    assert gm.extracted_total(
        {"wiring_material_cost": "n/a", "other_items_material_cost": True}
    ) is None


# ── _tax_reset: reprocessing invalidates the sales-tax attestation ──────────


def test_tax_reset_clears_on_new_estimate_file():
    prior = {"amount": "100", "estimate_file_id": "file-1", "tax_included": True}
    assert gm._tax_reset(prior, "file-2", 100) == {"tax_included": None}


def test_tax_reset_clears_on_changed_amount():
    prior = {"amount": "100", "estimate_file_id": "file-1", "tax_included": False}
    assert gm._tax_reset(prior, "file-1", 250) == {"tax_included": None}


def test_tax_reset_clears_when_figure_disappears():
    prior = {"amount": "100", "estimate_file_id": "file-1", "tax_included": True}
    assert gm._tax_reset(prior, "file-1", None) == {"tax_included": None}


def test_tax_reset_keeps_attestation_when_nothing_moved():
    prior = {"amount": "100", "estimate_file_id": "file-1", "tax_included": True}
    # Same file, numerically-equal amount ("100" == 100.00) — the answer stands.
    assert gm._tax_reset(prior, "file-1", 100.00) == {}


def test_tax_reset_noop_without_prior_attestation():
    assert gm._tax_reset(None, "file-1", 100) == {}
    prior = {"amount": "100", "estimate_file_id": "file-1", "tax_included": None}
    assert gm._tax_reset(prior, "file-2", 250) == {}

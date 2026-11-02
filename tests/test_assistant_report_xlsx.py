"""The assistant report workbook builder (docs/ASSISTANT_PHASE4_PLAN.md 3.9.2,
WI-4): house style, formula-safe text, Pacific times, number kinds, sheet
names, and the em dash rewrite of the existing Bid Invitations export."""

from __future__ import annotations

import io
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from app.services import assistant_report_xlsx as rx
from app.services.bid_invitations_excel import build_bid_invitations_workbook

EM = "\u2014"
UTC = timezone.utc


def _open(content: bytes):
    return openpyxl.load_workbook(io.BytesIO(content))


def _all_strings(wb):
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str):
                    yield ws.title, cell.coordinate, cell.value


def _sheet(rows, columns=None):
    columns = columns or [("Name", 30, "text"), ("Note", 30, "wrap")]
    return ("Main", columns, rows)


def test_opens_with_openpyxl_and_has_the_house_style():
    content = rx.build_workbook("Key Quotes: week of 10/26/2026", "Banner text", [_sheet([["A", "b"], ["C", "d"]])])
    wb = _open(content)
    ws = wb["Main"]
    assert ws["A1"].value == "Key Quotes: week of 10/26/2026" and ws["A1"].font.bold
    assert ws["A2"].value == "Banner text"
    assert ws["A3"].value == "Name" and ws["B3"].value == "Note"
    assert ws["A3"].fill.fgColor.rgb.endswith("202159")        # the navy of bid_invitations_excel
    assert ws["A3"].font.bold and ws["A3"].font.color.rgb.endswith("FFFFFF")
    assert ws["A3"].border.left.style == "thin"
    assert ws["A4"].value == "A" and ws["B5"].value == "d"
    assert ws["B4"].alignment.wrap_text is True
    assert ws.freeze_panes == "A4" and ws.print_title_rows in ("3:3", "$3:$3")
    assert ws.page_setup.orientation == "landscape" and ws.page_setup.fitToWidth == 1
    assert ws.sheet_properties.pageSetUpPr.fitToPage is True
    assert ws.column_dimensions["A"].width == 30
    assert "A1:B1" in {str(r) for r in ws.merged_cells.ranges}


@pytest.mark.parametrize("text", ["=cmd|' /C calc'!A0", "+SUM(A1)", "-2+3", "@SUM(1)", "\tlead", "=WEBSERVICE(\"x\")"])
def test_formula_like_text_is_neutralized(text):
    wb = _open(rx.build_workbook("T", "B", [_sheet([[text, text]])]))
    ws = wb["Main"]
    for cell in (ws["A4"], ws["B4"]):
        assert cell.data_type == "s" and cell.value == "'" + text
    # A formula-like title and banner are neutralized too.
    wb = _open(rx.build_workbook("=HYPERLINK(1)", "+1", [_sheet([])]))
    assert wb["Main"]["A1"].value.startswith("'") and wb["Main"]["A2"].value.startswith("'")


def test_times_render_pacific():
    summer = datetime(2026, 10, 23, 21, 30, tzinfo=UTC)         # PDT
    winter = datetime(2026, 12, 1, 20, 0, tzinfo=UTC)           # PST
    columns = [("When", 20, "datetime"), ("Day", 12, "date")]
    wb = _open(rx.build_workbook("T", "B", [_sheet([
        [summer, summer], [winter.isoformat(), "2026-12-01T07:30:00Z"], ["2026-12-01", date(2026, 3, 9)],
        [None, ""], ["not a date", "x"]], columns)]))
    ws = wb["Main"]
    assert ws["A4"].value == "10/23/2026 2:30 PM" and ws["B4"].value == "10/23/2026"
    assert ws["A5"].value == "12/1/2026 12:00 PM" and ws["B5"].value == "11/30/2026"     # 7:30 UTC is still the 30th
    assert ws["A6"].value == "12/1/2026" and ws["B6"].value == "3/9/2026"
    assert ws["A7"].value is None and ws["B7"].value is None
    assert ws["A8"].value == "not a date"
    assert rx.pacific(datetime(2026, 1, 5, 4, 30)) == "1/4/2026 8:30 PM"        # naive means UTC
    assert rx.pacific(None) == "" and rx.pacific("") == ""


def test_numeric_kinds_are_numbers_with_formats():
    columns = [("Price", 14, "money"), ("Pct", 8, "pct"), ("Count", 8, "int"), ("Text", 10, "text")]
    wb = _open(rx.build_workbook("T", "B", [_sheet([
        [Decimal("1250000.50"), "25.8", 7, 12], ["333.00", Decimal("-10.0"), "3", None], [None, "", None, "x"],
        ["junk", True, 2.0, "y"]], columns)]))
    ws = wb["Main"]
    assert ws["A4"].value == 1250000.5 and ws["A4"].number_format == '"$"#,##0.00' and ws["A4"].data_type == "n"
    assert ws["B4"].value == 25.8 and ws["B4"].number_format == '0.0"%"'
    assert ws["C4"].value == 7 and ws["C4"].number_format == "#,##0"
    assert ws["D4"].value == "12"                                # a text column stays text
    assert ws["A5"].value == 333 and ws["B5"].value == -10 and ws["C5"].value == 3
    assert ws["A6"].value is None and ws["B6"].value is None and ws["C6"].value is None
    assert ws["A7"].value is None and ws["B7"].value is None and ws["C7"].value == 2


def test_no_em_dash_survives_anywhere():
    text = f"Plaza {EM} Phase 2"
    content = rx.build_workbook(f"Report {EM} title", f"banner {EM} text",
                                [(f"Sheet {EM} one", [(f"Head {EM}", 20, "text")], [[text], [f"{EM}lead"]])])
    wb = _open(content)
    assert not [s for s in _all_strings(wb) if EM in s[2]]
    ws = wb.worksheets[0]
    assert ws.title == "Sheet - one" and ws["A1"].value == "Report - title"
    assert ws["A4"].value == "Plaza - Phase 2"
    assert ws["A5"].value == "'-lead"                            # the leading dash is neutralized after the rewrite
    assert rx.clean_text(None) == "" and rx.clean_text("a\x00b\x07c\n") == "abc\n"


def test_sheet_names_are_legal_and_unique():
    sheets = [("A/B:C[1]?*", [("H", 8, "text")], []), ("a/b:c[1]?*", [("H", 8, "text")], []),
              ("x" * 40, [("H", 8, "text")], []), ("", [("H", 8, "text")], [])]
    names = [ws.title for ws in _open(rx.build_workbook("T", "B", sheets)).worksheets]
    assert len(set(n.lower() for n in names)) == 4
    assert all(len(n) <= 31 and not any(c in n for c in "[]:*?/\\") for n in names)
    assert names[0] == "A B C 1" and names[1] == "a b c 1 2" and names[2] == "x" * 31 and names[3] == "Sheet"


def test_bad_input_is_refused():
    with pytest.raises(ValueError):
        rx.build_workbook("T", "B", [])
    with pytest.raises(ValueError):
        rx.build_workbook("T", "B", [("S", [("H", 8, "bogus")], [])])
    with pytest.raises(ValueError):
        rx.build_workbook("T", "B", [("S", [("H", 8, "text")], [["a", "b"]])])


def test_long_text_is_clipped():
    ws = _open(rx.build_workbook("T", "B", [_sheet([["x" * 20000, "y"]])]))["Main"]
    assert len(ws["A4"].value) == rx.MAX_CELL_CHARS


# ── the Bid Invitations export, rewritten ────────────────────────────────────


def _bid_payload(visible=True):
    return {
        "meta": {"range": "wtd", "date_from": "2026-10-19T07:00:00+00:00", "date_to": None, "open_ended": True,
                 "timezone": "America/Los_Angeles", "bid_dates_visible": visible},
        "summary": {"bids": 2, "won": 0, "lost": 0, "no_award": 0, "no_bid": 0, "waiting": 1, "active": 1},
        "rows": [
            {"project_id": "p1", "number": "0101", "name": f"Fire Station {EM} Phase 2", "status": "sent",
             "invitation_at": "2026-10-01T19:00:00+00:00", "est_start_date": "2026-11-16",
             "est_finish_date": "2027-03-05", "wage_type": None, "wage_label": f"Prevailing {EM} wage",
             "labor_note": "=cmd", "estimator_returned_at": "2026-10-20T20:00:00+00:00", "quotes_requested": 4,
             "quotes_received": 4, "sent_at": "2026-10-23T19:00:00+00:00", "on_time": "on_time",
             "bid_at": "2026-10-23T21:00:00+00:00",
             "gcs": [{"gc_id": "g1", "name": "Monument", "needs_by": "2026-10-23", "sent_at": "2026-10-23T19:00:00+00:00",
                      "on_time": "on_time"},
                     {"gc_id": "g2", "name": "Rafael", "needs_by": None, "sent_at": None, "on_time": None}]},
            {"project_id": "p2", "number": None, "name": f"{EM}Library", "status": "active",
             "invitation_at": None, "est_start_date": None, "est_finish_date": None, "wage_type": None,
             "wage_label": None, "labor_note": None, "estimator_returned_at": None, "quotes_requested": 0,
             "quotes_received": 0, "sent_at": None, "on_time": None, "bid_at": "2026-10-24T18:00:00+00:00",
             "gcs": [{"gc_id": "g1", "name": "Monument", "needs_by": None, "sent_at": None, "on_time": None}]},
        ],
    }


def test_the_source_export_really_contains_em_dashes():
    """Guards the premise of the rewrite: if the source file is ever fixed this
    test fails and strip_em_dashes can be dropped (owner question 23)."""
    raw = build_bid_invitations_workbook(_bid_payload())
    assert [s for s in _all_strings(_open(raw)) if EM in s[2]]


def test_strip_em_dashes_leaves_no_em_dash_in_any_cell_of_any_sheet():
    raw = build_bid_invitations_workbook(_bid_payload())
    fixed = rx.strip_em_dashes(raw)
    wb = _open(fixed)
    assert not [s for s in _all_strings(wb) if EM in s[2]]
    ws = wb.worksheets[0]
    assert ws["A1"].value.startswith("Bid Invitations: ") and EM not in ws["A1"].value      # a colon in the title
    flat = {v for _, _, v in _all_strings(wb)}
    assert "0101 - Fire Station - Phase 2" in flat                       # a hyphen elsewhere
    assert any("Prevailing - wage" in v for v in flat)
    assert "- Library" not in flat and "'-Library" in flat                 # a leading dash is formula-safe
    assert any(v.startswith("10/23/2026") and "Monument" in v and " - " in v for v in flat)
    # Style survives the round trip.
    assert ws["A3"].fill.fgColor.rgb.endswith("202159") and ws.freeze_panes == "A4"
    assert ws.page_setup.orientation == "landscape" and ws.print_title_rows in ("3:3", "$3:$3")
    assert {str(r) for r in ws.merged_cells.ranges} == {str(r) for r in _open(raw).worksheets[0].merged_cells.ranges}
    assert any(c.fill.fgColor.rgb.endswith(("FFFF99", "CCFFFF", "FFCCFF", "FFC000", "CCFF33")) for c in ws["A"])


def test_strip_em_dashes_returns_clean_workbooks_unchanged():
    clean = rx.build_workbook("T", "B", [_sheet([["a", "b"]])])
    assert rx.strip_em_dashes(clean) == clean


def test_strip_em_dashes_hides_nothing_for_non_viewers():
    wb = _open(rx.strip_em_dashes(build_bid_invitations_workbook(_bid_payload(visible=False))))
    assert not [s for s in _all_strings(wb) if EM in s[2]]
    assert wb.worksheets[0]["A3"].value == "Project"                    # no Bid Date column for this role


def test_modules_have_no_em_dash():
    assert EM not in Path(rx.__file__).read_text(encoding="utf-8")

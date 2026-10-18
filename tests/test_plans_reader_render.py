"""plans_reader_render: BOQ and estimate workbooks, markup and marked plans
PDFs, the deep brief and file names. Layout expectations come from the sent
deliverables in the 2026-10-01 production backup (docs/PLANS_READER.md 3.6);
the smoke test at the bottom re-renders one real sent BOQ when the backup is
on disk. No network, no database."""

from __future__ import annotations

import datetime
import io
from pathlib import Path

import openpyxl
import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

from app.services import plans_reader_render as prr
from app.services.plans_reader_schemas import (
    ESTIMATE_ALLOWANCES,
    LOW_VOLTAGE_NOTE,
    WIRING_WASTAGE_NOTE,
)

DATE = datetime.date(2026, 10, 17)


# Spec builders ---------------------------------------------------------------


def _item(
    desc, qty, unit="EA", *, wastage=False, refs=("E-101",), accessory=False, caveat=None, note=None
):
    return {
        "description": desc,
        "quantity": qty,
        "unit": unit,
        "sheet_refs": list(refs),
        "caveat": caveat,
        "material_note": note,
        "material_category": "General Material",
        "wastage_applies": wastage,
        "is_accessory": accessory,
    }


def _sg(name, items, note=None):
    return {"name": name, "note": note, "items": items}


def _group(name, subgroups, note=None):
    return {"name": name, "note": note, "subgroups": subgroups}


def _area(name="MAIN", *, notes=()):
    return {
        "area": name,
        "notes": list(notes),
        "sections": [
            {
                "heading": "DEMOLITION",
                "note": None,
                "groups": [
                    _group(
                        "DEMOLITION", [_sg("", [_item("REMOVAL OF PULLBOX", 8, refs=("ED-101",))])]
                    )
                ],
            },
            {
                "heading": "NEW WORK",
                "note": None,
                "groups": [
                    _group(
                        "WIRING",
                        [
                            _sg(
                                "CONDUCTORS (AWG)",
                                [
                                    _item(
                                        "#12 THHN/THWN Cu",
                                        12971,
                                        "FT",
                                        wastage=True,
                                        refs=("E1.00", "E1.01"),
                                    )
                                ],
                            ),
                            _sg(
                                "CONDUITS",
                                [
                                    _item('3/4"C EMT', 7530.65, "FT", wastage=True),
                                    _item('2"C EMT', 372.21, "FT", wastage=True, refs=("E1.02",)),
                                ],
                            ),
                            _sg(
                                "UNDERGROUND CONDUITS",
                                [_item('2"C PVC SCH 40', 90.88, "FT", wastage=True)],
                            ),
                            _sg(
                                "SAW CUT",
                                [
                                    _item(
                                        "SAW CUT & REPAIR CONCRETE FLOOR FOR CONDUIT INSTALLATION",
                                        20.26,
                                        "FT",
                                    )
                                ],
                            ),
                        ],
                    ),
                    _group(
                        "SWITCHGEAR",
                        [
                            _sg(
                                "",
                                [
                                    _item(
                                        "PANEL A, 277/480V, 3-PH, 4W, 200A MLO",
                                        1,
                                        caveat="(RATING ASSUMED)",
                                    )
                                ],
                            )
                        ],
                    ),
                    _group(
                        "LOW VOLTAGE WORK",
                        [
                            _sg("", [_item("TELE/DATA OUTLET", 78)]),
                            _sg(
                                "LOW VOLTAGE WIRING",
                                [
                                    _item('3/4"C EMT', 984, "FT", wastage=True),
                                    _item("CAT6 CABLE", 11964.6, "FT", wastage=True),
                                ],
                            ),
                        ],
                    ),
                ],
            },
        ],
    }


def _boq_spec(areas=None, closing=()):
    return {
        "title": "FIVE STAR TAX RESOLUTION, 8645 W DEER SPRINGS WAY, SUITE 200",
        "subtitle": "LAS VEGAS, NV 89149",
        "areas": areas if areas is not None else [_area()],
        "closing_notes": list(closing),
    }


def _estimate_spec(**over):
    spec = {
        "project_name": "FIVE STAR TAX RESOLUTION",
        "address": "LAS VEGAS, NV 89149",
        "include_standard_allowances": True,
        "extra_allowances": [],
        "general_notes": ["NEED TO CONFIRM SCOPE OF WORK FOR NV ENERGY TRANSFORMER."],
        "area_notes": [],
    }
    spec.update(over)
    return spec


def _load(data: bytes):
    return openpyxl.load_workbook(io.BytesIO(data))


def _rows(ws, desc_col=2):
    """[(row, description)] for every non-empty description cell from row 5."""
    out = []
    for r in range(5, ws.max_row + 1):
        v = ws.cell(row=r, column=desc_col).value
        if v not in (None, ""):
            out.append((r, v))
    return out


def _find(ws, text, col=2):
    for r in range(1, ws.max_row + 1):
        if ws.cell(row=r, column=col).value == text:
            return r
    raise AssertionError(f"{text!r} not found in column {col}")


def _all_text(wb) -> str:
    parts = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if c.value is not None:
                    parts.append(str(c.value))
    return "\n".join(parts)


# Quantities and fittings -----------------------------------------------------


def test_boq_quantity_applies_wastage_only_on_ft_rows():
    assert prr.boq_quantity(_item("#12 THHN", 12971, "FT", wastage=True)) == 14268.1
    assert prr.boq_quantity(_item("#12 THHN", 100.123, "FT", wastage=False)) == 100.12
    assert prr.boq_quantity(_item("OUTLET", 7, "EA", wastage=True)) == 7
    assert prr.estimate_wastage(_item("x", 1, "FT", wastage=True)) == 0.1
    assert prr.estimate_wastage(_item("x", 1, "EA", wastage=True)) == 0.0
    with pytest.raises(ValueError):
        prr.boq_quantity(_item("BAD", "lots"))


def test_parse_conduit():
    assert prr.parse_conduit('3/4"C EMT') == ("3/4", "EMT")
    assert prr.parse_conduit('1-1/4"C RGS') == ("1-1/4", "RGS")
    assert prr.parse_conduit('4"C PVC WITH PULL STRING (FOR NVE CONDUCTORS)') == ("4", "PVC")
    assert prr.parse_conduit("#12 THHN/THWN Cu") is None
    assert prr.parse_conduit('3/4" FLEX') is None


def test_fitting_ratios_match_sent_boqs():
    # Five Star: 3/4"C EMT 8283.715 ft -> 166 / 829 / 829.
    rows, straps = prr.conduit_fittings('3/4"C EMT', 8283.715)
    assert rows == [
        ('3/4"C EMT Compression Conn Stl', 166, "EA"),
        ('3/4"C EMT Compression Coupling Stl', 829, "EA"),
        ('3/4"C EMT 1-H Strap - Steel', 829, "EA"),
    ]
    assert straps == 829
    # YMCA: 2-1/2"C PVC 190.421 ft -> elbow 3, locknut 4, coupling 20, adapter 4.
    rows, straps = prr.conduit_fittings('2-1/2"C PVC SCH 40', 190.421)
    assert [r[1] for r in rows] == [3, 4, 20, 4]
    assert rows[0][0] == '2-1/2" ELBOW 90 DEG - RMC - GALV'
    assert straps == 0
    # Traffic Signal: 3"C PVC 1191.443 ft -> 18 / 24 / 120 / 24.
    assert [r[1] for r in prr.conduit_fittings('3"C PVC', 1191.443)[0]] == [18, 24, 120, 24]
    # Float noise does not add a coupling: 110 ft is exactly 11.
    assert prr.conduit_fittings('1"C EMT', 100 * 1.1)[0][1][1] == 11
    # Five Star OTHER CONDUIT ACCESSORIES: 876 straps -> 438 / 438 / 88.
    assert prr.other_conduit_accessories(876) == [
        (prr.ANCHOR_DESC, 438, "EA"),
        (prr.SCREW_DESC, 438, "EA"),
        (prr.SQ_BOX_DESC, 88, "EA"),
    ]
    assert prr.other_conduit_accessories(0) == []


# BOQ workbook ----------------------------------------------------------------


def test_boq_layout_matches_sent_template():
    wb = _load(
        prr.render_boq_xlsx(_boq_spec(closing=["Verify field conditions \u2014 before bid."]))
    )
    assert wb.sheetnames == ["Sheet1", "SHEET"]
    assert wb["Sheet1"].max_row == 1 and wb["Sheet1"]["A1"].value is None
    ws = wb["SHEET"]

    t = ws["B2"]
    assert (
        t.value
        == "FIVE STAR TAX RESOLUTION, 8645 W DEER SPRINGS WAY, SUITE 200\nLAS VEGAS, NV 89149"
    )
    assert t.font.b and t.font.sz == 12 and t.font.name == "Calibri"
    assert t.alignment.horizontal == "center" and t.alignment.wrap_text

    assert [ws.cell(row=4, column=c).value for c in range(1, 5)] == [
        "SR.\nNO.",
        "DESCRIPTION",
        "QUANTITY",
        "UNIT",
    ]
    for c in range(1, 5):
        cell = ws.cell(row=4, column=c)
        assert (
            cell.fill.fgColor.rgb == "FF2DC8FF" and cell.font.b and cell.border.left.style == "thin"
        )
    widths = {k: round(ws.column_dimensions[k].width, 1) for k in "ABCD"}
    assert widths == {"A": 6.5, "B": 68.2, "C": 9.5, "D": 6.8}

    demo = _find(ws, "DEMOLITION")
    assert demo == 5 and ws.cell(row=5, column=2).fill.fgColor.rgb == "FFFFFF00"
    assert ws["A5"].value == '=IF(C5<>"",1+MAX(A$4:A4),"")'
    new_work = _find(ws, "NEW WORK")
    assert ws.cell(row=new_work, column=2).fill.fgColor.rgb == "FFFFFF00"
    assert ws.cell(row=new_work - 1, column=2).value is None  # blank row between sections

    wiring = _find(ws, f"WIRING\n{WIRING_WASTAGE_NOTE}")
    wf = ws.cell(row=wiring, column=2).fill.fgColor
    assert wf.type == "theme" and wf.theme == 3 and round(wf.tint, 1) == 0.4
    sub = ws.cell(row=_find(ws, "CONDUCTORS (AWG)"), column=2)
    assert sub.alignment.horizontal == "left" and round(sub.fill.fgColor.tint, 1) == 0.8
    assert _find(ws, f"LOW VOLTAGE WORK\n{LOW_VOLTAGE_NOTE}")

    # Wastage on FT wiring rows, none on EA rows or the saw cut.
    r = _find(ws, "#12 THHN/THWN Cu")
    assert ws.cell(row=r, column=3).value == 14268.1 and ws.cell(row=r, column=4).value == "FT"
    assert ws.cell(row=r, column=1).value == f'=IF(C{r}<>"",1+MAX(A$5:A{r - 1}),"")'
    # Fractional quantities show their decimals (the sent files' "0" would
    # print 14268); whole ones keep "0".
    assert ws.cell(row=r, column=3).number_format == "0.##"
    panel = _find(ws, "PANEL A, 277/480V, 3-PH, 4W, 200A MLO (RATING ASSUMED)")
    assert ws.cell(row=panel, column=3).number_format == "0"
    saw = _find(ws, "SAW CUT & REPAIR CONCRETE FLOOR FOR CONDUIT INSTALLATION")
    assert ws.cell(row=saw, column=3).value == 20.26

    # EMT fittings right under the run, unnumbered and right-aligned.
    emt = _find(ws, '3/4"C EMT')
    assert ws.cell(row=emt, column=3).value == 8283.72
    for offset, (desc, qty) in enumerate(
        [
            ('3/4"C EMT Compression Conn Stl', 166),
            ('3/4"C EMT Compression Coupling Stl', 829),
            ('3/4"C EMT 1-H Strap - Steel', 829),
        ],
        start=1,
    ):
        row = emt + offset
        assert ws.cell(row=row, column=2).value == desc
        assert ws.cell(row=row, column=3).value == qty
        assert ws.cell(row=row, column=4).value == "EA"
        assert ws.cell(row=row, column=1).value is None
        assert ws.cell(row=row, column=2).alignment.horizontal == "right"

    # CONDUITS gets a headed anchors block: straps 829 + 41 = 870 -> 435 / 87.
    acc = _find(ws, "OTHER CONDUIT ACCESSORIES")
    assert ws.cell(row=acc, column=1).value is None
    assert round(ws.cell(row=acc, column=2).fill.fgColor.tint, 1) == 0.6
    assert [ws.cell(row=acc + i, column=3).value for i in (1, 2, 3)] == [435, 435, 87]
    assert ws.cell(row=acc + 1, column=2).value == prr.ANCHOR_DESC

    # PVC fittings: 90.88 * 1.1 = 99.97 ft -> elbow 2, locknut 2, coupling 10, adapter 2.
    pvc = _find(ws, '2"C PVC SCH 40')
    assert [ws.cell(row=pvc + i, column=3).value for i in (1, 2, 3, 4)] == [2, 2, 10, 2]
    assert ws.cell(row=pvc + 1, column=2).value == '2" ELBOW 90 DEG - RMC - GALV'

    # LOW VOLTAGE WIRING: the anchors block is unheaded, after the EMT fittings
    # and before the CAT6 row (Five Star layout): 109 straps -> 55 / 55 / 11.
    lv = [r for r, v in _rows(ws) if v == '3/4"C EMT'][1]
    assert ws.cell(row=lv + 4, column=2).value == prr.ANCHOR_DESC
    assert [ws.cell(row=lv + i, column=3).value for i in (4, 5, 6)] == [55, 55, 11]
    assert ws.cell(row=lv + 7, column=2).value == "CAT6 CABLE"

    # Caveat inline; switchgear items sit straight under the group heading.
    panel = _find(ws, "PANEL A, 277/480V, 3-PH, 4W, 200A MLO (RATING ASSUMED)")
    assert ws.cell(row=panel - 1, column=2).value == "SWITCHGEAR"

    # Closing NOTE row, cleaned of the em dash.
    note_rows = [v for _, v in _rows(ws) if str(v).startswith("NOTE:")]
    assert note_rows == ["NOTE:\nVerify field conditions - before bid."]
    assert "\u2014" not in _all_text(wb) and "\u2013" not in _all_text(wb)
    assert ws.print_area


def test_boq_multi_area_sheet_names_are_sanitized_and_unique():
    long_name = "JONES BLVD AND SHELBOURNE AVENUE NORTH EXTENSION"
    areas = [
        _area("FORT APACHE / POST: ROAD [1]"),
        _area("FORD AVENUE"),
        _area("ford avenue"),
        _area(long_name),
        _area("Sheet1"),
    ]
    wb = _load(prr.render_boq_xlsx(_boq_spec(areas=areas)))
    names = wb.sheetnames
    assert names[0] == "Sheet1"
    assert names[1] == "FORT APACHE POST ROAD 1"
    assert names[2] == "FORD AVENUE" and names[3] == "ford avenue (2)"
    assert names[4] == long_name[:31].rstrip() and len(names[4]) <= 31
    assert names[5] == "Sheet1 (2)"
    for n in names[1:]:
        assert not set(n) & set("[]:*?/\\")
        assert wb[n]["B4"].value == "DESCRIPTION"


def test_boq_respects_spec_fittings_and_spec_accessory_subgroup():
    area = {
        "area": "MAIN",
        "notes": ["Area note."],
        "sections": [
            {
                "heading": "NEW WORK",
                "note": None,
                "groups": [
                    _group(
                        "WIRING",
                        [
                            _sg(
                                "CONDUITS",
                                [
                                    _item('1"C EMT', 100, "FT", wastage=True),
                                    _item('1"C EMT Compression Conn Stl', 5, accessory=True),
                                    _item('1"C EMT 1-H Strap - Steel', 12, accessory=True),
                                ],
                            ),
                            _sg(
                                "OTHER CONDUIT ACCESSORIES",
                                [_item(prr.ANCHOR_DESC, 6, accessory=True)],
                            ),
                        ],
                    )
                ],
            }
        ],
    }
    ws = _load(prr.render_boq_xlsx(_boq_spec(areas=[area])))["SHEET"]
    descs = [v for _, v in _rows(ws)]
    # The spec's own fittings are used, nothing generated next to them.
    assert descs.count('1"C EMT Compression Conn Stl') == 1
    assert '1"C EMT Compression Coupling Stl' not in descs
    assert descs.count("OTHER CONDUIT ACCESSORIES") == 1
    assert descs.count(prr.ANCHOR_DESC) == 1
    conn = _find(ws, '1"C EMT Compression Conn Stl')
    assert ws.cell(row=conn, column=1).value is None
    assert ws.cell(row=_find(ws, prr.ANCHOR_DESC), column=3).value == 6
    assert "NOTE:\nArea note." in descs


def test_boq_neutralizes_formula_text_and_rejects_empty_specs():
    area = {
        "area": "MAIN",
        "notes": [],
        "sections": [
            {
                "heading": "NEW WORK",
                "note": None,
                "groups": [
                    _group(
                        "OTHER ITEMS",
                        [_sg("MISCELLANEOUS", [_item('=HYPERLINK("http://x","y")', 1)])],
                    )
                ],
            }
        ],
    }
    area["sections"][0]["groups"][0]["subgroups"][0]["items"].append(_item("-48V DC SUPPLY", 2))
    ws = _load(prr.render_boq_xlsx(_boq_spec(areas=[area])))["SHEET"]
    # Stored as plain text with Excel's quote prefix: never evaluated, and no
    # literal apostrophe on the deliverable.
    link = _find(ws, '=HYPERLINK("http://x","y")')
    cell = ws.cell(row=link, column=2)
    assert cell.data_type == "s" and cell.quotePrefix
    dc = ws.cell(row=_find(ws, "-48V DC SUPPLY"), column=2)
    assert dc.data_type == "s" and dc.quotePrefix
    plain = ws.cell(row=_find(ws, "MISCELLANEOUS"), column=2)
    assert not plain.quotePrefix
    with pytest.raises(ValueError, match="no areas"):
        prr.render_boq_xlsx(_boq_spec(areas=[]))
    with pytest.raises(ValueError):
        prr.render_boq_xlsx("not a spec")  # type: ignore[arg-type]


# Estimate workbook -----------------------------------------------------------


def test_estimate_layout_matches_sent_template():
    wb = _load(prr.render_estimate_xlsx(_boq_spec(), _estimate_spec(), date=DATE))
    assert wb.sheetnames == ["Sheet1", "Detailed Estimate", "Bid Recap & Summary"]
    ws = wb["Detailed Estimate"]

    assert (ws["D2"].value, ws["E2"].value) == ("PROJECT", "FIVE STAR TAX RESOLUTION")
    assert ws["E2"].font.b and ws["E2"].font.color.rgb == "FFC00000"
    assert (ws["D3"].value, ws["E3"].value) == ("ESTIMATE", "ELECTRICAL ESTIMATE")
    assert (ws["D4"].value, ws["E4"].value) == ("ADDRESS", "LAS VEGAS, NV 89149")
    assert ws["A5"].value == "DATE: "
    assert (
        ws["B5"].value == datetime.datetime(2026, 10, 17) and ws["B5"].number_format == "mm-dd-yy"
    )
    assert ws["L4"].value == "LABOR RATE" and ws["N4"].value is None

    headers = [ws.cell(row=7, column=c).value for c in range(1, 16)]
    assert headers[:7] == [
        "SR.\nNO.",
        "DRAWING SHEET#",
        "DESCRIPTION",
        "QUANTITY",
        "WASTAGE",
        "QTY WITH\nWASTAGE",
        "UNIT",
    ]
    assert headers[7:] == list(prr.EST_HEADERS[7:])
    assert all(ws.cell(row=7, column=c).fill.fgColor.rgb == "FF2DC8FF" for c in range(1, 16))

    # Pre-wastage quantity in D, wastage in E, F = D*(1+E).
    r = _find(ws, "#12 THHN/THWN Cu", col=3)
    assert ws.cell(row=r, column=4).value == 12971
    assert ws.cell(row=r, column=5).value == 0.1 and ws.cell(row=r, column=5).number_format == "0%"
    assert ws.cell(row=r, column=6).value == f"=D{r}*(1+E{r})"
    assert ws.cell(row=r, column=7).value == "FT"
    assert ws.cell(row=r, column=1).value == f'=IF(D{r}<>"",1+MAX(A$8:A{r - 1}),"")'
    assert ws.cell(row=r, column=2).value == "E1.00, E1.01"
    saw = _find(ws, "SAW CUT & REPAIR CONCRETE FLOOR FOR CONDUIT INSTALLATION", col=3)
    assert ws.cell(row=saw, column=5).value == 0
    assert ws["A8"].value == '=IF(D8<>"",1+MAX(A$7:A7),"")'

    # DRAWING SHEET# merged across the subgroup's rows.
    emt = _find(ws, '3/4"C EMT', col=3)
    assert f"B{emt}:B{emt + 1}" in {str(m) for m in ws.merged_cells.ranges}
    assert ws.cell(row=emt, column=2).value == "E-101, E1.02"

    # No fitting rows in the estimate.
    descs = [ws.cell(row=r, column=3).value for r in range(8, ws.max_row + 1)]
    assert not any(
        "Compression" in str(d) or "ELBOW" in str(d) or d == prr.ANCHOR_DESC for d in descs
    )
    assert "OTHER CONDUIT ACCESSORIES" not in descs

    # Subtotal label rows after DEMOLITION (as one block) and each NEW WORK group.
    subtotals = [
        r
        for r in range(8, ws.max_row + 1)
        if ws.cell(row=r, column=16).value == "SUBTOTAL MATERIAL"
    ]
    assert len(subtotals) == 4
    for r in subtotals:
        assert [ws.cell(row=r, column=c).value for c in (16, 17, 18)] == list(prr.SUBTOTAL_LABELS)
        assert ws.cell(row=r, column=16).fill.fgColor.rgb == "FF6DD9FF"
        assert ws.cell(row=r + 1, column=16).value is None

    # Pricing cells exist but carry nothing; no number anywhere in H..R.
    for row in ws.iter_rows(min_row=8, min_col=8, max_col=18):
        for c in row:
            assert c.value is None or c.value in prr.SUBTOTAL_LABELS, c.coordinate

    # Allowances: the nine fixed rows, unit LS, no amount.
    allow = _find(ws, "ALLOWANCES", col=3)
    rows = [ws.cell(row=allow + 1 + i, column=3).value for i in range(len(ESTIMATE_ALLOWANCES))]
    assert rows == list(ESTIMATE_ALLOWANCES)
    assert all(ws.cell(row=allow + 1 + i, column=7).value == "LS" for i in range(9))
    assert all(ws.cell(row=allow + 1 + i, column=4).value is None for i in range(9))

    note = allow + 1 + len(ESTIMATE_ALLOWANCES) + 1
    assert ws.cell(row=note, column=1).value == "NOTE:"
    assert ws.cell(row=note, column=1).fill.fgColor.rgb == "FFFFFF00"
    assert (
        ws.cell(row=note, column=2).value
        == "NEED TO CONFIRM SCOPE OF WORK FOR NV ENERGY TRANSFORMER."
    )
    assert f"B{note}:O{note}" in {str(m) for m in ws.merged_cells.ranges}

    # Bid Recap & Summary.
    rc = wb["Bid Recap & Summary"]
    assert rc["B2"].value == "BID RECAP" and rc["B2"].fill.fgColor.rgb == "FF00B0F0"
    assert [rc.cell(row=3, column=c).value for c in range(2, 12)] == list(prr.RECAP_HEADERS)
    labels = [rc.cell(row=r, column=3).value for r in range(4, 9)]
    assert labels == ["DEMOLITION", "WIRING", "SWITCHGEAR", "LOW VOLTAGE WORK", "ALLOWANCES"]
    assert rc["B4"].value == "=1+MAX($B$3:B3)"
    assert rc["C10"].value == "TOTALS"
    assert rc["B13"].value == "BID SUMMARY"
    assert rc["C14"].value == "TOTAL MATERIAL COST" and rc["C24"].value == "BASE BID PRICE"
    assert rc["C26"].value == prr.ESTIMATE_DISCLAIMER
    assert "We does not warranty" in prr.ESTIMATE_DISCLAIMER  # verbatim from the sent files
    for row in rc.iter_rows(min_row=4, min_col=4, max_col=11):
        for c in row:
            assert c.value is None, c.coordinate
    assert "\u2014" not in _all_text(wb)


def test_estimate_six_groups_put_the_disclaimer_in_c28():
    groups = [
        _group(name, [_sg("", [_item(f"{name} ITEM", 1)])])
        for name in (
            "WIRING",
            "SWITCHGEAR",
            "OTHER ITEMS",
            "LIGHTING FIXTURES",
            "LIGHTING CONTROLS",
            "LOW VOLTAGE WORK",
        )
    ]
    area = {
        "area": "MAIN",
        "notes": [],
        "sections": [{"heading": "NEW WORK", "note": None, "groups": groups}],
    }
    rc = _load(prr.render_estimate_xlsx(_boq_spec(areas=[area]), _estimate_spec(), date=DATE))[
        "Bid Recap & Summary"
    ]
    # Same positions as the Five Star sent file: ALLOWANCES C10, TOTALS C12, disclaimer C28.
    assert rc["C10"].value == "ALLOWANCES" and rc["C12"].value == "TOTALS"
    assert rc["C28"].value.startswith("Terms and Conditions/Disclaimer:")


def test_estimate_multi_area_recap_keeps_group_labels_and_routes_area_notes():
    spec = _boq_spec(areas=[_area("SILVERADO RANCH"), _area("BADURA AVE & TENAYA WAY")])
    est = _estimate_spec(
        general_notes=[],
        extra_allowances=["TRAFFIC CONTROL"],
        include_standard_allowances=False,
        area_notes=[{"area": "Silverado Ranch", "notes": ["Special scope, review carefully."]}],
    )
    wb = _load(prr.render_estimate_xlsx(spec, est, date=DATE))
    assert wb.sheetnames == [
        "Sheet1",
        "SILVERADO RANCH",
        "BADURA AVE & TENAYA WAY",
        "Bid Recap & Summary",
    ]
    a = wb["SILVERADO RANCH"]
    allow = _find(a, "ALLOWANCES", col=3)
    assert a.cell(row=allow + 1, column=3).value == "TRAFFIC CONTROL"
    assert a.cell(row=allow + 2, column=3).value is None
    note = _find(a, "NOTE:", col=1)
    assert a.cell(row=note, column=2).value == "Special scope, review carefully."
    b = wb["BADURA AVE & TENAYA WAY"]
    nb = _find(b, "NOTE:", col=1)
    assert b.cell(row=nb, column=2).value is None
    # One recap row per group across both areas (the WIRING label General
    # Material extraction needs), then ALLOWANCES.
    rc = wb["Bid Recap & Summary"]
    assert [rc.cell(row=r, column=3).value for r in range(4, 9)] == [
        "DEMOLITION",
        "WIRING",
        "SWITCHGEAR",
        "LOW VOLTAGE WORK",
        "ALLOWANCES",
    ]
    assert rc.cell(row=9, column=3).value is None


def _one_section_area(name, groups):
    return {
        "area": name,
        "notes": [],
        "sections": [{"heading": "NEW WORK", "note": None, "groups": groups}],
    }


def test_fractional_quantities_display_as_fractions():
    area = _one_section_area(
        "MAIN",
        [
            _group(
                "OTHER ITEMS",
                [
                    _sg(
                        "TRENCHING",
                        [
                            _item("CONCRETE ENCASEMENT", 0.4, "CY"),
                            _item("BACKFILL", 2.25, "CU FT"),
                            _item("PULL BOX", 3),
                        ],
                    )
                ],
            )
        ],
    )
    ws = _load(prr.render_boq_xlsx(_boq_spec(areas=[area])))["SHEET"]
    assert ws.cell(row=_find(ws, "CONCRETE ENCASEMENT"), column=3).number_format == "0.##"
    assert ws.cell(row=_find(ws, "BACKFILL"), column=3).number_format == "0.##"
    assert ws.cell(row=_find(ws, "PULL BOX"), column=3).number_format == "0"
    est = _load(prr.render_estimate_xlsx(_boq_spec(areas=[area]), _estimate_spec(), date=DATE))
    es = est["Detailed Estimate"]
    r = _find(es, "CONCRETE ENCASEMENT", col=3)
    assert es.cell(row=r, column=4).value == 0.4
    assert es.cell(row=r, column=4).number_format == "0.##"
    assert es.cell(row=r, column=6).number_format == "0.##"
    p = _find(es, "PULL BOX", col=3)
    assert es.cell(row=p, column=4).number_format == "0"
    assert es.cell(row=p, column=6).number_format == "0"


def test_estimate_skips_groups_with_only_accessory_rows():
    area = _one_section_area(
        "MAIN",
        [
            _group(
                "WIRING",
                [
                    _sg(
                        "OTHER CONDUIT ACCESSORIES",
                        [_item(prr.ANCHOR_DESC, 6, accessory=True)],
                    ),
                    _sg("", [_item('1"C EMT Compression Conn Stl', 2, accessory=True)]),
                ],
            ),
            _group("OTHER ITEMS", [_sg("", [_item("PULL BOX", 3)])]),
        ],
    )
    wb = _load(prr.render_estimate_xlsx(_boq_spec(areas=[area]), _estimate_spec(), date=DATE))
    ws = wb["Detailed Estimate"]
    descs = [ws.cell(row=r, column=3).value for r in range(8, ws.max_row + 1)]
    assert not any(str(d).startswith("WIRING") for d in descs if d)
    subtotals = [
        r
        for r in range(8, ws.max_row + 1)
        if ws.cell(row=r, column=16).value == "SUBTOTAL MATERIAL"
    ]
    assert len(subtotals) == 1
    rc = wb["Bid Recap & Summary"]
    assert [rc.cell(row=r, column=3).value for r in (4, 5, 6)] == [
        "OTHER ITEMS",
        "ALLOWANCES",
        None,
    ]
    # The BOQ still lists the accessory rows.
    boq = _load(prr.render_boq_xlsx(_boq_spec(areas=[area])))["SHEET"]
    assert _find(boq, prr.ANCHOR_DESC)


def test_single_area_estimate_takes_every_area_note():
    est = _estimate_spec(
        general_notes=[],
        area_notes=[{"area": "Main Building", "notes": ["Verify service size with NV Energy."]}],
    )
    wb = _load(prr.render_estimate_xlsx(_boq_spec(areas=[_area("A")]), est, date=DATE))
    ws = wb["Detailed Estimate"]
    note = _find(ws, "NOTE:", col=1)
    assert ws.cell(row=note, column=2).value == "Verify service size with NV Energy."


def test_multi_area_estimate_keeps_unmatched_area_notes(caplog):
    spec = _boq_spec(areas=[_area("BLD A"), _area("BLD B")])
    est = _estimate_spec(
        general_notes=[],
        area_notes=[
            {"area": "bld-a", "notes": ["Matched loosely."]},
            {"area": "Parking Lot", "notes": ["Site lighting by others."]},
        ],
    )
    with caplog.at_level("WARNING"):
        wb = _load(prr.render_estimate_xlsx(spec, est, date=DATE))
    a, b = wb["BLD A"], wb["BLD B"]
    na, nb = _find(a, "NOTE:", col=1), _find(b, "NOTE:", col=1)
    assert a.cell(row=na, column=2).value == "Matched loosely."
    assert a.cell(row=na + 1, column=2).value == "Parking Lot: Site lighting by others."
    assert b.cell(row=nb, column=2).value == "Parking Lot: Site lighting by others."
    assert "matches no BOQ area" in caplog.text


def test_multi_area_recap_unions_group_labels_for_general_material():
    first = _one_section_area("NORTH", [_group("WIRING", [_sg("", [_item("#12 THHN", 10, "FT")])])])
    second = _one_section_area(
        "SOUTH",
        [
            _group("WIRING", [_sg("", [_item("#10 THHN", 10, "FT")])]),
            _group("OTHER ITEMS", [_sg("", [_item("PULL BOX", 2)])]),
        ],
    )
    wb = _load(
        prr.render_estimate_xlsx(_boq_spec(areas=[first, second]), _estimate_spec(), date=DATE)
    )
    rc = wb["Bid Recap & Summary"]
    labels = [rc.cell(row=r, column=3).value for r in range(4, 8)]
    assert labels == ["WIRING", "OTHER ITEMS", "ALLOWANCES", None]


# Markup PDF ------------------------------------------------------------------


def _source_pdf(sizes, rotate=0) -> bytes:
    w = PdfWriter()
    for width, height in sizes:
        page = w.add_blank_page(width=width, height=height)
        if rotate:
            page.rotate(rotate)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _annots(page):
    return [a.get_object() for a in page.get("/Annots") or []]


def test_markup_pdf_copies_referenced_pages_and_adds_annotations():
    src = _source_pdf([(612, 792), (3024, 2160), (612, 792)])
    sheets = [
        {
            "kind": "trench",
            "page": 2,
            "label": "TRENCH",
            "notes": "2 FT DEEP X 1 FT WIDE",
            "lines": [
                {
                    "points": [[0.25, 0.5], [0.5, 0.5], [0.5, 0.75]],
                    "length_ft": 120,
                    "label": "TRENCH",
                }
            ],
        }
    ]
    out = PdfReader(io.BytesIO(prr.render_markup_pdf(src, sheets, kind="trench")))
    assert len(out.pages) == 1
    page = out.pages[0]
    assert float(page.mediabox.width) == 3024
    annots = _annots(page)
    lines = [a for a in annots if a["/Subtype"] == "/Line"]
    texts = [a for a in annots if a["/Subtype"] == "/FreeText"]
    assert len(lines) == 2 and len(texts) == 1
    # Fractions to user space, origin bottom-left: y = 2160 * (1 - fy).
    assert [float(v) for v in lines[0]["/L"]] == pytest.approx([756, 1080, 1512, 1080])
    assert [float(v) for v in lines[1]["/L"]] == pytest.approx([1512, 1080, 1512, 540])
    for ln in lines:
        assert [float(v) for v in ln["/C"]] == [1, 0, 0]
        assert float(ln["/BS"]["/W"]) == prr.MARKUP_LINE_WIDTH
        assert "/N" in ln["/AP"]
        assert ln["/Contents"] == "TRENCH 120 FT"
    label = texts[0]
    assert label["/Contents"] == "TRENCH\n2 FT DEEP X 1 FT WIDE"
    assert "1 0 0 rg" in label["/DA"] and "/Helv 36 Tf" in label["/DA"]
    ap = label["/AP"]["/N"].get_object()
    assert b"(TRENCH) Tj" in ap.get_data()
    rect = [float(v) for v in label["/Rect"]]
    assert rect[0] == pytest.approx(756) and rect[1] > 1080  # just above the first point


def test_markup_pdf_shares_a_page_and_handles_rotation():
    src = _source_pdf([(2160, 3024)], rotate=90)
    sheets = [
        {
            "kind": "sawcut",
            "page": 1,
            "label": "SAWCUT",
            "notes": None,
            "lines": [{"points": [[0.1, 0.1], [0.9, 0.1]], "length_ft": None, "label": None}],
        },
        {
            "kind": "sawcut",
            "page": 1,
            "label": "",
            "notes": None,
            "lines": [{"points": [[0.1, 0.9], [0.2, 0.9]], "length_ft": 5, "label": None}],
        },
    ]
    out = PdfReader(io.BytesIO(prr.render_markup_pdf(src, sheets, kind="sawcut_markup")))
    assert len(out.pages) == 1
    lines = [a for a in _annots(out.pages[0]) if a["/Subtype"] == "/Line"]
    assert len(lines) == 2
    # /Rotate 90: displayed (fx, fy) -> user (fy * w, fx * h) with w=2160, h=3024.
    assert [float(v) for v in lines[0]["/L"]] == pytest.approx([216, 302.4, 216, 2721.6])
    texts = [a for a in _annots(out.pages[0]) if a["/Subtype"] == "/FreeText"]
    assert len(texts) == 1
    matrix = [float(v) for v in texts[0]["/AP"]["/N"].get_object()["/Matrix"]]
    assert matrix == pytest.approx([0, 1, -1, 0, 0, 0])  # counter-rotated to read upright


def test_markup_pdf_rejects_bad_input():
    src = _source_pdf([(612, 792)])

    def one(page=1, kind="trench"):
        return [{"kind": kind, "page": page, "label": "X", "lines": [], "notes": None}]

    with pytest.raises(ValueError, match="outside the source drawing"):
        prr.render_markup_pdf(src, one(3), kind="trench")
    with pytest.raises(ValueError, match="no markup sheets"):
        prr.render_markup_pdf(src, [], kind="trench")
    with pytest.raises(ValueError, match="could not be read"):
        prr.render_markup_pdf(b"not a pdf", one(), kind="trench")
    with pytest.raises(ValueError, match="no sawcut markup sheets"):
        prr.render_markup_pdf(src, one(), kind="sawcut")
    with pytest.raises(ValueError, match="trench or sawcut"):
        prr.render_markup_pdf(src, one(kind="conduit"), kind="trench")
    with pytest.raises(ValueError, match="trench or sawcut"):
        prr.render_markup_pdf(src, one(), kind="boq")


def _sources_pdf(widths) -> bytes:
    return _source_pdf([(w, 792) for w in widths])


def _msheet(kind, page, label, *, doc_index=1, document_id=None, points=((0.2, 0.5), (0.6, 0.5))):
    sheet = {
        "kind": kind,
        "doc_index": doc_index,
        "page": page,
        "sheet_number": None,
        "label": label,
        "notes": None,
        "lines": [{"points": [list(p) for p in points], "length_ft": 10, "label": label}],
    }
    if document_id is not None:
        sheet["document_id"] = document_id
    return sheet


def _labels(reader):
    return [
        a["/Contents"] for p in reader.pages for a in _annots(p) if a["/Subtype"] == "/FreeText"
    ]


def test_markup_pdf_filters_by_kind_across_documents():
    doc_a = _sources_pdf([612, 1224])
    whole_b = _sources_pdf([1000, 2000, 3024])  # document B starts at page 2
    sheets = [
        _msheet("trench", 2, "TRENCH", doc_index=1, document_id="doc-a"),
        _msheet("sawcut", 1, "SAWCUT", doc_index=2, document_id="doc-b"),
        _msheet("trench", 2, "TRENCH (Need to confirm)", doc_index=2, document_id="doc-b"),
    ]
    assert prr.markup_documents(sheets, kind="trench") == ["doc-a", "doc-b"]
    assert prr.markup_documents(sheets, kind="sawcut_markup") == ["doc-b"]
    sources = {"doc-a": doc_a, "doc-b": (whole_b, 2)}

    trench = PdfReader(io.BytesIO(prr.render_markup_pdf(sources, sheets, kind="trench")))
    assert [float(p.mediabox.width) for p in trench.pages] == [1224, 3024]
    assert _labels(trench) == ["TRENCH", "TRENCH (Need to confirm)"]

    sawcut = PdfReader(io.BytesIO(prr.render_markup_pdf(sources, sheets, kind="sawcut")))
    assert [float(p.mediabox.width) for p in sawcut.pages] == [2000]
    assert _labels(sawcut) == ["SAWCUT"]

    # doc_index keys work when the sheets carry no document_id.
    plain = [{k: v for k, v in sh.items() if k != "document_id"} for sh in sheets]
    by_index = {1: doc_a, 2: (whole_b, 2)}
    out = PdfReader(io.BytesIO(prr.render_markup_pdf(by_index, plain, kind="trench")))
    assert [float(p.mediabox.width) for p in out.pages] == [1224, 3024]

    with pytest.raises(ValueError, match="more than one document"):
        prr.render_markup_pdf(doc_a, sheets, kind="trench")
    with pytest.raises(ValueError, match="was not provided"):
        prr.render_markup_pdf({"doc-a": doc_a}, sheets, kind="trench")
    with pytest.raises(ValueError, match="outside the source drawing"):
        prr.render_markup_pdf({"doc-a": doc_a, "doc-b": (whole_b, 3)}, sheets, kind="trench")


@pytest.mark.parametrize(
    ("rotation", "expected_l", "matrix"),
    [
        # /Rotate 180: displayed (fx, fy) -> user ((1 - fx) * w, fy * h).
        (180, [1944, 302.4, 216, 302.4], [-1, 0, 0, -1, 0, 0]),
        # /Rotate 270: displayed (fx, fy) -> user ((1 - fy) * w, (1 - fx) * h).
        (270, [1944, 2721.6, 1944, 302.4], [0, -1, 1, 0, 0, 0]),
    ],
)
def test_markup_pdf_rotated_180_and_270(rotation, expected_l, matrix):
    src = _source_pdf([(2160, 3024)], rotate=rotation)
    sheets = [_msheet("trench", 1, "TRENCH", points=((0.1, 0.1), (0.9, 0.1)))]
    out = PdfReader(io.BytesIO(prr.render_markup_pdf(src, sheets, kind="trench")))
    annots = _annots(out.pages[0])
    line = next(a for a in annots if a["/Subtype"] == "/Line")
    assert [float(v) for v in line["/L"]] == pytest.approx(expected_l)
    label = next(a for a in annots if a["/Subtype"] == "/FreeText")
    m = [float(v) for v in label["/AP"]["/N"].get_object()["/Matrix"]]
    assert m == pytest.approx(matrix)
    x0, y0, x1, y1 = (float(v) for v in label["/Rect"])
    assert 0 <= x0 < x1 <= 2160 and 0 <= y0 < y1 <= 3024
    ax, ay = expected_l[0], expected_l[1]
    if rotation == 180:  # displayed "above" is user -y, box ends at the anchor x
        assert y1 < ay and x1 == pytest.approx(ax)
    else:  # 270: displayed "above" is user +x, box hangs down from the anchor y
        assert x0 > ax and y1 == pytest.approx(ay)


def test_markup_label_splits_lines_and_fits_the_page():
    src = _source_pdf([(612, 792)])
    notes_only = _msheet("trench", 1, "")
    notes_only["notes"] = "2 FT DEEP\nREPAIR ASPHALT"
    long_label = _msheet("trench", 1, "TRENCH " * 40)
    out = PdfReader(io.BytesIO(prr.render_markup_pdf(src, [notes_only, long_label], kind="trench")))
    labels = [a for a in _annots(out.pages[0]) if a["/Subtype"] == "/FreeText"]
    assert len(labels) == 2
    first = labels[0]["/AP"]["/N"].get_object().get_data()
    assert b"(2 FT DEEP) Tj" in first and b"(REPAIR ASPHALT) Tj" in first
    assert b"\n" not in first
    second = labels[1]
    data = second["/AP"]["/N"].get_object().get_data()
    assert data.count(b" Tj") > 1  # wrapped onto several lines
    x0, y0, x1, y1 = (float(v) for v in second["/Rect"])
    assert 0 <= x0 and x1 <= 612 and (x1 - x0) <= 612 * 0.9 + 0.01
    assert "/Helv 36 Tf" not in second["/DA"]  # shrunk from the default size
    # One font object for the document, registered in the AcroForm /DR.
    fonts = {
        a["/AP"]["/N"].get_object()["/Resources"]["/Font"].raw_get("/Helv").idnum for a in labels
    }
    assert len(fonts) == 1
    dr = out.trailer["/Root"]["/AcroForm"]["/DR"]["/Font"]
    assert dr.raw_get("/Helv").idnum in fonts


def test_markup_pdf_malformed_source_raises_app_error(monkeypatch):
    import pypdf

    src = _source_pdf([(612, 792)])

    def broken(self, stream):
        raise KeyError("/Kids")

    monkeypatch.setattr(pypdf.PdfWriter, "write", broken)
    with pytest.raises(ValueError, match="could not be processed"):
        prr.render_markup_pdf(src, [_msheet("trench", 1, "TRENCH")], kind="trench")


# Marked plans PDF ------------------------------------------------------------


def _png(w=1200, h=800) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


def _marked(**over):
    page = {
        "png_bytes": _png(),
        "area": "MAIN",
        "sheet_number": "E-101",
        "legend": [
            {
                "prefix": "PNL",
                "description": "PANEL A, 277/480V, 3-PH, 4W, 200A MLO TYPE, 14KAIC",
                "quantity": 1,
                "unit": "EA",
            },
            {"prefix": "LV", "description": "CONDUIT RUN", "quantity": 40.8, "unit": "FT"},
        ],
        "marks": [
            {"legend_index": 0, "kind": "dot", "points": [[0.5, 0.5]]},
            {"legend_index": 1, "kind": "polyline", "points": [[0.1, 0.9], [0.6, 0.9]]},
            {
                "legend_index": 7,
                "kind": "dot",
                "points": [[0.2, 0.2]],
            },  # outside the legend, ignored
        ],
    }
    page.update(over)
    return page


def test_marked_plans_pdf_page_count_and_size():
    data = prr.render_marked_plans_pdf(
        [_marked(), _marked(sheet_number="E-102"), _marked(legend=[], marks=[])]
    )
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 3
    for p in reader.pages:
        assert float(p.mediabox.width) == pytest.approx(792, abs=1)
        assert float(p.mediabox.height) == pytest.approx(612, abs=1)


def test_marked_plans_page_draws_dots_and_legend():
    pages = prr._marked_pages(_marked())
    assert len(pages) == 1
    img = pages[0]
    assert img.size == prr.MARKED_PAGE_PX
    # The sheet (1200x800) is scaled into the left 78%: the dot at the sheet
    # centre is tinted, not white.
    pw, ph = prr.MARKED_PAGE_PX
    box_w = int(pw * prr.MARKED_SHEET_FRACTION) - 40
    scale = min(box_w / 1200, (ph - 80) / 800)
    dw, dh = int(1200 * scale), int(800 * scale)
    cx, cy = 40 + dw // 2, 40 + ((ph - 80) - dh) // 2 + dh // 2
    assert img.getpixel((cx, cy)) != (255, 255, 255)
    # Legend swatch for row 0 is drawn in the first palette color.
    legend_x = pw - 40 - 16 - 5
    found = any(img.getpixel((legend_x, y))[:3] == prr.MARKED_PALETTE[0] for y in range(150, 320))
    assert found
    with pytest.raises(ValueError):
        prr.render_marked_plans_pdf([])
    with pytest.raises(ValueError):
        prr.render_marked_plans_pdf([_marked(png_bytes=b"")])


def _legend(n):
    return [
        {"prefix": f"P{i}", "description": f"DEVICE {i}", "quantity": i + 1, "unit": "EA"}
        for i in range(n)
    ]


def _dot_center(idx_point):
    pw, ph = prr.MARKED_PAGE_PX
    box_w = int(pw * prr.MARKED_SHEET_FRACTION) - 40
    scale = min(box_w / 1200, (ph - 80) / 800)
    dw, dh = int(1200 * scale), int(800 * scale)
    oy = 40 + ((ph - 80) - dh) // 2
    fx, fy = idx_point
    return int(40 + fx * dw), int(oy + fy * dh)


def test_marked_plans_number_rows_past_the_palette():
    points = {0: (0.2, 0.3), 16: (0.6, 0.3)}  # same palette color
    page = _marked(
        legend=_legend(20),
        marks=[{"legend_index": i, "kind": "dot", "points": [list(p)]} for i, p in points.items()],
    )
    pages = prr._marked_pages(page)
    assert len(pages) == 1
    img = pages[0]
    crops = []
    for p in points.values():
        cx, cy = _dot_center(p)
        crops.append(img.crop((cx - 20, cy - 20, cx + 20, cy + 20)).tobytes())
    assert crops[0] != crops[1]  # "1" and "17" tell the two same-colored dots apart
    # Up to 16 rows nothing is numbered: two dots of one color look alike.
    plain = _marked(
        legend=_legend(2),
        marks=[{"legend_index": 0, "kind": "dot", "points": [list(p)]} for p in points.values()],
    )
    img2 = prr._marked_pages(plain)[0]
    same = []
    for p in points.values():
        cx, cy = _dot_center(p)
        same.append(img2.crop((cx - 20, cy - 20, cx + 20, cy + 20)).tobytes())
    assert same[0] == same[1]


def test_marked_plans_long_legend_spills_onto_continuation_pages():
    page = _marked(legend=_legend(80), marks=[])
    pages = prr._marked_pages(page)
    assert len(pages) == 2
    assert all(p.size == prr.MARKED_PAGE_PX for p in pages)
    data = prr.render_marked_plans_pdf([page, _marked(legend=_legend(200), marks=[])])
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 2 + 5


# Deep brief ------------------------------------------------------------------


def _brief():
    return {
        "project_summary": "Tenant improvement \u2014 second floor offices.",
        "electrical_scope_summary": "New lighting, power and data for 8,616 SF.",
        "systems": [{"system": "Lighting", "in_scope": "yes", "notes": "LED fixtures"}],
        "assumptions": ["Existing service is adequate."],
        "exclusions": ["Fire alarm by others."],
        "risks": [{"risk": "Panel B rating unclear", "severity": "medium", "source": "E0.02"}],
        "rfis": [{"question": "Confirm pendant fixture selection.", "source": "E2.00"}],
        "missing_documents": [],
        "other_trades_affecting_electrical": ["RTU replacement by mechanical."],
        "sheet_summaries": [
            {
                "sheet_number": "E1.00",
                "title": "Power Plan",
                "summary": "Receptacles and circuits.",
            },
            {
                "sheet_number": "E2.00",
                "title": "Lighting Plan",
                "summary": "Fixtures and controls.",
            },
        ],
        "spec_highlights": [
            {"section": "26 05 19", "title": "Conductors", "summary": "THHN copper."}
        ],
    }


def test_brief_docx_reopens_with_sections_and_tables():
    from docx import Document

    data = prr.render_brief_docx(_brief(), project_name="Five Star Tax Resolution")
    doc = Document(io.BytesIO(data))
    texts = [p.text for p in doc.paragraphs]
    assert "Five Star Tax Resolution Deep Brief" in texts
    for heading in (
        "Project summary",
        "Systems",
        "Risks",
        "RFIs and confirmations",
        "Sheet summaries",
    ):
        assert heading in texts
    assert "Tenant improvement - second floor offices." in texts
    assert "Confirm pendant fixture selection. (E2.00)" in texts
    sheet_table = next(t for t in doc.tables if t.rows[0].cells[0].text == "Sheet")
    assert [c.text for c in sheet_table.rows[1].cells] == [
        "E1.00",
        "Power Plan",
        "Receptacles and circuits.",
    ]
    assert len(sheet_table.rows) == 3
    assert "None noted." in texts  # empty missing_documents
    assert all("\u2014" not in t for t in texts)
    with pytest.raises(ValueError):
        prr.render_brief_docx("nope", project_name="X")  # type: ignore[arg-type]


def test_brief_pdf_bytes_uses_the_converter_when_configured(monkeypatch):
    from app.services import office_preview

    monkeypatch.setattr(office_preview, "get_converter", lambda: None)
    assert prr.brief_pdf_bytes(b"docx") is None

    calls = []

    def fake_convert(content, filename):
        calls.append(filename)
        return b"%PDF-1.7 fake"

    monkeypatch.setattr(office_preview, "get_converter", lambda: object())
    monkeypatch.setattr(office_preview, "convert_for_send", fake_convert)
    assert prr.brief_pdf_bytes(b"docx") == b"%PDF-1.7 fake"
    assert calls == ["Deep Brief.docx"]

    def failing(content, filename):
        raise office_preview.ConversionError("converter down")

    monkeypatch.setattr(office_preview, "convert_for_send", failing)
    assert prr.brief_pdf_bytes(b"docx") is None


def test_office_pdf_bytes_passes_the_file_name_to_the_converter(monkeypatch):
    """The workbooks convert under their own names (the Gotenberg converter
    picks one PDF page per sheet from the .xlsx extension); the brief helper
    is the same call with the brief's name."""
    from app.services import office_preview

    monkeypatch.setattr(office_preview, "get_converter", lambda: object())
    calls = []
    monkeypatch.setattr(office_preview, "convert_for_send", lambda c, f: calls.append((c, f)) or b"%PDF-1.7 ok")
    assert prr.office_pdf_bytes(b"xlsx", "BOQ - COUNTS.xlsx") == b"%PDF-1.7 ok"
    assert prr.brief_pdf_bytes(b"docx") == b"%PDF-1.7 ok"
    assert calls == [(b"xlsx", "BOQ - COUNTS.xlsx"), (b"docx", "Deep Brief.docx")]
    monkeypatch.setattr(office_preview, "get_converter", lambda: None)
    assert prr.office_pdf_bytes(b"xlsx", "BOQ - COUNTS.xlsx") is None


def test_office_pdf_bytes_drops_the_blank_sheet_s_page(monkeypatch):
    """LibreOffice prints the workbooks' blank first sheet as a 48 x 0 pt
    page; the copy keeps only real pages, and a PDF that is all real (or all
    blank, or unreadable) comes back as it was."""
    import io

    from pypdf import PdfReader, PdfWriter

    from app.services import office_preview

    def pdf_with(sizes):
        w = PdfWriter()
        for width, height in sizes:
            w.add_blank_page(width=width, height=height)
        out = io.BytesIO()
        w.write(out)
        return out.getvalue()

    monkeypatch.setattr(office_preview, "get_converter", lambda: object())
    converted = pdf_with([(48, 0.03), (1276, 975), (861, 787)])
    monkeypatch.setattr(office_preview, "convert_for_send", lambda c, f: converted)
    trimmed = prr.office_pdf_bytes(b"xlsx", "BOQ - COUNTS.xlsx")
    pages = PdfReader(io.BytesIO(trimmed)).pages
    assert [(round(float(p.mediabox.width)), round(float(p.mediabox.height))) for p in pages] == [(1276, 975), (861, 787)]

    real = pdf_with([(612, 792)])
    monkeypatch.setattr(office_preview, "convert_for_send", lambda c, f: real)
    assert prr.office_pdf_bytes(b"docx", "Deep Brief.docx") == real
    blank = pdf_with([(48, 0.03)])
    monkeypatch.setattr(office_preview, "convert_for_send", lambda c, f: blank)
    assert prr.office_pdf_bytes(b"xlsx", "BOQ - COUNTS.xlsx") == blank
    monkeypatch.setattr(office_preview, "convert_for_send", lambda c, f: b"%PDF-not really")
    assert prr.office_pdf_bytes(b"xlsx", "BOQ - COUNTS.xlsx") == b"%PDF-not really"


def test_brief_pdf_bytes_survives_any_converter_failure(monkeypatch):
    import httpx

    from app.services import office_preview

    monkeypatch.setattr(office_preview, "get_converter", lambda: object())
    request = httpx.Request("PUT", "https://graph.microsoft.com/v1.0/drive")
    for exc in (
        httpx.HTTPStatusError("503", request=request, response=httpx.Response(503)),
        httpx.ConnectTimeout("timed out", request=request),
        RuntimeError("unexpected"),
    ):

        def raising(content, filename, exc=exc):
            raise exc

        monkeypatch.setattr(office_preview, "convert_for_send", raising)
        assert prr.brief_pdf_bytes(b"docx") is None

    def broken_settings():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(office_preview, "get_converter", broken_settings)
    assert prr.brief_pdf_bytes(b"docx") is None


# File names ------------------------------------------------------------------


def test_deliverable_filenames_follow_the_sent_files():
    f = prr.deliverable_filename
    assert f("boq", "Five Star", 1) == "BOQ - COUNTS.xlsx"
    assert (
        f("estimate", "Five Star Tax Resolution", 1)
        == "FIVE STAR TAX RESOLUTION - ELECTRICAL ESTIMATE.xlsx"
    )
    assert (
        f("estimate", 'YMCA: Torrey/Pines "ELC"', 1)
        == "YMCA TORREY PINES ELC - ELECTRICAL ESTIMATE.xlsx"
    )
    assert f("trench_markup", "X", 1) == "Trench Markup.pdf"
    assert f("sawcut_markup", "X", 1) == "Sawcut Markup.pdf"
    assert f("marked_plans", "X", 1) == "Marked Plans.pdf"
    assert f("marked_plans", "X", 1, area="Ford Avenue") == "Marked Plans - FORD AVENUE.pdf"
    assert f("brief", "X", 1) == "Deep Brief.docx"
    assert f("brief_pdf", "X", 1) == "Deep Brief.pdf"
    assert f("boq", "X", 2) == "Rev 2 - BOQ - COUNTS.xlsx"
    assert f("brief", "X", 3) == "Rev 3 - Deep Brief.docx"
    with pytest.raises(ValueError):
        f("invoice", "X", 1)


# Smoke: re-render one real sent BOQ -------------------------------------------

_BACKUP = (
    Path(__file__).resolve().parents[2]
    / "backups"
    / "BDR_Prod_2026-10-01"
    / "storage"
    / "project-files"
)
# Sent BOQs that re-render row for row from a spec parsed back out of them:
# EMT and PVC fittings (YMCA), PVC only (NVARNG gate), EMT only (State Public
# Works, Desert Willow, UNLV, UMC). Edikted's OTHER CONDUIT ACCESSORIES
# numbers are stale in the sent file, so it is not listed.
_SENT_BOQS = {
    "ymca_torrey_pines": "ba167b17-0ff5-4012-a638-403bcced09af/boq/b26fea0a676743cd9a81c0a14f039939-BOQ - COUNTS.xlsx",
    "nvarng_gate": "aec676fa-55b0-4253-83cd-151482ed34fe/boq/3ff38c09102f41d386009baa574c982b-BOQ - COUNTS.xlsx",
    "state_public_works": "c45e3f91-133e-4b1b-bc1b-49e2c8b36f02/boq/bd346bc402bd40d683c13bf19f132381-BOQ - COUNTS.xlsx",
    "desert_willow": "d3efbef9-cda3-43f7-8143-fd29822881b1/boq/ed73bf74c63e49ec820ddf5e7d8c2b5c-BOQ - COUNTS.xlsx",
    "unlv": "82b14adb-e8e6-41cd-93af-5f929fa3ad05/boq/923779de1319457880293e4d62d4a8ec-BOQ - COUNT.xlsx",
    "umc_pharmacy": "7dfc64e6-36ef-4c67-9d02-0d7942015011/boq/aeec3919a12d46dfaade27b9e8c8fbd1-BOQ - COUNTS.xlsx",
}


def _spec_from_sent_boq(path: Path) -> tuple[dict, list[tuple[str, float, str]]]:
    """Parse a sent BOQ back into the BOQ_SPEC shape (pre-wastage FT wiring
    quantities, fitting rows dropped because the renderer regenerates them)
    and return the sent (description, quantity, unit) rows for comparison."""
    ws = openpyxl.load_workbook(path)["SHEET"]
    title = ws["B2"].value.split("\n")
    sections: list[dict] = []
    expected: list[tuple[str, float, str]] = []
    group = sub = None
    for r in range(5, ws.max_row + 1):
        b = ws.cell(row=r, column=2)
        if b.value in (None, ""):
            continue
        text = str(b.value)
        qty = ws.cell(row=r, column=3).value
        fill = b.fill.fgColor.rgb if b.fill.fill_type and b.fill.fgColor.type == "rgb" else None
        if fill == "FFFFFF00":
            sections.append({"heading": text, "note": None, "groups": []})
            group = sub = None
            continue
        if b.font.b and qty is None:
            if b.alignment.horizontal == "center":
                name, _, note = text.partition("\n")
                group = _group(name, [], note=note or None)
                sections[-1]["groups"].append(group)
                sub = None
            elif text != "OTHER CONDUIT ACCESSORIES":
                sub = _sg(text, [])
                group["subgroups"].append(sub)
            continue
        unit = ws.cell(row=r, column=4).value
        expected.append((text, float(qty), unit))
        if b.alignment.horizontal == "right":
            continue  # a fitting row: regenerated by the renderer
        if group is None:
            group = _group("", [])
            sections[-1]["groups"].append(group)
        if sub is None:
            sub = _sg("", [])
            group["subgroups"].append(sub)
        wastage = unit == "FT" and group["name"] == "WIRING"
        q = float(qty) / 1.1 if wastage else float(qty)
        sub["items"].append(_item(text, q, unit, wastage=wastage))
    spec = {
        "title": title[0],
        "subtitle": title[1] if len(title) > 1 else None,
        "areas": [{"area": "SHEET", "notes": [], "sections": sections}],
        "closing_notes": [],
    }
    return spec, expected


@pytest.mark.parametrize("name", sorted(_SENT_BOQS))
def test_smoke_rerenders_sent_boqs(name):
    """The fitting arithmetic (connectors, couplings, straps, elbows,
    locknuts, adapters, anchors, screws, SQ boxes) checked against what the
    estimators sent. The parser treats FT rows in the WIRING group as the
    wastage rows, which is how those files were built; the fitting rows are
    dropped from the spec and regenerated, so their quantities are a real
    check."""
    path = _BACKUP / _SENT_BOQS[name]
    if not path.exists():
        pytest.skip("production backup not on disk")
    spec, expected = _spec_from_sent_boq(path)
    ws = _load(prr.render_boq_xlsx(spec))["SHEET"]
    got = []
    for r in range(5, ws.max_row + 1):
        qty = ws.cell(row=r, column=3).value
        if qty is not None:
            got.append((ws.cell(row=r, column=2).value, float(qty), ws.cell(row=r, column=4).value))
    assert [g[0] for g in got] == [e[0] for e in expected]
    assert [g[2] for g in got] == [e[2] for e in expected]
    for g, e in zip(got, expected, strict=True):
        assert g[1] == pytest.approx(e[1], abs=0.01), g[0]
    sent = openpyxl.load_workbook(path)["SHEET"]
    assert ws["B2"].value == sent["B2"].value

    def has_oca(sheet):
        return any(
            sheet.cell(row=r, column=2).value == "OTHER CONDUIT ACCESSORIES"
            for r in range(5, sheet.max_row + 1)
        )

    if has_oca(sent):
        assert _find(ws, "OTHER CONDUIT ACCESSORIES") == _find(sent, "OTHER CONDUIT ACCESSORIES")

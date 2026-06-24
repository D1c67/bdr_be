"""Generate per-category RFQ Excel files from confirmed line items.

The output mirrors the reference RFQ (`excel_format/6954_Lighting_RFQ.xlsx`): a
category title row, the BOQ's four columns (SR.NO | DESCRIPTION | QUANTITY | UNIT),
then one row per material. No price column — the vendor fills pricing in.
"""

import io
from typing import Any

_HEADERS = ["SR.NO", "DESCRIPTION", "QUANTITY", "UNIT", "NOTES"]


def build_rfq_workbook(category_name: str, line_items: list[dict[str, Any]]) -> bytes:
    """Build an .xlsx for one RFQ category and return its bytes."""
    import openpyxl
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = (category_name or "RFQ")[:31]  # Excel caps sheet names at 31 chars

    # Title row (category name), like "LIGHTING FIXTURES" in the reference RFQ.
    title = ws.cell(row=1, column=2, value=category_name.upper() if category_name else "RFQ")
    title.font = Font(bold=True)

    header_row = 3
    for col, name in enumerate(_HEADERS, start=1):
        c = ws.cell(row=header_row, column=col, value=name)
        c.font = Font(bold=True)

    r = header_row + 1
    for item in line_items:
        ws.cell(row=r, column=1, value=item.get("sr_no"))
        ws.cell(row=r, column=2, value=item.get("description"))
        qty = item.get("quantity")
        ws.cell(row=r, column=3, value=float(qty) if qty is not None else None)
        ws.cell(row=r, column=4, value=item.get("unit"))
        ws.cell(row=r, column=5, value=item.get("notes"))
        r += 1

    # Reasonable column widths (description is the wide one).
    for col, width in {"A": 8, "B": 70, "C": 12, "D": 10, "E": 30}.items():
        ws.column_dimensions[col].width = width

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def rows_for_preview(xlsx_bytes: bytes, max_rows: int = 500) -> list[list[Any]]:
    """Parse a stored .xlsx back into a list of rows for server-side preview."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True, read_only=True)
    rows: list[list[Any]] = []
    ws = wb.active
    for row in ws.iter_rows(values_only=True):
        vals = list(row)
        while vals and (vals[-1] is None or vals[-1] == ""):
            vals.pop()
        rows.append(["" if v is None else v for v in vals])
        if len(rows) >= max_rows:
            break
    wb.close()
    return rows

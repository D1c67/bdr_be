"""Certified Payroll — Gusto payroll-detail parser (.xls/.xlsx exports).

Parses the 65-column Gusto "payroll detail" export into ParsedDetailEntry rows.
Entry point is parse_payroll_detail(file_content: bytes, filename: str); pandas
picks the engine from the bytes (xlrd for legacy .xls). Columns are mapped by
POSITION (COLUMN_MAP), not header text — the export repeats several headers
verbatim (35, 43-45, 59-63 are duplicates and are skipped).
"""

import io
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import List, Optional

import pandas as pd


@dataclass
class ParsedDetailEntry:
    """Parsed payroll detail entry from Gusto XLS export."""

    employee_name: str
    pay_date: Optional[date]
    time_period: Optional[str]

    # Hours
    hours_total: Optional[Decimal]
    hours_regular: Optional[Decimal]
    hours_grave_shift: Optional[Decimal]
    hours_ot: Optional[Decimal]
    hours_holiday: Optional[Decimal]
    hours_foreman: Optional[Decimal]
    hours_gf: Optional[Decimal]
    hours_sal: Optional[Decimal]
    hours_regular_pay: Optional[Decimal]
    hours_overtime_pay: Optional[Decimal]
    hours_salary: Optional[Decimal]
    hours_holiday_pay: Optional[Decimal]

    # Gross pay
    gross_pay_total: Optional[Decimal]
    gross_pay_regular: Optional[Decimal]
    gross_pay_grave_shift: Optional[Decimal]
    gross_pay_ot: Optional[Decimal]
    gross_pay_holiday: Optional[Decimal]
    gross_pay_foreman: Optional[Decimal]
    gross_pay_reimb: Optional[Decimal]
    gross_pay_gf: Optional[Decimal]
    gross_pay_sal: Optional[Decimal]
    gross_pay_regular_pay: Optional[Decimal]
    gross_pay_overtime_pay: Optional[Decimal]
    gross_pay_reimbursement: Optional[Decimal]
    gross_pay_salary: Optional[Decimal]
    gross_pay_holiday_pay: Optional[Decimal]

    # Deductions
    pretax_deductions_total: Optional[Decimal]
    pretax_401k: Optional[Decimal]
    pretax_401k_catchup: Optional[Decimal]
    adjusted_gross: Optional[Decimal]

    # Other pay
    other_pay_total: Optional[Decimal]
    other_pay_qot: Optional[Decimal]

    # Employee taxes
    employee_taxes_total: Optional[Decimal]
    employee_taxes_fit: Optional[Decimal]
    employee_taxes_ss: Optional[Decimal]
    employee_taxes_med: Optional[Decimal]

    # After-tax deductions
    aftertax_deductions_total: Optional[Decimal]
    aftertax_working_dues: Optional[Decimal]
    aftertax_roth_401k: Optional[Decimal]

    # Net pay
    net_pay: Optional[Decimal]

    # Employer taxes & contributions
    employer_taxes_contributions_total: Optional[Decimal]
    employer_taxes_total: Optional[Decimal]
    employer_taxes_futa: Optional[Decimal]
    employer_taxes_ss: Optional[Decimal]
    employer_taxes_med: Optional[Decimal]
    employer_taxes_sui: Optional[Decimal]
    employer_taxes_cep: Optional[Decimal]

    # Company contributions
    company_contributions_total: Optional[Decimal]
    company_contributions_pension: Optional[Decimal]
    company_contributions_401k: Optional[Decimal]
    company_contributions_401k_catchup: Optional[Decimal]
    company_contributions_dental_vision: Optional[Decimal]

    # Total payroll cost
    total_payroll_cost: Optional[Decimal]


def _to_decimal(value) -> Optional[Decimal]:
    """Convert a cell value to Decimal, returning None for empty/NaN."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        d = Decimal(str(value))
        return d.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _parse_pay_date(value) -> Optional[date]:
    """Parse pay date from various formats."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    # datetime is a subclass of date — narrow it first, else a Timestamp/datetime
    # cell would fall through the `date` branch and be returned un-narrowed.
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    # Try MM/DD/YYYY
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# The Gusto payroll-detail export is exactly 65 columns wide (indices 0-64).
# parse_payroll_detail validates the header row against this before trusting the
# positional COLUMN_MAP.
EXPECTED_COLUMN_COUNT = 65

# Column-index-to-field mapping for the 65-column Gusto export.
# Duplicate columns (35, 43-45, 59-63) are skipped.
COLUMN_MAP = {
    0: "employee_name",
    1: "pay_date",
    2: "time_period",
    3: "hours_total",
    4: "hours_regular",
    5: "hours_grave_shift",
    6: "hours_ot",
    7: "hours_holiday",
    8: "hours_foreman",
    9: "hours_gf",
    10: "hours_sal",
    11: "hours_regular_pay",
    12: "hours_overtime_pay",
    13: "hours_salary",
    14: "hours_holiday_pay",
    15: "gross_pay_total",
    16: "gross_pay_regular",
    17: "gross_pay_grave_shift",
    18: "gross_pay_ot",
    19: "gross_pay_holiday",
    20: "gross_pay_foreman",
    21: "gross_pay_reimb",
    22: "gross_pay_gf",
    23: "gross_pay_sal",
    24: "gross_pay_regular_pay",
    25: "gross_pay_overtime_pay",
    26: "gross_pay_reimbursement",
    27: "gross_pay_salary",
    28: "gross_pay_holiday_pay",
    29: "pretax_deductions_total",
    30: "pretax_401k",
    31: "pretax_401k_catchup",
    32: "adjusted_gross",
    33: "other_pay_total",
    34: "other_pay_qot",
    # 35 = duplicate of 34 (Qualified OT Tracking)
    36: "employee_taxes_total",
    37: "employee_taxes_fit",
    38: "employee_taxes_ss",
    39: "employee_taxes_med",
    40: "aftertax_deductions_total",
    41: "aftertax_working_dues",
    42: "aftertax_roth_401k",
    # 43-45 = duplicates of 37-39
    46: "net_pay",
    47: "employer_taxes_contributions_total",
    48: "employer_taxes_total",
    49: "employer_taxes_futa",
    50: "employer_taxes_ss",
    51: "employer_taxes_med",
    52: "employer_taxes_sui",
    53: "employer_taxes_cep",
    54: "company_contributions_total",
    55: "company_contributions_pension",
    56: "company_contributions_401k",
    57: "company_contributions_401k_catchup",
    58: "company_contributions_dental_vision",
    # 59-63 = duplicates of 49-53
    64: "total_payroll_cost",
}

# Fields that are date type
DATE_FIELDS = {"pay_date"}
# Fields that are plain strings
STRING_FIELDS = {"employee_name", "time_period"}


def parse_payroll_detail(file_content: bytes, filename: str) -> List[ParsedDetailEntry]:
    """Parse a Gusto payroll detail XLS/XLSX file.

    File structure:
      Row 0: Company name
      Row 1: Report title
      Row 2: Blank
      Row 3: Date range text
      Row 4: Column headers (65 columns)
      Row 5+: Employee data rows
      Last row: Totals (skip)
    """
    try:
        df = pd.read_excel(io.BytesIO(file_content), header=None)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 — corrupt/unreadable workbook → 400, not 500
        raise ValueError(f"Could not read payroll-detail file: {exc}") from exc

    # Find header row — look for "Name" in column 0
    header_row = None
    for idx in range(min(10, len(df))):
        cell = df.iloc[idx, 0]
        if isinstance(cell, str) and cell.strip().lower() == "name":
            header_row = idx
            break

    if header_row is None:
        raise ValueError("Could not find header row (expected 'Name' in first column)")

    # COLUMN_MAP is strictly positional, so a Gusto export that inserts or drops
    # a pay-type/benefit column (or that Gusto re-lays-out) would shift every
    # later index and silently write money into the wrong fields. Refuse any
    # sheet whose header width is not the expected 65 columns rather than parse
    # a mis-aligned layout into a certified report.
    header_cols = int(df.iloc[header_row].notna().sum())
    if header_cols != EXPECTED_COLUMN_COUNT:
        raise ValueError(
            f"Unexpected payroll-detail layout: header row has {header_cols} columns, "
            f"expected {EXPECTED_COLUMN_COUNT}. The Gusto export format may have changed — "
            "column positions can no longer be trusted."
        )

    entries = []
    # Data rows start after header
    for row_idx in range(header_row + 1, len(df)):
        row = df.iloc[row_idx]

        # Get name from column 0
        name_val = row.iloc[0] if len(row) > 0 else None

        # Skip blank rows or totals row
        if name_val is None or (isinstance(name_val, float) and pd.isna(name_val)):
            continue
        name_str = str(name_val).strip()
        if not name_str or name_str.lower() in ("totals", "total"):
            continue

        # Build entry data from column map
        data = {}
        for col_idx, field_name in COLUMN_MAP.items():
            if col_idx >= len(row):
                data[field_name] = None
                continue

            cell_val = row.iloc[col_idx]

            if field_name in STRING_FIELDS:
                if cell_val is None or (isinstance(cell_val, float) and pd.isna(cell_val)):
                    data[field_name] = None if field_name != "employee_name" else ""
                else:
                    data[field_name] = str(cell_val).strip()
            elif field_name in DATE_FIELDS:
                data[field_name] = _parse_pay_date(cell_val)
            else:
                data[field_name] = _to_decimal(cell_val)

        entries.append(ParsedDetailEntry(**data))

    return entries

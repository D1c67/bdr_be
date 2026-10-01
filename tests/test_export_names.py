"""Short names inside export ZIPs (app/services/export_names.py)."""

import pytest

from app.services import export_names as en


@pytest.mark.parametrize(
    "raw, short",
    [
        ("E-Sheets (pages 12-40).pdf", "E- p12-40.pdf"),
        ("E-Sheets (pages 12-40 + cover sheets).pdf", "E- p12-40+cover.pdf"),
        ("e sheets.pdf", "E-.pdf"),
        ("FA-Sheets (pages 3-9).pdf", "FA- p3-9.pdf"),
        ("Low Voltage Drawings", "LV Dwgs"),
        ("Switch Gear Submittal.pdf", "SG Submittal.pdf"),
        ("Switchgear.pdf", "SG.pdf"),
        ("Div 26 Specifications.pdf", "Div 26 Spec.pdf"),
        ("General / Cover Sheets", "Gen-Cover"),
        ("Fire Protection Drawings", "FP Dwgs"),
        ("Geotechnical Report", "Geotech Rpt"),
        ("26.9.7126 Monument Tower.pdf", "7126 Monument Tower.pdf"),
        ("26.10.0042B Plans.pdf", "0042B Plans.pdf"),
        ("Cover Sheets.pdf", "Cover.pdf"),
        ("Quote.pdf", "Quote.pdf"),
    ],
)
def test_short_name(raw, short):
    assert en.short_name(raw) == short


def test_short_job_number():
    assert en.short_job_number("26.9.7126") == "7126"
    assert en.short_job_number("26.9.7126B") == "7126B"
    assert en.short_job_number("24-1180") == "1180"
    assert en.short_job_number("12") is None
    assert en.short_job_number(None) is None


def test_fit_keeps_the_extension():
    out = en.fit("x" * 200 + ".pdf", 80)
    assert len(out) == 80
    assert out.endswith(".pdf")


def test_fit_path_stays_under_the_budget():
    folders = ["F" * 90, "Elec Dwgs", "Other label that is quite long indeed"]
    path = en.fit_path(folders, "N" * 150 + ".pdf")
    assert len(path) <= en.PATH_MAX
    assert path.endswith(".pdf")
    assert all(len(p) <= en.FOLDER_MAX for p in path.split("/")[:-1])


def test_flat_name_prefixes_the_folders():
    assert en.flat_name(["BID SET", "Elec Dwgs"], "E- p1-4.pdf") == "BID SET - Elec Dwgs - E- p1-4.pdf"
    assert len(en.flat_name(["A" * 60], "B" * 200 + ".pdf")) <= en.PATH_MAX

"""Unit tests for project submittal sending — pure parts only (no DB / no Graph
/ no Gotenberg): subject/body templates, attachment content types, the PDF
filename + quantity formatter, and the HTML-escaping injection boundary."""

from app.services import submittal_ingest as si
from app.services import submittal_pdf as spdf
from app.services import submittal_sending as ss

_PROJECT = {"id": "p1", "number": "26-104", "name": "Riverside Plaza"}


# ── Subject / body templates ───────────────────────────────────────────────


def test_build_subject_format():
    assert ss.build_subject(_PROJECT) == "26-104 - Riverside Plaza - Submittal Request"


def test_build_subject_without_number():
    assert ss.build_subject({"number": None, "name": "X"}) == "TBD - X - Submittal Request"


def test_base_body_attached_plans_only():
    body = ss.build_base_body("Jane", _PROJECT, None, shared_present=True, has_specs=False)
    assert body.startswith("Hello Jane,")
    assert "The plans/drawings are attached." in body
    assert "specifications" not in body
    assert body.endswith("Thank you,\nThe G3 Estimating Team")


def test_base_body_attached_with_specs():
    body = ss.build_base_body("Jane", _PROJECT, None, shared_present=True, has_specs=True)
    assert "The plans/drawings and specifications are attached." in body


def test_base_body_link_when_oversize():
    body = ss.build_base_body("Jane", _PROJECT, "https://1drv.ms/x", shared_present=True, has_specs=False)
    assert "available here: https://1drv.ms/x" in body


def test_base_body_no_docs_line_when_nothing_shared():
    body = ss.build_base_body("Jane", _PROJECT, None, shared_present=False, has_specs=False)
    assert "plans/drawings are attached" not in body
    assert "available here" not in body


def test_custom_body_substitutes_contact_and_appends_link():
    out = ss.build_custom_body("Hi <Contact Name>", "Jane Smith", "https://1drv.ms/x")
    assert out.startswith("Hi Jane Smith")
    assert out.endswith("The plans/drawings are available here: https://1drv.ms/x")


def test_custom_body_keeps_existing_link():
    out = ss.build_custom_body("Docs https://1drv.ms/x — <Contact Name>", "Jane", "https://1drv.ms/x")
    assert out.count("https://1drv.ms/x") == 1


# ── Small helpers ──────────────────────────────────────────────────────────


def test_content_type():
    assert ss._content_type("a.pdf") == "application/pdf"
    assert ss._content_type("weird.zzz") == "application/octet-stream"


def test_safe_component_strips_path_chars():
    assert "/" not in ss._safe_component("a/b:c*?.pdf")
    assert ss._safe_component("") == "file"


# ── PDF filename + quantity formatting ─────────────────────────────────────


def test_pdf_filename():
    name = spdf.pdf_filename(_PROJECT, "Low Voltage")
    assert name == "Submittal Request - 26-104 - Low Voltage.pdf"


def test_pdf_filename_sanitizes():
    name = spdf.pdf_filename({"number": "26/104"}, "Switch<>gear")
    assert "/" not in name and "<" not in name and name.endswith(".pdf")


def test_fmt_qty():
    assert spdf._fmt_qty(5) == "5"
    assert spdf._fmt_qty(5.0) == "5"
    assert spdf._fmt_qty(5.5) == "5.5"
    assert spdf._fmt_qty(None) == ""
    assert spdf._fmt_qty("") == ""
    # Non-numeric passes through, escaped.
    assert spdf._fmt_qty("lot") == "lot"


# ── HTML escaping — ad-hoc descriptions are the injection boundary ─────────


def test_render_html_escapes_adhoc_description():
    items = [{"description": "<script>alert(1)</script>", "quantity": None, "unit": None, "notes": None}]
    html = spdf.render_html(_PROJECT, "Low Voltage", items)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_html_no_items():
    html = spdf.render_html(_PROJECT, "Low Voltage", [])
    assert "No items listed." in html


# ── Sender guard (pure) ────────────────────────────────────────────────────


def test_is_from_contact_matches_case_insensitively():
    assert si.is_from_contact({"from_address": "Sales@Acme.com"}, {"contact_email": "sales@acme.com"})


def test_is_from_contact_rejects_mismatch():
    assert not si.is_from_contact({"from_address": "other@x.com"}, {"contact_email": "sales@acme.com"})


def test_is_from_contact_false_when_contact_missing():
    assert not si.is_from_contact({"from_address": "sales@acme.com"}, {"contact_email": None})

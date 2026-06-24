"""Unit tests for the branded vendor-email HTML shell."""

from app.services import email_branding as eb
from app.services import rfq_sending as rs

BODY = rs.build_base_body("Jane Smith", "Friday, June 19th 2:00 PM", None)


def test_render_contains_signature_with_logo_and_phone():
    html = eb.render_vendor_email(BODY)
    assert f'src="cid:{eb.LOGO_CONTENT_ID}"' in html
    assert eb.OFFICE_PHONE_DISPLAY in html
    assert f'href="tel:{eb.OFFICE_PHONE_TEL}"' in html
    assert "G3 ELECTRICAL" in html


def test_render_signoff_appears_once_in_signature_block():
    html = eb.render_vendor_email(BODY)
    # Stripped from the body text, rendered once by the signature block.
    assert html.count(eb.SIGNOFF) == 1
    # The polite closing line survives the strip.
    assert "Thank you," in html


def test_render_keeps_body_without_signoff_intact():
    html = eb.render_vendor_email("Hi Jane,\n\nPlease quote the BOM.\n\nBest, Sam")
    assert "Best, Sam" in html
    assert html.count(eb.SIGNOFF) == 1  # signature still identifies the team


def test_render_escapes_html_in_body():
    html = eb.render_vendor_email("Quote <5kV> gear & wire")
    assert "&lt;5kV&gt;" in html
    assert "Quote <5kV>" not in html


def test_render_linkifies_drawings_url():
    body = rs.build_base_body("Jane", "Friday, June 19th 2:00 PM", "https://1drv.ms/f/x?e=1&y=2")
    html = eb.render_vendor_email(body)
    # href uses the escaped form; trailing sentence punctuation stays outside.
    assert '<a href="https://1drv.ms/f/x?e=1&amp;y=2"' in html


def test_render_paragraphs_and_line_breaks():
    html = eb.render_vendor_email("line one\nline two\n\nsecond para")
    assert "line one<br>line two" in html
    assert html.count("<p ") == 2


def test_red_is_minimal():
    html = eb.render_vendor_email(BODY)
    assert html.count("#951e2d") == 1  # hairline accent only


def test_render_proposal_email_labels_proposal_and_keeps_signature():
    html = eb.render_proposal_email("Dear <GC Name>,\n\nProposal attached.\n\nThank you,")
    assert "PROPOSAL" in html
    assert "REQUEST FOR QUOTE" not in html  # proposal banner, not the RFQ label
    # Same minimal branded shell + signature as the vendor email.
    assert "G3 ELECTRICAL" in html
    assert f'src="cid:{eb.LOGO_CONTENT_ID}"' in html
    assert eb.SIGNOFF in html
    assert eb.OFFICE_PHONE_DISPLAY in html


def test_logo_file_exists_and_is_inline_sized():
    content = eb.logo_bytes()
    assert content[:2] == b"\xff\xd8"  # JPEG magic
    assert len(content) < 3 * 1024 * 1024  # stays on the inline-attachment path

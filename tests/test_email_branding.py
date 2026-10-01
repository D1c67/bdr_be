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


def test_paragraphs_can_skip_the_linkify_pass():
    text = "See https://x.example/a?b=1&c=2 now\n\n<b>next</b>"
    assert '<a href="https://x.example/a?b=1&amp;c=2"' in eb._paragraphs(text)
    plain = eb._paragraphs(text, linkify=False)
    assert "<a " not in plain and "https://x.example/a?b=1&amp;c=2" in plain
    assert "&lt;b&gt;next&lt;/b&gt;" in plain and plain.count("<p ") == 2


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


LINK = "https://g3.sharepoint.com/:f:/s/x?e=1&y=2"


def test_documents_link_renders_card_above_greeting_once():
    body = rs.build_base_body("Jane", "Friday, June 19th 2:00 PM", LINK)
    html = eb.render_vendor_email(body, documents_link=LINK)
    assert f'src="cid:{eb.DOCUMENTS_CARD_CONTENT_ID}"' in html
    assert html.index(eb.DOCUMENTS_CARD_CONTENT_ID) < html.index("Hello Jane")
    # Standard link sentence dropped from the body: button + fallback only.
    assert eb.DOCUMENTS_LINK_SENTENCE not in html
    assert html.count('href="https://g3.sharepoint.com/:f:/s/x?e=1&amp;y=2"') == 2


def test_documents_card_drops_reworded_link_line_too():
    body = f"Hi Jane,\n\nYou can find the plans and specs here: {LINK}\n\nPlease quote."
    html = eb.render_vendor_email(body, documents_link=LINK)
    assert "plans and specs here" not in html
    assert "Please quote." in html


def test_documents_card_keeps_link_inside_other_prose():
    body = f"Hi Jane,\n\nSee {LINK} and also the BOM attached.\n\nPlease quote."
    html = eb.render_vendor_email(body, documents_link=LINK)
    assert "and also the BOM attached" in html


def test_no_documents_link_no_card():
    assert eb.DOCUMENTS_CARD_CONTENT_ID not in eb.render_vendor_email(BODY)


def test_documents_card_asset_matches_declared_size():
    from PIL import Image
    import io

    im = Image.open(io.BytesIO(eb.documents_card_bytes()))
    w, h = eb.DOCUMENTS_CARD_SIZE
    assert im.size == (w * 2, h * 2) and im.n_frames > 1

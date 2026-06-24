"""Unit tests for the proposal docx engine (pure, no DB / no LLM).

Most tests render against the REAL committed template asset — the same bytes
production uses — so template drift breaks the suite, not a live send.
"""

import io
import zipfile
from dataclasses import replace

import pytest
from docx import Document
from docx.oxml.ns import qn

from app.services import proposal_docx as pdx
from app.services.proposal_docx import ProposalContext, ProposalRenderError

HAS_TEMPLATE = pdx.TEMPLATE_PATH.exists()
needs_template = pytest.mark.skipif(not HAS_TEMPLATE, reason="template asset not present")

CTX = ProposalContext(
    project_number="26.4.7080",
    project_name="Red Rock Slot Expansion",
    address="11011 W Charleston Blvd, Las Vegas, NV 89135",
    gc_name="Taylor International Corp.",
    date_str="06/10/2026",
    labor_time="DAY",
    wage_text="Prevailing Wage",
    material_amount="$41,188",
    labor_amount="$40,950",
    total_amount="$82,138",
    scope_lines=(
        "Demolish existing lighting and electrical devices.",
        "Furnish and install conduit, wiring and boxes.",
        "Furnish and install GFCI receptacles.",
        "Furnish and install panels.",
        "Furnish and install lighting, lighting controls and panels.",
    ),
)


# ── filenames ──────────────────────────────────────────────────────────────


def test_last4_typical():
    assert pdx.last4("26.4.7080") == "7080"
    assert pdx.last4("7080") == "7080"
    assert pdx.last4("26-47") == "2647"


def test_last4_short_and_empty():
    assert pdx.last4("4.7") == "47"  # fewer than 4 digits → use all
    with pytest.raises(ProposalRenderError):
        pdx.last4("TBD")


def test_sanitize_component():
    assert pdx.sanitize_component("A/B Contractors") == "A-B Contractors"
    assert pdx.sanitize_component('Bad:*?"Name') == "Bad----Name"
    assert pdx.sanitize_component("  Two   Spaces  ") == "Two Spaces"
    assert pdx.sanitize_component("Ünïcode Bau GmbH") == "Ünïcode Bau GmbH"
    assert pdx.sanitize_component("///") == "---"
    with pytest.raises(ProposalRenderError):
        pdx.sanitize_component("   ")


def test_build_filename():
    assert (
        pdx.build_filename("26.4.7080", "Taylor International Corp.")
        == "Proposal 7080 - Taylor International Corp..docx"
    )
    assert pdx.build_filename("26.4.7080", "A/B Co") == "Proposal 7080 - A-B Co.docx"


# ── cross-run replacement on a synthetic split-run doc ─────────────────────


def _synthetic_doc(*run_groups: tuple[str, ...]) -> Document:
    doc = Document()
    for runs in run_groups:
        p = doc.add_paragraph()
        for chunk in runs:
            p.add_run(chunk)
    return doc


def test_replace_across_three_runs():
    doc = _synthetic_doc(("Hello <GC ", "Na", "me>, welcome",))
    n = pdx.replace_in_paragraph(doc.paragraphs[0]._p, "<GC Name>", "Turner")
    assert n == 1
    assert doc.paragraphs[0].text == "Hello Turner, welcome"
    # No runs created or destroyed — formatting containers intact.
    assert len(doc.paragraphs[0].runs) == 3


def test_replace_multiple_occurrences_and_single_run():
    doc = _synthetic_doc(("<X> and <X>",))
    n = pdx.replace_in_paragraph(doc.paragraphs[0]._p, "<X>", "Y")
    assert n == 2
    assert doc.paragraphs[0].text == "Y and Y"


def test_replace_preserves_surrounding_text():
    doc = _synthetic_doc(("based off <Prevailing Wage or ", "Non-prevailing wage>", " wage rates."))
    pdx.replace_in_paragraph(
        doc.paragraphs[0]._p, "<Prevailing Wage or Non-prevailing wage>", "Prevailing Wage"
    )
    assert doc.paragraphs[0].text == "based off Prevailing Wage wage rates."


def test_replace_terminates_when_value_contains_placeholder():
    """A self-referential replacement must not loop forever (review finding)."""
    doc = _synthetic_doc(("Project: <X> end",))
    n = pdx.replace_in_paragraph(doc.paragraphs[0]._p, "<X>", "value with <X> inside")
    assert n == 1
    assert doc.paragraphs[0].text == "Project: value with <X> inside end"


# ── rendering against the real template ────────────────────────────────────


@needs_template
def test_render_full_template():
    out = pdx.render_proposal(pdx.TEMPLATE_PATH.read_bytes(), CTX)

    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        names = zf.namelist()
        assert len(names) == len(set(names))
        # Untouched parts survive rendering: logo, ink signature, footer.
        # (The orphaned embedded xlsx + EMF from the old green chart were
        # cleaned out by the user's Word re-save of the asset — expected.)
        for keeper in (
            "word/media/image1.jpg",
            "word/ink/ink1.xml",
            "word/media/image3.png",
            "word/footer1.xml",
        ):
            assert keeper in names, keeper

    text = pdx.extract_document_text(out)
    assert "06/10/2026" in text
    assert "Taylor International Corp." in text
    assert "Red Rock Slot Expansion 11011 W Charleston Blvd" in text
    assert "$82,138" in text
    assert "Prevailing Wage wage rates" in text
    assert "DAY time work hours" in text
    for ph in pdx.ALL_PLACEHOLDERS:
        assert ph not in text

    # full validation passes for the right GC and fails for an absent one
    pdx.validate_output(out, gc_name=CTX.gc_name, scope_lines=CTX.scope_lines)


@needs_template
def test_scope_lines_inserted_in_position_with_numbering():
    out = pdx.render_proposal(pdx.TEMPLATE_PATH.read_bytes(), CTX)
    doc = Document(io.BytesIO(out))

    numbered = [
        p for p in doc.paragraphs if p._p.find(".//" + qn("w:numPr")) is not None and p.text.strip()
    ]
    texts = [p.text for p in numbered]
    anchor_i = next(i for i, t in enumerate(texts) if t.casefold().startswith(pdx.ANCHOR_TEXT))
    closer_i = next(i for i, t in enumerate(texts) if t.casefold().startswith(pdx.CLOSER_TEXT))
    inserted = texts[anchor_i + 1 : closer_i]
    assert inserted == list(CTX.scope_lines)

    # clones share the anchor's numId → Word renumbers natively
    def num_id(p):
        el = p._p.find(".//" + qn("w:numId"))
        return el.get(qn("w:val")) if el is not None else None

    anchor_num = num_id(numbered[anchor_i])
    assert anchor_num is not None
    for p in numbered[anchor_i + 1 : closer_i]:
        assert num_id(p) == anchor_num

    # no duplicated bookmarks from cloning (Word repair-dialog trigger)
    ids = [b.get(qn("w:id")) for b in doc.element.body.iter(qn("w:bookmarkStart"))]
    assert len(ids) == len(set(ids))


@needs_template
def test_anchor_fallback_and_missing():
    base = Document(io.BytesIO(pdx.TEMPLATE_PATH.read_bytes()))

    def wipe(needle: str):
        for p in base.paragraphs:
            if p.text.strip().casefold().startswith(needle):
                for r in p.runs:
                    r.text = "REWORDED"

    wipe(pdx.ANCHOR_TEXT)
    buf = io.BytesIO()
    base.save(buf)
    doc = Document(io.BytesIO(buf.getvalue()))
    _, mode = pdx.find_scope_anchor(doc)
    assert mode == "before"  # falls back to inserting before the closer

    wipe(pdx.CLOSER_TEXT)
    buf2 = io.BytesIO()
    base.save(buf2)
    with pytest.raises(ProposalRenderError):
        pdx.find_scope_anchor(Document(io.BytesIO(buf2.getvalue())))


@needs_template
def test_validate_output_negatives():
    template = pdx.TEMPLATE_PATH.read_bytes()
    out = pdx.render_proposal(template, CTX)

    with pytest.raises(ProposalRenderError, match="not present as the To: cell"):
        pdx.validate_output(out, gc_name="Turner Construction", scope_lines=CTX.scope_lines)

    with pytest.raises(ProposalRenderError, match="missing or out of order"):
        pdx.validate_output(
            out, gc_name=CTX.gc_name, scope_lines=CTX.scope_lines + ("Never inserted line.",)
        )

    # unreplaced placeholder caught (raw template has them everywhere)
    with pytest.raises(ProposalRenderError, match="placeholder"):
        pdx.validate_output(template, gc_name=CTX.gc_name, scope_lines=())

    # ANY surviving angle bracket fails — including tokens the old regex
    # guard missed, like "<10 amp>" or "< Custom>" (review finding)
    leaky = replace(CTX, scope_lines=CTX.scope_lines + ("Furnish and install <10 amp> breakers.",))
    out_leaky = pdx.render_proposal(template, leaky)
    with pytest.raises(ProposalRenderError, match="Angle-bracket"):
        pdx.validate_output(out_leaky, gc_name=CTX.gc_name, scope_lines=leaky.scope_lines)


@needs_template
def test_validate_output_isolation_negative_check():
    out = pdx.render_proposal(pdx.TEMPLATE_PATH.read_bytes(), CTX)

    # a different GC's name in the doc would be caught if it appeared…
    pdx.validate_output(
        out,
        gc_name=CTX.gc_name,
        scope_lines=CTX.scope_lines,
        other_gc_names=("Turner Construction",),
    )
    # …and IS caught when it actually appears: a Taylor doc whose scope text
    # accidentally carries Turner's name must refuse to validate.
    leaky = replace(
        CTX, scope_lines=CTX.scope_lines + ("Coordinate with Turner Construction.",)
    )
    out_leaky = pdx.render_proposal(pdx.TEMPLATE_PATH.read_bytes(), leaky)
    with pytest.raises(ProposalRenderError, match="ISOLATION"):
        pdx.validate_output(
            out_leaky,
            gc_name=CTX.gc_name,
            scope_lines=leaky.scope_lines,
            other_gc_names=("Turner Construction",),
        )
    # substring-contained names are skipped (cannot be distinguished), not failed
    pdx.validate_output(
        out,
        gc_name=CTX.gc_name,
        scope_lines=CTX.scope_lines,
        other_gc_names=("Taylor International",),  # contained in target name
    )


@needs_template
def test_render_for_two_gcs_differs_only_by_gc():
    template = pdx.TEMPLATE_PATH.read_bytes()
    a = pdx.render_proposal(template, CTX)
    b = pdx.render_proposal(template, replace(CTX, gc_name="Turner Construction"))
    ta, tb = pdx.extract_document_text(a), pdx.extract_document_text(b)
    assert "Taylor International Corp." in ta and "Taylor International Corp." not in tb
    assert "Turner Construction" in tb and "Turner Construction" not in ta
    assert ta.replace("Taylor International Corp.", "X") == tb.replace("Turner Construction", "X")


@needs_template
def test_template_asset_has_expected_anchors_and_placeholders():
    """Guards the committed asset itself: if the user re-saves it in Word and
    something drifts, this fails before any runtime code does."""
    raw = pdx.TEMPLATE_PATH.read_bytes()
    text = pdx.extract_document_text(raw)
    for ph in pdx.ALL_PLACEHOLDERS:
        assert ph in text, f"template lost placeholder {ph!r}"
    doc = Document(io.BytesIO(raw))
    _, mode = pdx.find_scope_anchor(doc)
    assert mode == "after"
    # the green chart OLE object must be gone (replaced by the native table)
    assert all(p._p.find(".//" + qn("w:object")) is None for p in doc.paragraphs)


# ── PDF leak re-scan (validate_pdf_isolation) ───────────────────────────────
#
# Operates on already-extracted PDF text (plain strings) — no PDF/gotenberg
# needed. This re-proves isolation on the artifact that is actually emailed.

PDF_OK = (
    "Proposal for Taylor International Corp. "
    "Material $41,188 Labor $40,950 Total $82,138"
)


def test_validate_pdf_isolation_passes():
    pdx.validate_pdf_isolation(
        PDF_OK,
        gc_name="Taylor International Corp.",
        other_gc_names=("Turner Construction",),
        amounts=("$41,188", "$40,950", "$82,138"),
    )


def test_validate_pdf_isolation_missing_gc_name_raises():
    with pytest.raises(ProposalRenderError, match="not present in the rendered PDF"):
        pdx.validate_pdf_isolation(PDF_OK, gc_name="Turner Construction")


def test_validate_pdf_isolation_missing_amount_raises():
    with pytest.raises(ProposalRenderError, match="missing from the rendered PDF"):
        pdx.validate_pdf_isolation(
            PDF_OK, gc_name="Taylor International Corp.", amounts=("$99,999",)
        )


def test_validate_pdf_isolation_other_gc_name_leak_raises():
    leak = PDF_OK + " Coordinate with Turner Construction."
    with pytest.raises(ProposalRenderError, match="ISOLATION"):
        pdx.validate_pdf_isolation(
            leak,
            gc_name="Taylor International Corp.",
            other_gc_names=("Turner Construction",),
        )


def test_validate_pdf_isolation_skips_contained_names():
    # "Taylor International" is a substring of the target — can't be told apart,
    # so it's skipped (the positive name check still pins the right GC).
    pdx.validate_pdf_isolation(
        PDF_OK,
        gc_name="Taylor International Corp.",
        other_gc_names=("Taylor International",),
    )


def test_validate_pdf_isolation_normalizes_wrapped_name():
    # A GC name that wrapped across PDF lines (newlines/extra spaces) still
    # matches once whitespace is collapsed — never false-blocks a real send.
    pdx.validate_pdf_isolation(
        "Proposal for Taylor International Corp. see attached",
        gc_name="Taylor   International\nCorp.",
    )

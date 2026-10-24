"""app/services/vendor_quotes.py: the quote PDF parser, the notes
classifier, the reconciliation and the selection rule. Synthetic quote
PDFs are built here (one page, Helvetica, hand-written PDF syntax) with
made-up amounts; the golden-marked test reads the two 7154 backup quotes
and the expected counts, with every dollar figure read from
EVAL_ROOT/references/7154/expected.json, never typed here."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.services import estimating_contracts as ec
from app.services import vendor_quotes as vq
from app.services.pricing_types import PriceRow

EVAL_ROOT = Path("/Users/thomasmoore/Desktop/BDR/backups/eval")
BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
_QUOTE_DIR = BACKUP / "storage" / "project-files" / "7dfc64e6-36ef-4c67-9d02-0d7942015011" / "quote"
_HAVE_DATA = (EVAL_ROOT / "references" / "7154" / "expected.json").is_file() and _QUOTE_DIR.is_dir()


# ── Synthetic PDFs ────────────────────────────────────────────────────────


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(lines: list[str]) -> bytes:
    """A one-page PDF whose text layer holds `lines`, one per text line."""
    content = "BT /F1 10 Tf 40 760 Td 13 TL " + " ".join(f"({_esc(ln)}) Tj T*" for ln in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets: list[int] = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


CODALE_STYLE = [
    "Date: Aug 28, 2026 Quote: ACME26-10001-1 Quote",
    "Acme Electric Supply",
    "Project TEST PHARMACY",
    "QTY Type",
    "21 L1 _",
    "1 L2 _",
    "16 L3 _",
    "16 L3 _",
    "16 L3 _",
    "3 L4 _",
    "50 L5 _",
    "50 FEET",
    "2 X1 _",
    "1 CONTROLS _",
    "1 SERVICES _",
    "SUBTOTAL $12,345.00",
    "Notes:",
    "1. LAMPS/LEDS INCLUDED",
    "2. GROUND FREIGHT INCLUDED FOR QUANTITIES LISTED",
    "3. LIGHTING CONTROL SYSTEM INCLUDED",
    "4. THIRD PARTY TESTING/COMMISSIONING OF LIGHTING CONTROL INCLUDED",
    "5. SALES TAX IS NOT INCLUDED",
    "6. EM BATTERIES NOT INCLUDED. EM ON LIFE SAFETY CIRCUIT PER PLANS",
    "G. Pre-shipment of anchor bolts is plus freight.",
]

GRAYBAR_STYLE = [
    "PREPARED BY",
    "Some Person - Graybar - Las Vegas",
    "Qty Type Model Number & Notes",
    "21 L1",
    "1 L2",
    "16 L3",
    "3 L4 WAC BL-29903-FINISH TBD OR EQUAL",
    "50 L5 (PER FOOT UNDERCAB)",
    "2 X1",
    "1 CONTROLS LOT LIGHTING CONTROLS -INCLUDES 1/2 OF COMMISSIONING",
    "- NOTE EM BATTERIES NOT INCLUDED. EM ON LIFE SAFETY CIRCUIT PER PLANS",
    "Grand Total $13,000.00",
    "1. FREIGHT INCLUDED.",
    "10. SALES TAX NOT INCLUDED.",
    "Job Name : TEST REMODEL Quote Number : 123456-7890-",
    "1",
]


def _row(i: int, desc: str, group: str, unit: str, qty: float, *, section: str = "NEW WORK", subgroup: str | None = None,
         material_category: str | None = None) -> PriceRow:
    return PriceRow(
        line_id=f"B{i}", description=desc, group=group, subgroup=subgroup, section=section, unit=unit,
        quantity=Decimal(str(qty)), qty_base=Decimal(str(qty)), wastage_applies=False,
        material_category=material_category,
    )


def _lighting_rows() -> list[PriceRow]:
    return [
        _row(1, "L1, 2X4 RECESSED LED TROFFER", "LIGHTING FIXTURES", "EA", 16, subgroup="FIXTURES", material_category="Lighting"),
        _row(2, "L1, 2X4 RECESSED LED TROFFER, LIFE SAFETY", "LIGHTING FIXTURES", "EA", 5, subgroup="FIXTURES", material_category="Lighting"),
        _row(3, "L2, 2X2 RECESSED LED TROFFER", "LIGHTING FIXTURES", "EA", 1, subgroup="FIXTURES", material_category="Lighting"),
        _row(4, "L3, 6-INCH RECESSED LED DOWNLIGHT", "LIGHTING FIXTURES", "EA", 13, subgroup="FIXTURES", material_category="Lighting"),
        _row(5, "L3, 6-INCH RECESSED LED DOWNLIGHT, SHADED", "LIGHTING FIXTURES", "EA", 3, subgroup="FIXTURES", material_category="Lighting"),
        _row(6, "L4, WALL-MOUNTED LED TASK LIGHT", "LIGHTING FIXTURES", "EA", 3, subgroup="FIXTURES", material_category="Lighting"),
        _row(7, "L5, UNDERCABINET LED LIGHTING RUN", "LIGHTING FIXTURES", "EA", 7, subgroup="FIXTURES", material_category="Lighting"),
        _row(8, "X1, LED EDGE-LIT EXIT SIGN", "LIGHTING FIXTURES", "EA", 2, subgroup="FIXTURES", material_category="Lighting"),
        _row(9, "CEILING OCCUPANCY SENSOR", "LIGHTING CONTROLS", "EA", 3, subgroup="CONTROLS", material_category="Lighting"),
        _row(10, "WALL DIMMER", "LIGHTING CONTROLS", "EA", 10, subgroup="CONTROLS", material_category="Lighting"),
        _row(11, "DUPLEX RECEPTACLE", "OTHER ITEMS", "EA", 20, subgroup="RECEPTACLES"),
    ]


# ── Parser ───────────────────────────────────────────────────────────────


def test_pdf_text_parser_collapses_duplicates_and_reads_the_feet_continuation():
    quote = vq.parse_quote_pdf(make_pdf(CODALE_STYLE), filename="Acme Lighting Quote.pdf")
    assert quote.status == "parsed" and quote.parser == "text"
    assert quote.quote_number == "ACME26-10001-1"
    assert quote.amount_pretax == Decimal("12345.00")
    marks = {ln.mark: (ln.qty, ln.uom) for ln in quote.fixture_lines()}
    assert marks["L3"] == (Decimal("16"), "EA")  # three identical "16 L3" lines are ONE sixteen
    assert sum(1 for ln in quote.lines if ln.mark == "L3") == 1
    assert "duplicate_lines_collapsed:L3 x3" in quote.warnings
    assert marks["L5"] == (Decimal("50"), "FT")  # "50 FEET" continuation sets FT
    assert marks["L1"] == (Decimal("21"), "EA") and marks["X1"] == (Decimal("2"), "EA")
    kinds = {ln.mark: ln.kind for ln in quote.lines}
    assert kinds["CONTROLS"] == "control" and kinds["SERVICES"] == "service"
    assert all(ln.lot for ln in quote.lines if ln.kind != "fixture")


def test_notes_classifier_reads_tax_em_controls_commissioning_and_freight():
    quote = vq.parse_quote_pdf(make_pdf(CODALE_STYLE))
    n = quote.notes
    assert quote.tax_included is False and n.tax_included is False
    assert n.em_batteries_excluded is True
    assert n.controls_included is True
    assert n.commissioning == "included"
    assert n.freight_included is True  # the numbered note wins over the terms' "plus freight"
    assert n.lot_priced is True
    graybar = vq.parse_quote_pdf(make_pdf(GRAYBAR_STYLE))
    assert graybar.vendor == "Graybar"
    assert graybar.quote_number == "123456-7890-1"  # the hyphen break joins
    assert graybar.amount_pretax == Decimal("13000.00")
    assert graybar.notes.commissioning == "partial"
    assert graybar.notes.tax_included is False
    assert {ln.mark: ln.uom for ln in graybar.fixture_lines()}["L5"] == "FT"  # "(PER FOOT ...)"
    assert graybar.warnings == []


def test_tax_included_note_and_unknown_tax_are_distinguished():
    yes = vq.parse_quote_text(["QTY Type", "2 A1", "TOTAL $100.00", "SALES TAX INCLUDED"])
    assert yes.tax_included is True
    unknown = vq.parse_quote_text(["QTY Type", "2 A1", "TOTAL $100.00"])
    assert unknown.tax_included is None
    assert vq.quote_ref("q", unknown, category="Lighting").tax_included is False  # unanswered = not included


def test_scanned_quote_needs_review_and_the_fallback_runs_only_when_enabled():
    scanned = make_pdf([])
    calls: list[int] = []

    def fallback(pdf: bytes, pages):
        calls.append(len(pages))
        return {
            "vendor": "Scanned Vendor", "quote_number": "S-1", "amount_pretax": 999.5, "tax_included": False,
            "lines": [{"qty": 4, "mark": "L1", "uom": "EA", "text": "", "kind": "fixture", "lot": False}],
            "notes": {"tax_included": False, "em_batteries_excluded": False, "controls_included": None, "commissioning": "unknown"},
        }

    off = vq.parse_quote_pdf(scanned, fallback_enabled=False, llm_fallback=fallback)
    assert off.status == "needs_review" and "no_text_layer" in off.warnings and calls == []
    injected_only = vq.parse_quote_pdf(scanned, fallback_enabled=True, llm_fallback=None)
    assert injected_only.status == "needs_review" and calls == []
    on = vq.parse_quote_pdf(scanned, fallback_enabled=True, llm_fallback=fallback)
    assert calls == [1]
    assert on.parser == "llm" and on.status == "parsed" and on.amount_pretax == Decimal("999.5")
    assert on.fixture_lines()[0].mark == "L1"
    assert "llm_fallback" in on.warnings and "no_text_layer" in on.warnings


def test_parsed_quote_round_trips_through_the_extraction_row():
    quote = vq.parse_quote_pdf(make_pdf(GRAYBAR_STYLE))
    row = quote.to_row()
    back = vq.ParsedQuote.from_row({**row, "id": "x"})
    assert back.amount_pretax == quote.amount_pretax
    assert [ln.to_dict() for ln in back.lines] == [ln.to_dict() for ln in quote.lines]
    assert back.notes.to_dict() == quote.notes.to_dict()


# ── Reconciliation ─────────────────────────────────────────────────────────


def test_boq_marks_fold_em_variants_into_the_base_mark():
    assert vq.boq_fixture_mark("L1, 2X4 RECESSED LED TROFFER, LIFE SAFETY") == ("L1", True)
    assert vq.boq_fixture_mark("L3, 6-INCH RECESSED LED DOWNLIGHT, SHADED") == ("L3", True)
    assert vq.boq_fixture_mark("L1-EM, 2X4 TROFFER WITH BATTERY") == ("L1", True)
    assert vq.boq_fixture_mark("X1, LED EDGE-LIT EXIT SIGN") == ("X1", False)
    assert vq.boq_fixture_mark("CEILING OCCUPANCY SENSOR") == (None, False)
    assert vq.fold_em_mark("L1E") == "L1" and vq.fold_em_mark("L10") == "L10"


def test_lighting_reconciliation_folds_em_blocks_uom_conflict_and_flags_em_premium():
    quote = vq.parse_quote_pdf(make_pdf(GRAYBAR_STYLE))
    rec = vq.reconcile(quote, _lighting_rows(), category="Lighting", run_id="run-1", output_version=1)
    by_mark = {m.mark: m for m in rec.matches}
    assert by_mark["L1"].boq_qty == 21 and by_mark["L1"].status == "match" and by_mark["L1"].em_line_ids == ("B2",)
    assert by_mark["L3"].boq_qty == 16 and by_mark["L3"].status == "match" and by_mark["L3"].em_line_ids == ("B5",)
    assert by_mark["L5"].status == "uom_conflict"
    assert rec.blocked_line_ids == ("B7",)
    assert len(rec.findings) == 1
    f = rec.findings[0]
    assert f.gate_id == vq.GATE_UOM_CONFLICT and f.severity == "block" and f.route == "measure" and f.stage == "pricing"
    assert f.target["line_id"] == "B7" and f.run_id == "run-1" and f.evidence["quote_uom"] == "FT"
    assert vq.FLAG_EM_PREMIUM in rec.flags
    assert vq.FLAG_CONTROLS_COVERED in rec.flags  # the quote carries a CONTROLS lot
    assert "B9" in rec.covered_line_ids and "B10" in rec.covered_line_ids
    assert "B11" not in rec.covered_line_ids  # the receptacle is General Material, not this category
    assert rec.eligible


def test_control_rows_not_in_the_quote_keep_library_material():
    text = ["QTY Type", "21 L1", "1 L2", "16 L3", "3 L4", "7 L5", "2 X1", "SUBTOTAL $5,000.00", "1. SALES TAX NOT INCLUDED", "2. CONTROLS NOT INCLUDED"]
    quote = vq.parse_quote_text(text)
    assert quote.notes.controls_included is False
    rec = vq.reconcile(quote, _lighting_rows(), category="Lighting")
    assert vq.FLAG_NOT_IN_QUOTE in rec.flags
    assert {"B9", "B10"} <= set(rec.not_in_quote_line_ids)
    assert rec.blocked_line_ids == () and rec.findings == []
    assert vq.FLAG_EM_PREMIUM not in rec.flags  # the quote says nothing about EM batteries


def test_switchgear_reconciliation_matches_designations_and_flags_manual():
    rows = [
        _row(1, "PANEL LP1, 225A MLO, 42 CIRCUIT", "SWITCHGEAR", "EA", 1, subgroup="PANELS", material_category="Switchgear"),
        _row(2, "PANEL HP2, 400A MCB", "SWITCHGEAR", "EA", 1, subgroup="PANELS", material_category="Switchgear"),
        _row(3, "20A/1P BREAKER", "SWITCHGEAR", "EA", 12, subgroup="BREAKERS", material_category="Switchgear"),
    ]
    quote = vq.parse_quote_text(["QTY Type", "1 LP1 PANELBOARD 225A", "12 QO120 BREAKERS", "TOTAL $9,000.00", "TAX NOT INCLUDED"])
    rec = vq.reconcile(quote, rows, category="Switchgear")
    assert rec.kind == "switchgear"
    assert "B1" in rec.covered_line_ids and "B3" in rec.covered_line_ids
    assert rec.not_in_quote_line_ids == ("B2",)
    assert vq.FLAG_MANUAL_ELIGIBLE in rec.flags


def test_subcontract_reconciliation_follows_the_policy_and_keeps_wire_in_general_material():
    rows = [
        _row(1, "TRENCH 24 IN DEEP", "OTHER ITEMS", "FT", 100, subgroup="TRENCHING"),
        _row(2, '2"C PVC SCH 40', "WIRING", "FT", 120, subgroup="CONDUITS", material_category="Trenching"),
    ]
    quote = vq.parse_quote_text(["QTY Type", "100 TRENCHING LF", "TOTAL $2,000.00"])
    rec = vq.reconcile(quote, rows, category="Trenching")
    assert rec.kind == "subcontract" and rec.mode == "material"
    assert rec.covered_line_ids == ("B1",)
    assert rec.not_in_quote_line_ids == ("B2",)  # conduit stays General Material unless the quote names it


def test_selection_prefers_the_eligible_human_choice_else_lowest_tax_inclusive():
    cheap_untaxed = vq.ParsedQuote(vendor="A", amount_pretax=Decimal("1000"), tax_included=False)
    taxed = vq.ParsedQuote(vendor="B", amount_pretax=Decimal("1060"), tax_included=True)
    failed = vq.ParsedQuote(vendor="C", amount_pretax=Decimal("1"), status="failed")
    cands = [
        (vq.quote_ref("a", cheap_untaxed, category="Lighting"), cheap_untaxed),
        (vq.quote_ref("b", taxed, category="Lighting"), taxed),
        (vq.quote_ref("c", failed, category="Lighting"), failed),
    ]
    refs, chosen = vq.select_quotes(cands)
    assert [r.quote_id for r in refs] == ["a", "b"]
    assert chosen["Lighting"][0].quote_id == "b"  # 1060 taxed beats 1000 + 8.375% tax
    refs, chosen = vq.select_quotes(cands, human_selected=["a"])
    assert chosen["Lighting"][0].quote_id == "a"
    refs, chosen = vq.select_quotes(cands, human_selected=["c"])
    assert chosen["Lighting"][0].quote_id == "b"  # an ineligible selection falls back to the rule


def test_record_hands_findings_to_the_injected_store_with_stage_pricing():
    quote = vq.parse_quote_pdf(make_pdf(GRAYBAR_STYLE))
    rec = vq.reconcile(quote, _lighting_rows(), category="Lighting", run_id="run-1")
    store = ec.InMemoryFindingsStore()
    stored = vq.record(None, {"id": "run-1"}, [rec], store.record_findings)
    assert len(stored) == 1 and stored[0].stage == "pricing"
    assert store.for_run("run-1", stage="pricing")[0].gate_id == "uom_conflict_quote"
    assert vq.record(None, {"id": "run-1"}, [], store.record_findings) == []


# ── Golden: the 7154 quotes ───────────────────────────────────────────────


@pytest.mark.golden
@pytest.mark.skipif(not _HAVE_DATA, reason="backup or EVAL_ROOT not present")
def test_7154_backup_quotes_parse_to_the_acceptance_counts():
    expected = json.loads((EVAL_ROOT / "references" / "7154" / "expected.json").read_text())
    files = {p.name.split("-", 1)[1]: p for p in _QUOTE_DIR.glob("*.pdf")}
    codale = vq.parse_quote_pdf(files["Codale Lighting Quote.pdf"].read_bytes(), filename="Codale Lighting Quote.pdf")
    graybar = vq.parse_quote_pdf(files["Graybar Lighting Quote.pdf"].read_bytes(), filename="Graybar Lighting Quote.pdf")
    assert codale.status == "parsed" and graybar.status == "parsed"
    assert codale.amount_pretax == Decimal(str(expected["quotes"]["codale_pretax_usd"]))
    assert graybar.amount_pretax == Decimal(str(expected["quotes"]["graybar_pretax_usd"]))
    assert codale.quote_number == expected["quotes"]["codale_quote_number"]
    assert graybar.quote_number == expected["quotes"]["graybar_quote_number"]
    assert "duplicate_lines_collapsed:L3 x3" in codale.warnings
    for quote in (codale, graybar):
        marks = quote.marks()
        assert marks["L5"].qty == 50 and marks["L5"].uom == "FT"
        assert marks["L1"].qty == 21 and marks["L3"].qty == 16
        assert quote.tax_included is False and quote.notes.em_batteries_excluded
    assert codale.notes.commissioning == "included" and graybar.notes.commissioning == "partial"
    # The stored AI BOQ (L5 7 EA) against either quote: L1 21 and L3 16 after
    # EM folding, L5 blocks with uom_conflict_quote, em_premium_check raised.
    for quote in (codale, graybar):
        rec = vq.reconcile(quote, _lighting_rows(), category="Lighting", run_id="7154")
        by_mark = {m.mark: m for m in rec.matches}
        assert by_mark["L1"].boq_qty == 21 and by_mark["L1"].quote_qty == 21
        assert by_mark["L3"].boq_qty == 16 and by_mark["L3"].quote_qty == 16
        assert by_mark["L5"].status == "uom_conflict" and rec.blocked_line_ids == ("B7",)
        assert [f.gate_id for f in rec.findings] == ["uom_conflict_quote"]
        assert vq.FLAG_EM_PREMIUM in rec.flags
    refs, chosen = vq.select_quotes([
        (vq.quote_ref("codale", codale, category="Lighting"), codale),
        (vq.quote_ref("graybar", graybar, category="Lighting"), graybar),
    ])
    lowest = min((codale.amount_pretax, "codale"), (graybar.amount_pretax, "graybar"))[1]
    assert chosen["Lighting"][0].quote_id == lowest

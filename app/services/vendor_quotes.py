"""Vendor quote PDFs: the text parser, the notes classifier and the
reconciliation of a quote against the BOQ (AI_ESTIMATING_BUILD_SPEC WS6
`vendor_quotes`, acceptance "7154 quotes parse").

Parsing (pure over the page texts; `extract_pages` is the only PDFium
touch and takes `pdf_split._PDFIUM_LOCK`):

1. the QTY window: from the first line that starts with QTY (the item
   table header, "QTY Type" or "Qty Type Model Number & Notes") to the
   SUBTOTAL, GRAND TOTAL or TOTAL line, whose dollar figure is the quote's
   pre-tax amount;
2. one item per `<qty> <TYPE> <rest>` line; a `<qty> FEET` (FT, LF) line
   right after an item is a continuation that sets the item's unit to FT
   (Codale prints "50 L5" then "50 FEET"); "(PER FOOT ...)" in the rest
   does the same (Graybar);
3. exact duplicates (same quantity, type, unit and text) collapse to one
   line with a warning: Codale printed "16 L3" three times for one 16;
4. the notes classifier reads the whole text: sales tax included or not,
   EM batteries excluded, lighting controls included, commissioning
   (included, partial, excluded), freight, spares, and whether the quote
   is lot priced (a CONTROLS or SERVICES line with a lot note);
5. a PDF without a usable text layer, or one where no item and no amount
   was found, is `needs_review`; with `quote_lines_llm_fallback_enabled`
   the caller may hand such a quote to the injected LLM caller (feature
   `pricing_quote` through app/services/llm.py), never made live here.

Reconciliation per category kind (the WS6 design paragraph):

- Lighting: BOQ fixture marks (L1, X1) against quote marks, EM variants
  folded into their base mark (L1-EM, "LIFE SAFETY", "SHADED" per the
  7154 adjudication alias); control rows not in the quote keep library
  material with flag `not_in_quote`; a quote per foot against a BOQ EA
  row (or the reverse) is a `uom_conflict_quote` finding (block, route
  measure) recorded through the injected WS4 `record_findings` with stage
  pricing; `em_premium_check` when EM fixtures were folded and the quote
  excludes EM batteries;
- Switchgear: equipment designations (LP1, MSB) from the BOQ against the
  quote; an unmatched designation flags the category `manual_eligible`;
- subcontract categories (Trenching, Low Voltage, ...): by policy
  (`replaces: material | material_and_labor | adds`); conduit and wire
  rows stay General Material unless the quote names them.

Selection: the human-selected quote when eligible, else the lowest
tax-inclusive amount (Q9, `pricing_engine.choose_quotes`). Everything here
is pure apart from `extract_pages`; database reads and writes live in
`pricing_run` and the router. Pricing data is internal: tests build quotes
from synthetic PDFs and synthetic amounts.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services import estimating_contracts as ec
from app.services import pricing_engine as pe
from app.services.electrical_items import parse_item
from app.services.pricing_policy import DEFAULT_POLICY, PricingPolicy
from app.services.pricing_types import PriceRow, QuoteRef

logger = logging.getLogger(__name__)

QUOTE_PARSER_VERSION = 1
QUOTE_UOMS: tuple[str, ...] = ("EA", "FT", "LS")
QUOTE_STATUSES: tuple[str, ...] = ("parsed", "needs_review", "failed")
LINE_KINDS: tuple[str, ...] = ("fixture", "control", "service", "other")
GATE_UOM_CONFLICT = "uom_conflict_quote"
FLAG_EM_PREMIUM = "em_premium_check"
FLAG_NOT_IN_QUOTE = "not_in_quote"
FLAG_MANUAL_ELIGIBLE = "manual_eligible"
FLAG_CONTROLS_COVERED = "controls_covered"
FLAG_COUNT_MISMATCH = "count_mismatch"
MIN_TEXT_CHARS = 80
CATEGORY_KINDS: dict[str, str] = {
    "Lighting": "lighting",
    "Switchgear": "switchgear",
    "Generator & Equipment": "switchgear",
}
SUBCONTRACT_CATEGORIES: frozenset[str] = frozenset({
    "Trenching", "Low Voltage", "EV Chargers", "Solar", "Demolition", "Other",
})
# Quote text excerpts in warnings and tool results are capped (never a page).
_EXCERPT = 120

# Regexes (all on upper-cased text) ------------------------------------------------------

_QTY_HEADER_RE = re.compile(r"^\s*QTY\b", re.IGNORECASE)
_TOTAL_RE = re.compile(
    r"^\s*(?:SUB\s*TOTAL|GRAND\s+TOTAL|TOTAL(?:\s+PRICE)?|NET\s+TOTAL)\b[^$\d]*\$?\s*([\d,]+(?:\.\d{2})?)",
    re.IGNORECASE,
)
_ANY_TOTAL_RE = re.compile(
    r"(?:SUB\s*TOTAL|GRAND\s+TOTAL|TOTAL(?:\s+PRICE)?)\s*:?\s*\$\s*([\d,]+(?:\.\d{2})?)", re.IGNORECASE
)
_ITEM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s+([A-Z][A-Z0-9/.-]{0,11})\b\s*(.*)$")
_FEET_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)?\s*(?:FEET|FT\.?|LF|LIN(?:EAR)?\.?\s*(?:FT|FEET))\b\s*(.*)$")
_PER_FOOT_RE = re.compile(r"\bPER\s*(?:FOOT|FT|LF)\b|/\s*(?:FT|LF)\b|\bLINEAR\s+(?:FEET|FT)\b|\bFEET\b")
_NOTE_LINE_RE = re.compile(r"^\s*(?:-|\*|NOTE\b)")
_FIXTURE_MARK_RE = re.compile(r"^[A-Z]{1,3}\d{0,3}[A-Z]?$")
_UNIT_WORDS: frozenset[str] = frozenset({"FEET", "FT", "LF", "EA", "EACH", "LOT", "LS", "PCS", "PC"})
_CONTROL_WORDS = re.compile(r"\bCONTROLS?\b|\bLIGHTING\s+CONTROL")
_SERVICE_WORDS = re.compile(r"\bSERVICES?\b|\bCOMMISSIONING\b|\bSTART-?UP\b|\bPROGRAMMING\b")
_LOT_RE = re.compile(r"\bLOT\b")
_QUOTE_NUMBER_RE = re.compile(
    r"QUOTE(?:\s*(?:NUMBER|NO\.?|#))?\s*:?\s*([A-Z0-9][A-Z0-9-]{3,})", re.IGNORECASE
)
_VENDORS: tuple[tuple[str, str], ...] = (
    (r"\bCODALE\b", "Codale Electric Supply"),
    (r"\bGRAYBAR\b", "Graybar"),
    (r"\bCONSOLIDATED\s+ELECTRICAL\b|\bCED\b", "Consolidated Electrical Distributors"),
    (r"\bWESCO\b", "Wesco"),
    (r"\bREXEL\b", "Rexel"),
    (r"\bPLATT\b", "Platt Electric Supply"),
    (r"\bBORDER\s+STATES\b", "Border States"),
    (r"\bMAYER\b", "Mayer Electric"),
    (r"\bCRESCENT\b", "Crescent Electric Supply"),
    (r"\bCITY\s+ELECTRIC\b", "City Electric Supply"),
    (r"\bSUMMIT\s+ELECTRIC\b", "Summit Electric Supply"),
)
_SUPPLY_LINE_RE = re.compile(r"^[A-Z0-9 .,&'-]{3,60}\b(?:ELECTRIC(?:AL)?\s+SUPPLY|LIGHTING|DISTRIBUTORS?)\b")

_TAX_NOT_RE = re.compile(
    r"SALES\s+TAX\s+(?:IS\s+|ARE\s+)?NOT\s+INCLUDED|\bTAX(?:ES)?\s+(?:IS\s+|ARE\s+)?NOT\s+INCLUDED"
    r"|\bPLUS\s+(?:APPLICABLE\s+|SALES\s+)?TAX|\bEXCLUDES?\s+(?:APPLICABLE\s+|SALES\s+)?TAX"
    r"|\bTAX(?:ES)?\s+EXCLUDED|\bNO\s+SALES\s+TAX\s+INCLUDED"
)
_TAX_YES_RE = re.compile(
    r"(?<!NOT )(?:SALES\s+)?TAX(?:ES)?\s+(?:IS\s+|ARE\s+)?INCLUDED|\bINCLUDES?\s+(?:APPLICABLE\s+|SALES\s+)?TAX"
    r"|\bTAX\s+INCL\b"
)
_EM_EXCLUDED_RE = re.compile(
    r"EM\s+BATTER(?:Y|IES)\s+(?:ARE\s+)?NOT\s+INCLUDED|BATTERY\s+PACKS?\s+(?:ARE\s+)?NOT\s+INCLUDED"
    r"|\bEM\s+(?:IS\s+)?NOT\s+INCLUDED|EMERGENCY\s+BATTER(?:Y|IES)\s+(?:ARE\s+)?NOT\s+INCLUDED"
)
_CONTROLS_YES_RE = re.compile(
    r"LIGHTING\s+CONTROLS?(?:\s+SYSTEM)?(?:\s+&\s+SERVICES)?\s+(?:ARE\s+|IS\s+)?INCLUDED|\bLOT\s+LIGHTING\s+CONTROLS"
)
_CONTROLS_NO_RE = re.compile(
    r"(?:LIGHTING\s+)?CONTROLS?\s+(?:ARE\s+|IS\s+)?NOT\s+INCLUDED|CONTROLS?\s+BY\s+OTHERS|EXCLUDES?\s+(?:LIGHTING\s+)?CONTROLS"
)
_COMMISSIONING_PARTIAL_RE = re.compile(r"(?:1/2|HALF|50\s*%)\s+OF\s+(?:THE\s+)?COMMISSIONING")
_COMMISSIONING_NO_RE = re.compile(r"COMMISSIONING\s+(?:IS\s+)?NOT\s+INCLUDED|COMMISSIONING[^.\n]{0,40}EXCLUDED")
_COMMISSIONING_YES_RE = re.compile(r"COMMISSIONING[^.\n]{0,60}INCLUDED|INCLUDES?[^.\n]{0,40}COMMISSIONING")
_FREIGHT_YES_RE = re.compile(r"FREIGHT\s+(?:IS\s+)?INCLUDED")
_FREIGHT_NO_RE = re.compile(r"FREIGHT\s+(?:IS\s+)?NOT\s+INCLUDED|\bPLUS\s+FREIGHT")
_SPARES_NO_RE = re.compile(r"SPARES?(?:\s+MATERIALS?)?\s+(?:ARE\s+)?NOT\s+INCLUDED")
_NUMBERED_NOTE_RE = re.compile(r"^\s*\d{1,2}\.\s+\S")
_EM_DESC_RE = re.compile(r"\bEM\b|\bEMERGENCY\b|\bLIFE\s+SAFETY\b|\bBATTERY\b|\bSHADED\b|\bNIGHT\s*LIGHT\b")
_EM_TAG_RE = re.compile(r"^(?P<base>[A-Z]{1,3}\d{0,3})(?:-?EM|-?E)$")
_BOQ_MARK_RE = re.compile(r"^\s*(?:TYPE\s+)?([A-Z]{1,3}\d{0,3}(?:-?EM|-?E)?)\s*(?:[,:]|\s-\s|\s+-|\s)")
_DESIGNATION_RE = re.compile(
    r"\b(?:PANEL(?:BOARD)?|PNL|SWITCHBOARD|SWBD|MSB|MDP|DISTRIBUTION\s+PANEL|ATS|XFMR|TRANSFORMER|DISCONNECT)"
    r"\s*[\"']?\s*([A-Z]{1,4}-?\d{1,3}[A-Z]?)\b"
)
_LEAD_DESIGNATION_RE = re.compile(r"^\s*([A-Z]{1,4}-?\d{1,3}[A-Z]?)\s*[,:-]")
_WIRE_CONDUIT_RE = re.compile(r"\bCONDUIT\b|\bWIRE\b|\bTHHN\b|\bEMT\b|\bPVC\b|\bCONDUCTORS?\b")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Shapes ------------------------------------------------------------------------------------


@dataclass
class QuoteLine:
    """One item of the quote's QTY window."""

    qty: Decimal
    mark: str
    uom: str = "EA"
    text: str = ""
    kind: str = "fixture"
    lot: bool = False
    source_lines: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        self.qty = Decimal(str(self.qty))
        if self.uom not in QUOTE_UOMS:
            raise ValueError(f"QuoteLine.uom {self.uom!r} not in {QUOTE_UOMS}")
        if self.kind not in LINE_KINDS:
            raise ValueError(f"QuoteLine.kind {self.kind!r} not in {LINE_KINDS}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "qty": str(self.qty), "mark": self.mark, "uom": self.uom, "text": self.text,
            "kind": self.kind, "lot": self.lot, "source_lines": list(self.source_lines),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> QuoteLine:
        return cls(
            qty=data["qty"], mark=str(data["mark"]), uom=str(data.get("uom") or "EA"),
            text=str(data.get("text") or ""), kind=str(data.get("kind") or "fixture"),
            lot=bool(data.get("lot")), source_lines=tuple(data.get("source_lines") or ()),
        )


@dataclass
class QuoteNotes:
    """What the notes say about scope (None = the quote does not say)."""

    tax_included: bool | None = None
    em_batteries_excluded: bool = False
    controls_included: bool | None = None
    commissioning: str = "unknown"  # included | partial | excluded | unknown
    freight_included: bool | None = None
    spares_excluded: bool = False
    lot_priced: bool = False
    raw: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "tax_included": self.tax_included,
            "em_batteries_excluded": self.em_batteries_excluded,
            "controls_included": self.controls_included,
            "commissioning": self.commissioning,
            "freight_included": self.freight_included,
            "spares_excluded": self.spares_excluded,
            "lot_priced": self.lot_priced,
            "raw": list(self.raw),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> QuoteNotes:
        data = data or {}
        return cls(
            tax_included=data.get("tax_included"),
            em_batteries_excluded=bool(data.get("em_batteries_excluded")),
            controls_included=data.get("controls_included"),
            commissioning=str(data.get("commissioning") or "unknown"),
            freight_included=data.get("freight_included"),
            spares_excluded=bool(data.get("spares_excluded")),
            lot_priced=bool(data.get("lot_priced")),
            raw=tuple(str(r) for r in data.get("raw") or ()),
        )


@dataclass
class ParsedQuote:
    """The parser's reading of one quote PDF."""

    vendor: str | None = None
    quote_number: str | None = None
    amount_pretax: Decimal | None = None
    tax_included: bool | None = None
    lines: list[QuoteLine] = field(default_factory=list)
    notes: QuoteNotes = field(default_factory=QuoteNotes)
    warnings: list[str] = field(default_factory=list)
    status: str = "parsed"
    parser: str = "text"
    text_quality: str = "text"  # text | sparse | none
    pages: int = 0

    def __post_init__(self) -> None:
        if self.amount_pretax is not None:
            self.amount_pretax = Decimal(str(self.amount_pretax))
        if self.status not in QUOTE_STATUSES:
            raise ValueError(f"ParsedQuote.status {self.status!r} not in {QUOTE_STATUSES}")

    @property
    def eligible(self) -> bool:
        """A quote the engine may price from: parsed with a positive amount."""
        return self.status == "parsed" and self.amount_pretax is not None and self.amount_pretax > 0

    def fixture_lines(self) -> list[QuoteLine]:
        return [ln for ln in self.lines if ln.kind == "fixture"]

    def marks(self) -> dict[str, QuoteLine]:
        """Fixture marks to their (first) line, EM variants folded in by
        `fold_em_marks` upstream; later duplicates of a mark sum."""
        out: dict[str, QuoteLine] = {}
        for ln in self.fixture_lines():
            base = fold_em_mark(ln.mark)
            if base in out:
                prior = out[base]
                out[base] = QuoteLine(
                    qty=prior.qty + ln.qty, mark=base, uom=prior.uom if prior.uom == ln.uom else "FT",
                    text=prior.text, kind="fixture", lot=prior.lot or ln.lot,
                    source_lines=prior.source_lines + ln.source_lines,
                )
            else:
                out[base] = ln
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "quote_number": self.quote_number,
            "amount_pretax": None if self.amount_pretax is None else str(self.amount_pretax),
            "tax_included": self.tax_included,
            "lines": [ln.to_dict() for ln in self.lines],
            "notes": self.notes.to_dict(),
            "warnings": list(self.warnings),
            "status": self.status,
            "parser": self.parser,
            "parser_version": QUOTE_PARSER_VERSION,
            "text_quality": self.text_quality,
            "pages": self.pages,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ParsedQuote:
        return cls(
            vendor=data.get("vendor"),
            quote_number=data.get("quote_number"),
            amount_pretax=data.get("amount_pretax"),
            tax_included=data.get("tax_included"),
            lines=[QuoteLine.from_dict(ln) for ln in data.get("lines") or ()],
            notes=QuoteNotes.from_dict(data.get("notes")),
            warnings=list(data.get("warnings") or ()),
            status=str(data.get("status") or "parsed"),
            parser=str(data.get("parser") or "text"),
            text_quality=str(data.get("text_quality") or "text"),
            pages=int(data.get("pages") or 0),
        )

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ParsedQuote:
        """From a vendor_quote_extractions row (lines, notes and warnings
        as stored by `to_row`)."""
        return cls(
            vendor=row.get("vendor"),
            quote_number=row.get("quote_number"),
            amount_pretax=row.get("amount_pretax"),
            tax_included=row.get("tax_included"),
            lines=[QuoteLine.from_dict(ln) for ln in row.get("lines") or ()],
            notes=QuoteNotes.from_dict(row.get("notes")),
            warnings=list(row.get("warnings") or ()),
            status=str(row.get("status") or "parsed") if row.get("status") in QUOTE_STATUSES else "needs_review",
            parser=str(row.get("parser") or "text"),
        )

    def to_row(self) -> dict[str, Any]:
        """The vendor_quote_extractions columns this parse fills."""
        return {
            "vendor": self.vendor,
            "quote_number": self.quote_number,
            "amount_pretax": None if self.amount_pretax is None else str(self.amount_pretax),
            "tax_included": self.tax_included,
            "parser": self.parser,
            "parser_version": QUOTE_PARSER_VERSION,
            "status": self.status,
            "lines": [ln.to_dict() for ln in self.lines],
            "notes": self.notes.to_dict(),
            "warnings": list(self.warnings),
        }


# PDF text -------------------------------------------------------------------------------------


def extract_pages(pdf_bytes: bytes) -> list[str]:
    """The text of every page (pypdfium2, under the app-wide PDFium lock).
    A page without a text layer yields an empty string."""
    import pypdfium2 as pdfium

    from app.services import pdf_split

    out: list[str] = []
    with pdf_split._PDFIUM_LOCK:
        doc = pdfium.PdfDocument(io.BytesIO(pdf_bytes))
        try:
            for i in range(len(doc)):
                page = doc[i]
                try:
                    out.append(page.get_textpage().get_text_range() or "")
                finally:
                    page.close()
        finally:
            doc.close()
    return out


def _dec(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


def _join_hyphen_breaks(lines: list[str]) -> list[str]:
    """'Quote Number : 676639-1126219-' + '1' prints on two lines."""
    out: list[str] = []
    for line in lines:
        if out and out[-1].rstrip().endswith("-") and re.fullmatch(r"\s*\d+\s*", line or ""):
            out[-1] = out[-1].rstrip() + line.strip()
        else:
            out.append(line)
    return out


def _vendor(lines: Sequence[str], filename: str | None) -> str | None:
    head = "\n".join(lines[:40]).upper()
    for pattern, name in _VENDORS:
        if re.search(pattern, head):
            return name
    for line in lines[:40]:
        if _SUPPLY_LINE_RE.match(line.strip().upper()):
            return line.strip()[:60]
    if filename:
        stem = re.sub(r"\.(pdf|PDF)$", "", filename)
        stem = re.sub(r"(?i)\b(lighting|gear|switchgear|quote|quotation|proposal|bid)\b", " ", stem)
        stem = re.sub(r"[_\-]+", " ", stem).strip()
        if stem:
            return stem[:60]
    return None


def _quote_number(text: str) -> str | None:
    for m in _QUOTE_NUMBER_RE.finditer(text):
        candidate = m.group(1).strip("-")
        if candidate.upper() in ("NUMBER", "START", "DATE", "PAGE"):
            continue
        if re.search(r"\d", candidate):
            return candidate
    return None


def classify_notes(text: str, lines: Sequence[QuoteLine] = ()) -> QuoteNotes:
    """The notes classifier over the whole (upper-cased) text."""
    up = text.upper()
    tax: bool | None
    if _TAX_NOT_RE.search(up):
        tax = False
    elif _TAX_YES_RE.search(up):
        tax = True
    else:
        tax = None
    controls: bool | None
    if _CONTROLS_NO_RE.search(up):
        controls = False
    elif _CONTROLS_YES_RE.search(up) or any(ln.kind == "control" for ln in lines):
        controls = True
    else:
        controls = None
    if _COMMISSIONING_PARTIAL_RE.search(up):
        commissioning = "partial"
    elif _COMMISSIONING_NO_RE.search(up):
        commissioning = "excluded"
    elif _COMMISSIONING_YES_RE.search(up):
        commissioning = "included"
    else:
        commissioning = "unknown"
    freight: bool | None
    if _FREIGHT_YES_RE.search(up):
        freight = True
    elif _FREIGHT_NO_RE.search(up):
        freight = False
    else:
        freight = None
    raw = tuple(
        re.sub(r"\s+", " ", ln.strip())[:200]
        for ln in text.splitlines()
        if _NUMBERED_NOTE_RE.match(ln) or _NOTE_LINE_RE.match(ln)
    )[:40]
    return QuoteNotes(
        tax_included=tax,
        em_batteries_excluded=bool(_EM_EXCLUDED_RE.search(up)),
        controls_included=controls,
        commissioning=commissioning,
        freight_included=freight,
        spares_excluded=bool(_SPARES_NO_RE.search(up)),
        lot_priced=any(ln.lot for ln in lines),
        raw=raw,
    )


def _line_kind(mark: str, rest: str) -> tuple[str, bool]:
    up = f"{mark} {rest}".upper()
    lot = bool(_LOT_RE.search(up))
    if _CONTROL_WORDS.search(up):
        return "control", lot or True
    if _SERVICE_WORDS.search(up):
        return "service", lot or True
    if _FIXTURE_MARK_RE.match(mark):
        return "fixture", lot
    return "other", lot


def parse_quote_text(pages: Sequence[str], *, filename: str | None = None) -> ParsedQuote:
    """Parse the page texts (see the module docstring). Pure."""
    raw_lines: list[str] = []
    for page in pages:
        raw_lines.extend((page or "").splitlines())
    lines = _join_hyphen_breaks(raw_lines)
    text = "\n".join(lines)
    chars = len(re.sub(r"\s+", "", text))
    quote = ParsedQuote(pages=len(pages))

    # The QTY window.
    start = next((i for i, ln in enumerate(lines) if _QTY_HEADER_RE.match(ln)), None)
    end = None
    amount: Decimal | None = None
    if start is not None:
        for j in range(start + 1, len(lines)):
            m = _TOTAL_RE.match(lines[j])
            if m:
                end = j
                amount = _dec(m.group(1))
                break
    if amount is None:
        m = _ANY_TOTAL_RE.search(text)
        if m:
            amount = _dec(m.group(1))
    # No text layer, or too little text to hold a quote (a scanned PDF with
    # a few OCR crumbs): needs review, never a parse of nothing.
    if chars == 0 or (chars < MIN_TEXT_CHARS and start is None and amount is None):
        quote.text_quality = "none" if chars == 0 else "sparse"
        quote.status = "needs_review"
        quote.warnings.append("no_text_layer" if chars == 0 else "sparse_text_layer")
        quote.vendor = _vendor(lines, filename)
        return quote
    quote.vendor = _vendor(lines, filename)
    quote.quote_number = _quote_number(text)
    quote.amount_pretax = amount
    if start is None:
        quote.warnings.append("no_qty_window")
    elif end is None:
        quote.warnings.append("no_total_line")
        end = len(lines)

    items: list[QuoteLine] = []
    if start is not None and end is not None:
        for idx in range(start + 1, end):
            line = lines[idx]
            up = line.upper().strip()
            if not up:
                continue
            feet = _FEET_RE.match(up)
            if feet and items:
                prev = items[-1]
                prev.uom = "FT"
                prev.source_lines = prev.source_lines + (idx,)
                qty = _dec(feet.group(1)) if feet.group(1) else None
                if qty is not None and qty != prev.qty:
                    quote.warnings.append(f"feet_continuation_qty:{prev.mark} {prev.qty} vs {qty}")
                continue
            if _NOTE_LINE_RE.match(up):
                continue
            m = _ITEM_RE.match(up)
            if not m:
                continue
            qty_text, mark, rest = m.group(1), m.group(2), m.group(3).strip()
            if mark in _UNIT_WORDS:
                continue
            qty = _dec(qty_text)
            if qty is None:
                continue
            kind, lot = _line_kind(mark, rest)
            uom = "FT" if _PER_FOOT_RE.search(rest) else "EA"
            if kind in ("control", "service") and lot:
                uom = "LS"
            items.append(QuoteLine(qty=qty, mark=mark, uom=uom, text=rest[:_EXCERPT], kind=kind, lot=lot,
                                   source_lines=(idx,)))

    # Exact duplicates collapse to one line with a warning.
    collapsed: list[QuoteLine] = []
    seen: dict[tuple[str, str, str, str], QuoteLine] = {}
    dup_counts: dict[str, int] = {}
    for ln in items:
        key = (str(ln.qty), ln.mark, ln.uom, re.sub(r"\s+", " ", ln.text).strip("_ ").upper())
        if key in seen:
            seen[key].source_lines = seen[key].source_lines + ln.source_lines
            dup_counts[ln.mark] = dup_counts.get(ln.mark, 1) + 1
            continue
        seen[key] = ln
        collapsed.append(ln)
    for mark, n in dup_counts.items():
        quote.warnings.append(f"duplicate_lines_collapsed:{mark} x{n}")
    quote.lines = collapsed
    quote.notes = classify_notes(text, collapsed)
    quote.tax_included = quote.notes.tax_included
    if not collapsed and amount is None:
        quote.status = "needs_review"
        quote.warnings.append("no_items_found")
    elif amount is None:
        quote.status = "needs_review"
        quote.warnings.append("no_amount_found")
    return quote


FallbackCaller = Callable[[bytes, Sequence[str]], Mapping[str, Any] | None]


def parsed_from_fallback(result: Mapping[str, Any], *, pages: int) -> ParsedQuote:
    """A ParsedQuote from the LLM fallback's structured answer (the shape of
    `FALLBACK_RESPONSE_SCHEMA`); anything malformed is `needs_review`."""
    try:
        lines = []
        for ln in result.get("lines") or ():
            uom = str(ln.get("uom") or "EA").upper()
            lines.append(QuoteLine(
                qty=ln.get("qty") or 0, mark=str(ln.get("mark") or "").upper() or "ITEM",
                uom=uom if uom in QUOTE_UOMS else "EA", text=str(ln.get("text") or "")[:_EXCERPT],
                kind=str(ln.get("kind") or "fixture") if ln.get("kind") in LINE_KINDS else "other",
                lot=bool(ln.get("lot")),
            ))
        notes = QuoteNotes.from_dict(result.get("notes"))
        amount = result.get("amount_pretax")
        quote = ParsedQuote(
            vendor=result.get("vendor"), quote_number=result.get("quote_number"),
            amount_pretax=amount, tax_included=notes.tax_included if notes.tax_included is not None else result.get("tax_included"),
            lines=lines, notes=notes, parser="llm", text_quality="none", pages=pages,
            warnings=["llm_fallback"],
        )
        if quote.amount_pretax is None:
            quote.status = "needs_review"
            quote.warnings.append("no_amount_found")
        return quote
    except (TypeError, ValueError, InvalidOperation) as exc:
        return ParsedQuote(status="needs_review", parser="llm", pages=pages,
                           warnings=["llm_fallback", f"llm_fallback_malformed:{exc}"[:_EXCERPT]])


FALLBACK_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["vendor", "quote_number", "amount_pretax", "tax_included", "lines", "notes"],
    "properties": {
        "vendor": {"type": ["string", "null"]},
        "quote_number": {"type": ["string", "null"]},
        "amount_pretax": {"type": ["number", "null"]},
        "tax_included": {"type": ["boolean", "null"]},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["qty", "mark", "uom", "text", "kind", "lot"],
                "properties": {
                    "qty": {"type": "number"},
                    "mark": {"type": "string"},
                    "uom": {"type": "string", "enum": list(QUOTE_UOMS)},
                    "text": {"type": "string"},
                    "kind": {"type": "string", "enum": list(LINE_KINDS)},
                    "lot": {"type": "boolean"},
                },
            },
        },
        "notes": {
            "type": "object",
            "additionalProperties": False,
            "required": ["tax_included", "em_batteries_excluded", "controls_included", "commissioning"],
            "properties": {
                "tax_included": {"type": ["boolean", "null"]},
                "em_batteries_excluded": {"type": "boolean"},
                "controls_included": {"type": ["boolean", "null"]},
                "commissioning": {"type": "string", "enum": ["included", "partial", "excluded", "unknown"]},
            },
        },
    },
}

FALLBACK_SYSTEM = (
    "You read one electrical vendor quote (lighting, switchgear or a subcontract) and return its "
    "items as data. Quantities and type marks exactly as printed; unit FT only when the quote prices "
    "per foot; LS for a lot. Report the pre-tax total, whether sales tax is included, whether EM "
    "batteries are excluded, whether lighting controls are included and how commissioning is "
    "covered. Never invent an item or an amount; use null when the quote does not say."
)


def llm_quote_fallback(pdf_bytes: bytes, pages: Sequence[str], *, settings: Any = None) -> Mapping[str, Any] | None:
    """The production fallback caller: ONE `pricing_quote` turn through
    app/services/llm.py with the page images (scanned quotes have no text)
    and whatever text exists. Never called in tests (they inject a scripted
    caller); never called at all unless quote_lines_llm_fallback_enabled."""
    from app.services import llm, llm_parts

    parts: list[Any] = [llm_parts.TextPart(text="Vendor quote pages follow. Answer in the required schema.")]
    text = "\n\n".join(p for p in pages if p and p.strip())
    if text:
        parts.append(llm_parts.TextPart(text=text[:20_000]))
    for png in _page_pngs(pdf_bytes, limit=4):
        parts.append(llm_parts.ImagePart(png=png, detail="high"))
    req = llm_parts.ModelRequest(
        feature="pricing_quote",
        system=FALLBACK_SYSTEM,
        messages=(llm_parts.UserTurn(parts=tuple(parts)),),
        tools=(),
        tools_enabled=False,
        response_schema=FALLBACK_RESPONSE_SCHEMA,
        schema_name="vendor_quote",
        step="quote",
    )
    turn = llm.complete_turn("pricing_quote", req, settings=settings)
    return turn.parsed if isinstance(turn.parsed, Mapping) else None


def _page_pngs(pdf_bytes: bytes, *, limit: int, scale: float = 1.4) -> list[bytes]:
    import pypdfium2 as pdfium

    from app.services import pdf_split

    out: list[bytes] = []
    with pdf_split._PDFIUM_LOCK:
        doc = pdfium.PdfDocument(io.BytesIO(pdf_bytes))
        try:
            for i in range(min(len(doc), limit)):
                page = doc[i]
                try:
                    image = page.render(scale=scale).to_pil()
                finally:
                    page.close()
                buf = io.BytesIO()
                image.save(buf, format="PNG")
                out.append(buf.getvalue())
        finally:
            doc.close()
    return out


def parse_quote_pdf(
    pdf_bytes: bytes,
    *,
    filename: str | None = None,
    fallback_enabled: bool = False,
    llm_fallback: FallbackCaller | None = None,
) -> ParsedQuote:
    """Text parse first; when that needs review and the fallback is enabled
    AND a caller was injected, the caller's structured answer replaces it
    (parser `llm`). The text parse's warnings are kept."""
    try:
        pages = extract_pages(pdf_bytes)
    except Exception as exc:  # noqa: BLE001 - a broken PDF is a failed parse, not a crash
        logger.warning("vendor_quotes: could not read %s: %s", filename, exc)
        return ParsedQuote(status="failed", warnings=[f"unreadable_pdf:{str(exc)[:_EXCERPT]}"])
    quote = parse_quote_text(pages, filename=filename)
    if quote.status == "needs_review" and fallback_enabled and llm_fallback is not None:
        try:
            result = llm_fallback(pdf_bytes, pages)
        except Exception as exc:  # noqa: BLE001 - the fallback failing keeps the text parse
            quote.warnings.append(f"llm_fallback_failed:{str(exc)[:_EXCERPT]}")
            return quote
        if result:
            replaced = parsed_from_fallback(result, pages=len(pages))
            replaced.warnings = list(dict.fromkeys([*quote.warnings, *replaced.warnings]))
            if replaced.vendor is None:
                replaced.vendor = quote.vendor
            return replaced
        quote.warnings.append("llm_fallback_empty")
    return quote


# Reconciliation ----------------------------------------------------------------------------------


def fold_em_mark(mark: str) -> str:
    """L1-EM, L1EM, L1E -> L1; everything else unchanged."""
    m = _EM_TAG_RE.match(mark.upper())
    return m.group("base") if m else mark.upper()


def boq_fixture_mark(description: str) -> tuple[str | None, bool]:
    """(base mark, is_em) of a BOQ lighting line: 'L1, 2X4 RECESSED LED
    TROFFER, LIFE SAFETY' -> ('L1', True); 'L3, 6-INCH DOWNLIGHT, SHADED'
    -> ('L3', True) (SHADED = EM, the 7154 alias); 'CEILING OCCUPANCY
    SENSOR' -> (None, False)."""
    up = str(description or "").upper().strip()
    m = _BOQ_MARK_RE.match(up + " ")
    if not m:
        return None, False
    raw = m.group(1)
    base = fold_em_mark(raw)
    em = base != raw or bool(_EM_DESC_RE.search(up[len(raw):]))
    return base, em


def boq_designations(description: str) -> list[str]:
    up = str(description or "").upper()
    out: list[str] = []
    for m in _DESIGNATION_RE.finditer(up):
        d = m.group(1).replace("-", "")
        if d not in out:
            out.append(d)
    m = _LEAD_DESIGNATION_RE.match(up)
    if m:
        d = m.group(1).replace("-", "")
        if d not in out:
            out.append(d)
    return out


@dataclass
class MarkMatch:
    mark: str
    boq_qty: Decimal
    boq_uom: str
    line_ids: tuple[str, ...]
    quote_qty: Decimal | None
    quote_uom: str | None
    status: str  # match | count_mismatch | uom_conflict | not_in_quote | not_in_boq
    em_line_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mark": self.mark, "boq_qty": str(self.boq_qty), "boq_uom": self.boq_uom,
            "line_ids": list(self.line_ids), "quote_qty": None if self.quote_qty is None else str(self.quote_qty),
            "quote_uom": self.quote_uom, "status": self.status, "em_line_ids": list(self.em_line_ids),
        }


@dataclass
class Reconciliation:
    """How one quote maps onto the BOQ lines of its category."""

    category: str
    kind: str
    mode: str
    eligible: bool
    matches: list[MarkMatch] = field(default_factory=list)
    covered_line_ids: tuple[str, ...] = ()
    not_in_quote_line_ids: tuple[str, ...] = ()
    blocked_line_ids: tuple[str, ...] = ()
    not_in_boq_marks: tuple[str, ...] = ()
    findings: list[ec.GateFinding] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category, "kind": self.kind, "mode": self.mode, "eligible": self.eligible,
            "matches": [m.to_dict() for m in self.matches],
            "covered_line_ids": list(self.covered_line_ids),
            "not_in_quote_line_ids": list(self.not_in_quote_line_ids),
            "blocked_line_ids": list(self.blocked_line_ids),
            "not_in_boq_marks": list(self.not_in_boq_marks),
            "findings": [f.to_row() for f in self.findings],
            "flags": list(self.flags), "warnings": list(self.warnings),
        }


def category_rows(rows: Iterable[PriceRow], category: str) -> list[PriceRow]:
    """The BOQ rows that price into `category` (the engine's own mapping)."""
    out: list[PriceRow] = []
    for row in rows:
        identity = parse_item(row.description, row.unit)
        if pe.category_for(row, identity) == category:
            out.append(row)
    return out


def _uom_finding(
    row: PriceRow, mark: str, quote: ParsedQuote, qline: QuoteLine, *, run_id: str | None, output_version: int | None,
) -> ec.GateFinding:
    return ec.GateFinding(
        run_id=run_id,
        output_version=output_version,
        stage="pricing",
        gate_id=GATE_UOM_CONFLICT,
        severity="block",
        route="measure",
        target={"line_id": row.line_id, "mark": mark, "field": "unit"},
        message=(
            f"{mark}: the BOQ counts {row.quantity.normalize()} {row.unit} but the vendor quote prices "
            f"{qline.qty.normalize()} {qline.uom}; measure the run length before pricing this line"
        ),
        evidence={
            "boq_qty": str(row.quantity), "boq_uom": row.unit,
            "quote_qty": str(qline.qty), "quote_uom": qline.uom,
            "vendor": quote.vendor, "quote_number": quote.quote_number,
        },
    )


def _reconcile_lighting(
    quote: ParsedQuote, rows: Sequence[PriceRow], rec: Reconciliation, *, run_id: str | None, output_version: int | None,
) -> None:
    marks = quote.marks()
    by_mark: dict[str, list[tuple[PriceRow, bool]]] = {}
    control_rows: list[PriceRow] = []
    other_rows: list[PriceRow] = []
    for row in rows:
        identity = parse_item(row.description, row.unit)
        mark, em = boq_fixture_mark(row.description)
        if mark is not None and identity.item_class in ("luminaire", "luminaire_linear", "other", "control") and (
            identity.item_class != "control" or (row.group or "").upper() != "LIGHTING CONTROLS"
        ):
            by_mark.setdefault(mark, []).append((row, em))
        elif identity.item_class == "control" or (row.group or "").upper() == "LIGHTING CONTROLS":
            control_rows.append(row)
        else:
            other_rows.append(row)
    covered: list[str] = []
    not_in_quote: list[str] = []
    blocked: list[str] = []
    em_folded_any = False
    for mark in sorted(by_mark):
        pairs = by_mark[mark]
        line_ids = tuple(r.line_id for r, _em in pairs)
        em_ids = tuple(r.line_id for r, em in pairs if em)
        em_folded_any = em_folded_any or bool(em_ids)
        boq_qty = sum((r.quantity for r, _em in pairs), Decimal(0))
        boq_uoms = {pe.canon_uom(r.unit) if r.unit else "EA" for r, _em in pairs}
        boq_uom = "FT" if boq_uoms == {"FT"} else ("EA" if boq_uoms == {"EA"} else "/".join(sorted(boq_uoms)))
        qline = marks.get(mark)
        if qline is None:
            rec.matches.append(MarkMatch(mark, boq_qty, boq_uom, line_ids, None, None, "not_in_quote", em_ids))
            not_in_quote.extend(line_ids)
            rec.warnings.append(f"not_in_quote:{mark}")
            continue
        if qline.uom != "LS" and boq_uom in ("EA", "FT") and qline.uom != boq_uom:
            for row, _em in pairs:
                rec.findings.append(_uom_finding(row, mark, quote, qline, run_id=run_id, output_version=output_version))
                blocked.append(row.line_id)
            rec.matches.append(MarkMatch(mark, boq_qty, boq_uom, line_ids, qline.qty, qline.uom, "uom_conflict", em_ids))
            continue
        status = "match" if qline.qty == boq_qty else "count_mismatch"
        if status == "count_mismatch":
            rec.warnings.append(f"{FLAG_COUNT_MISMATCH}:{mark} boq {boq_qty.normalize()} quote {qline.qty.normalize()}")
            if FLAG_COUNT_MISMATCH not in rec.flags:
                rec.flags.append(FLAG_COUNT_MISMATCH)
        rec.matches.append(MarkMatch(mark, boq_qty, boq_uom, line_ids, qline.qty, qline.uom, status, em_ids))
        covered.extend(line_ids)
    for mark in sorted(marks):
        if mark not in by_mark:
            rec.not_in_boq_marks = (*rec.not_in_boq_marks, mark)
            rec.warnings.append(f"not_in_boq:{mark}")
    controls_in_quote = quote.notes.controls_included is True or any(ln.kind == "control" for ln in quote.lines)
    if control_rows:
        if controls_in_quote:
            covered.extend(r.line_id for r in control_rows)
            rec.flags.append(FLAG_CONTROLS_COVERED)
        else:
            not_in_quote.extend(r.line_id for r in control_rows)
            rec.flags.append(FLAG_NOT_IN_QUOTE)
            rec.warnings.append("controls_not_in_quote: control rows keep library material")
    for row in other_rows:
        not_in_quote.append(row.line_id)
    if em_folded_any and quote.notes.em_batteries_excluded:
        rec.flags.append(FLAG_EM_PREMIUM)
        rec.warnings.append("em_premium_check: EM fixtures folded into base marks but the quote excludes EM batteries")
    if quote.notes.commissioning == "partial":
        rec.warnings.append("commissioning_partial: the quote covers part of the commissioning")
    rec.covered_line_ids = tuple(dict.fromkeys(covered))
    rec.not_in_quote_line_ids = tuple(dict.fromkeys(not_in_quote))
    rec.blocked_line_ids = tuple(dict.fromkeys(blocked))


def _reconcile_switchgear(quote: ParsedQuote, rows: Sequence[PriceRow], rec: Reconciliation) -> None:
    quote_marks = {ln.mark.replace("-", "").upper() for ln in quote.lines}
    quote_text = " ".join(f"{ln.mark} {ln.text}" for ln in quote.lines).upper().replace("-", "")
    covered: list[str] = []
    unmatched: list[str] = []
    for row in rows:
        desigs = boq_designations(row.description)
        if not desigs:
            covered.append(row.line_id)  # breakers, accessories: the gear quote covers them
            continue
        hit = any(d in quote_marks or re.search(rf"\b{re.escape(d)}\b", quote_text) for d in desigs)
        rec.matches.append(MarkMatch(
            "/".join(desigs), row.quantity, pe.canon_uom(row.unit) if row.unit else "EA", (row.line_id,),
            None, None, "match" if hit else "not_in_quote",
        ))
        (covered if hit else unmatched).append(row.line_id)
    if unmatched:
        rec.flags.append(FLAG_MANUAL_ELIGIBLE)
        rec.warnings.append(f"{FLAG_MANUAL_ELIGIBLE}: {len(unmatched)} designation(s) not in the quote")
    rec.covered_line_ids = tuple(covered)
    rec.not_in_quote_line_ids = tuple(unmatched)


def _reconcile_subcontract(quote: ParsedQuote, rows: Sequence[PriceRow], rec: Reconciliation) -> None:
    quote_text = " ".join(f"{ln.mark} {ln.text}" for ln in quote.lines).upper()
    names_wire = bool(_WIRE_CONDUIT_RE.search(quote_text))
    covered: list[str] = []
    not_in_quote: list[str] = []
    for row in rows:
        identity = parse_item(row.description, row.unit)
        if identity.item_class in ("conductor", "conduit") and not names_wire:
            not_in_quote.append(row.line_id)
            continue
        if rec.mode == "adds":
            not_in_quote.append(row.line_id)
        else:
            covered.append(row.line_id)
    if not_in_quote and rec.mode != "adds":
        rec.warnings.append("wire_conduit_not_in_quote: conduit and wire rows keep General Material pricing")
    rec.covered_line_ids = tuple(covered)
    rec.not_in_quote_line_ids = tuple(not_in_quote)


def reconcile(
    quote: ParsedQuote,
    rows: Sequence[PriceRow],
    *,
    category: str,
    policy: PricingPolicy = DEFAULT_POLICY,
    run_id: str | None = None,
    output_version: int | None = None,
) -> Reconciliation:
    """Map `quote` onto the BOQ rows of `category` (see the module
    docstring). Pure: findings are returned, not recorded."""
    kind = CATEGORY_KINDS.get(category, "subcontract" if category in SUBCONTRACT_CATEGORIES else "general")
    rec = Reconciliation(
        category=category, kind=kind, mode=policy.quote_replace_mode(category), eligible=quote.eligible,
    )
    mine = category_rows(rows, category)
    if kind == "lighting":
        _reconcile_lighting(quote, mine, rec, run_id=run_id, output_version=output_version)
    elif kind == "switchgear":
        _reconcile_switchgear(quote, mine, rec)
    else:
        _reconcile_subcontract(quote, mine, rec)
    if not quote.eligible:
        rec.warnings.append(f"quote_not_eligible:{quote.status}")
    return rec


def record(
    sb: Any, run: Any, reconciliations: Iterable[Reconciliation],
    record_findings: ec.RecordFindings = ec.null_record_findings,
) -> list[ec.GateFinding]:
    """Record every reconciliation finding with stage `pricing` through the
    injected WS4 persistence (the contract; a fake in tests)."""
    findings = [f for rec in reconciliations for f in rec.findings]
    if not findings:
        return []
    return list(record_findings(sb, run, findings, "pricing"))


# Selection ------------------------------------------------------------------------------------------


def quote_ref(quote_id: str, parsed: ParsedQuote, *, category: str, file_sha256: str | None = None) -> QuoteRef:
    """The engine's QuoteRef of a parsed quote. An unanswered tax note
    counts as not included (the figure is never understated)."""
    return QuoteRef(
        quote_id=str(quote_id),
        category=category,
        vendor=parsed.vendor or "",
        amount_usd=parsed.amount_pretax if parsed.amount_pretax is not None else Decimal(0),
        tax_included=parsed.tax_included is True,
        replaces_sections=(),
        file_sha256=file_sha256,
    )


def select_quotes(
    candidates: Sequence[tuple[QuoteRef, ParsedQuote]],
    *,
    human_selected: Iterable[str] = (),
    tax_rate_pct: Decimal = DEFAULT_POLICY.tax_rate_pct,
) -> tuple[list[QuoteRef], dict[str, tuple[QuoteRef, list[QuoteRef]]]]:
    """(the eligible QuoteRefs, chosen per category): the human-selected
    quote when eligible, else the lowest tax-inclusive amount (Q9)."""
    eligible = [ref for ref, parsed in candidates if parsed.eligible]
    chosen = pe.choose_quotes(eligible, selected_quote_ids=human_selected, tax_rate_pct=tax_rate_pct)
    return eligible, chosen


__all__ = [
    "QUOTE_PARSER_VERSION", "GATE_UOM_CONFLICT", "FLAG_EM_PREMIUM", "FLAG_NOT_IN_QUOTE",
    "FLAG_MANUAL_ELIGIBLE", "QuoteLine", "QuoteNotes", "ParsedQuote", "MarkMatch", "Reconciliation",
    "extract_pages", "parse_quote_text", "parse_quote_pdf", "parsed_from_fallback", "classify_notes",
    "llm_quote_fallback", "fold_em_mark", "boq_fixture_mark", "boq_designations", "category_rows",
    "reconcile", "record", "quote_ref", "select_quotes", "sha256_bytes",
]

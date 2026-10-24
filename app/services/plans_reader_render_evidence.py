"""Deliverables drawn from the code-built evidence (docs/AI_ESTIMATING_BUILD_SPEC.md
section 6 WS8, Q29 "both"): the letter-page Marked Plans spec in code mode and
the full-size `Annotated Plans.pdf`.

Two entry points, both code only (no model call, ever):

- `marked_plans_spec_from_evidence(evidence)` and `marked_plans_spec_for_run(sb,
  run)`: the renderer-only marked-plans spec (plans_reader_evidence
  .evidence_to_marked_spec) that `plans_reader_render.render_marked_plans_pdf`
  draws on letter pages when `plans_reader_marked_plans_mode` is `code`. The
  spine passes it as a `spec_overrides` entry at the first render; a modify or
  re-render rebuilds it from the stored evidence through
  `deliverable_spec_from_evidence` (the `deliverable_spec(kind, main, sb, run)`
  extension).
- `render_annotated_plans_pdf(sources, evidence)`: a copy of every source
  sheet that carries evidence, full size, with one `/Circle` annotation per
  count mark, one `/PolyLine` per route polyline (dashed when the route is
  assumed), one `/FreeText` legend per sheet and, when the sheet scale is
  known, a `/VP` viewport with a `/Measure` dictionary so a viewer's
  measuring tool reads feet. Every annotation carries its own appearance
  stream, so PDFium, pypdf and the desktop viewers all draw it.
  `render_annotated_plans(sb, run, evidence=..., version=..., user=None)` is
  the spine seam (`plans_reader_runs.ANNOTATED_PLANS_SEAM`): it renders,
  uploads under `plans-reader/{run}/deliverables/v{n}/` and inserts the
  `plans_reader_deliverables` row of kind `annotated_plans` (migration
  0165). `rerender_from_evidence(sb, run)` renders the next version from the
  stored evidence (the only "re-render" a code kind has). The upload is
  retried once after UPLOAD_RETRY_PAUSE_S on a transport error or a storage
  error answered with a body that is not JSON (seen live on the first WS4
  iteration: storage3 raised json.JSONDecodeError "Expecting value" while
  parsing a gateway error page during a network outage); a failure that
  persists raises AnnotatedPlansStoreError, an app-authored ValueError. The
  annotated plans are an optional deliverable (spec 4.1): the spine never
  fails a run for them, it calls `record_annotated_failure(sb, run_id,
  exc)`, which adds one note to the stored evidence so the Evidence tab says
  why the file is missing; the next successful render removes that note.

`CODE_KINDS = ("annotated_plans",)` are deliverable kinds that exist only as
a product of the evidence: they can be downloaded, previewed and re-rendered
from evidence, never modified by a model or changed from chat
(`routers/ai_estimating_evidence.py` serves them; the legacy deliverable
routes and chat refuse them because they are not in DELIVERABLE_KINDS).

Coordinates: evidence points are fractions of the DISPLAYED page (section
5.1, origin top-left, after /Rotate, inside the CropBox); they go to PDF user
space through plans_reader_render._to_user_space, which honors the page
rotation (a /Rotate 90 page is a required fixture of the tests). Sizes
follow the sent markups: a count circle is CIRCLE_RADIUS_IN on paper, a
polyline POLYLINE_WIDTH_PT wide.

Every string written into the file is cleaned (no em or en dashes, no
control characters) and the legend text is the English chip text, so the
file never carries money, price provenance or model prompt text. Raised
errors are app-authored ValueErrors. Synchronous and I/O bound: call from a
worker or a plain `def` route, never inside `async def`.
"""

from __future__ import annotations

import io
import json
import logging
import math
import time
from collections.abc import Mapping
from typing import Any

from app.services import plans_reader_render as render
from app.services.plans_reader_evidence import (
    METHOD_APPROX,
    METHOD_ASSIGNED,
    METHOD_UNCLAIMED,
    EvidenceOutput,
    evidence_to_marked_spec,
)

logger = logging.getLogger(__name__)

CODE_KINDS: tuple[str, ...] = ("annotated_plans",)
ANNOTATED_KIND = "annotated_plans"
ANNOTATED_FILENAME = "Annotated Plans.pdf"
DELIVERABLE_VERSION_ATTEMPTS = 3
PDF_MIME = "application/pdf"
UPLOAD_ATTEMPTS = 2  # the first try and one retry after a short pause
UPLOAD_RETRY_PAUSE_S = 3.0
FAILURE_NOTE_HEAD = "Annotated plans were not made when the run finished"
MSG_STORE_FAILED = "The annotated plans file could not be saved to the file store; try again in a minute."
MSG_SOURCES_FAILED = "The source drawings could not be read from the file store; try again in a minute."
MSG_RENDER_FAILED = "The file could not be rendered."

CIRCLE_RADIUS_IN = 0.22  # paper inches; a sent-markup dot on a 42 x 30 sheet
CIRCLE_BORDER_PT = 2.0
CIRCLE_FILL_ALPHA = 0.35
POLYLINE_WIDTH_PT = 5.0
DASH_PATTERN = (12.0, 8.0)
LEGEND_FONT_SIZE = 28.0
LEGEND_MAX_ROWS = 40
LEGEND_TITLE = "AI Estimating evidence (code, no model)"
UNCLAIMED_LEGEND = "tag on the plan with no BOQ line"

# Legend colors (RGB 0..1), one per legend row, cycling; the same hues as the
# letter-page marked plans so the two files read alike.
_PALETTE: tuple[tuple[float, float, float], ...] = (
    (0.86, 0.08, 0.08),
    (0.00, 0.45, 0.85),
    (0.05, 0.62, 0.20),
    (0.90, 0.50, 0.00),
    (0.55, 0.10, 0.75),
    (0.00, 0.60, 0.60),
    (0.80, 0.10, 0.50),
    (0.45, 0.35, 0.05),
)
_UNCLAIMED_COLOR = (0.45, 0.45, 0.45)
_ASSIGNED_ALPHA = 0.22  # allocation marks are drawn lighter (their own tone)
_RING_ALPHA = 0.0  # approx marks: ring only
_MEASURE_EPS = 1e-6


class AnnotationCounts(dict):
    """`{"Circle": n, "PolyLine": n, "FreeText": n, "pages": n}`."""


class AnnotatedPlansStoreError(ValueError):
    """The file store failed the annotated plans (the upload after its
    retry, or a source download). The message is app-authored, never the
    storage client's text."""


def _color(index: int | None, method: str | None) -> tuple[float, float, float]:
    if method == METHOD_UNCLAIMED or index is None:
        return _UNCLAIMED_COLOR
    return _PALETTE[int(index) % len(_PALETTE)]


def _num(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _as_evidence(evidence: Mapping[str, Any] | EvidenceOutput) -> dict[str, Any]:
    if isinstance(evidence, EvidenceOutput):
        return evidence.to_dict()
    if not isinstance(evidence, Mapping):
        raise ValueError("The evidence must be an evidence output.")
    return dict(evidence)


# ---------------------------------------------------------------------------
# Marked plans (letter pages) from evidence
# ---------------------------------------------------------------------------


def marked_plans_spec_from_evidence(evidence: Mapping[str, Any] | EvidenceOutput) -> dict[str, Any] | None:
    """The renderer-only marked plans spec, or None when the evidence has no
    marked sheet (nothing to draw). Never validated against the model
    schema and never sent to a model."""
    spec = evidence_to_marked_spec(_as_evidence(evidence))
    sheets = [s for s in (spec.get("sheets") or []) if isinstance(s, dict) and s.get("marks")]
    return {"sheets": sheets} if sheets else None


def _latest_evidence_row(sb: Any, run_id: str) -> dict[str, Any] | None:
    try:
        rows = (
            sb.table("plans_reader_outputs").select("*").eq("run_id", str(run_id)).eq("scope", "evidence").execute().data
            or []
        )
    except Exception:  # noqa: BLE001 - before 0165 the scope does not exist
        logger.warning("render_evidence: evidence rows of run %s not readable", run_id, exc_info=True)
        return None
    return max(rows, key=lambda r: int(r.get("version") or 0), default=None)


def latest_evidence(sb: Any, run_id: str) -> dict[str, Any] | None:
    """The newest stored evidence output of a run (plans_reader_outputs,
    scope `evidence`), or None."""
    best = _latest_evidence_row(sb, run_id)
    out = best.get("output") if best else None
    return dict(out) if isinstance(out, Mapping) else None


def marked_plans_spec_for_run(sb: Any, run: Mapping[str, Any] | str) -> dict[str, Any] | None:
    run_id = run if isinstance(run, str) else str(run.get("id") or "")
    evidence = latest_evidence(sb, run_id)
    return marked_plans_spec_from_evidence(evidence) if evidence else None


def deliverable_spec_from_evidence(kind: str, sb: Any, run: Mapping[str, Any] | str | None) -> dict[str, Any] | None:
    """The `deliverable_spec(kind, main, sb, run)` extension (spec 6 WS8):
    in code mode the marked plans spec comes from the stored evidence; any
    other kind, a missing client or model mode answers None so the caller
    falls back to the main output's part."""
    if kind != "marked_plans" or sb is None or run is None:
        return None
    try:
        from app.core.config import get_settings

        if getattr(get_settings(), "plans_reader_marked_plans_mode", "model") != "code":
            return None
    except Exception:  # noqa: BLE001 - settings unavailable reads as model mode
        return None
    try:
        return marked_plans_spec_for_run(sb, run)
    except Exception:  # noqa: BLE001 - the model's spec renders instead
        logger.exception("render_evidence: marked plans spec from evidence failed")
        return None


# ---------------------------------------------------------------------------
# Annotated Plans.pdf
# ---------------------------------------------------------------------------


def _sheet_entries(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Sheets with at least one drawable mark, in evidence order."""
    out = []
    for s in evidence.get("sheets") or []:
        if not isinstance(s, Mapping):
            continue
        marks = [m for m in (s.get("marks") or []) if isinstance(m, Mapping) and m.get("points")]
        if marks and s.get("document_id") and _num(s.get("page")):
            out.append({**dict(s), "marks": marks})
    return out


def expected_counts(evidence: Mapping[str, Any] | EvidenceOutput) -> AnnotationCounts:
    """What the annotated file must contain: one /Circle per dot mark, one
    /PolyLine per polyline with two or more points, one /FreeText legend per
    sheet, one page per sheet with marks."""
    data = _as_evidence(evidence)
    counts = AnnotationCounts(Circle=0, PolyLine=0, FreeText=0, pages=0)
    for s in _sheet_entries(data):
        counts["pages"] += 1
        counts["FreeText"] += 1
        for m in s["marks"]:
            pts = [p for p in m.get("points") or [] if isinstance(p, (list, tuple)) and len(p) >= 2]
            if m.get("kind") == "polyline":
                if len(pts) >= 2:
                    counts["PolyLine"] += 1
            elif pts:
                counts["Circle"] += 1
    return counts


def count_annotations(pdf_bytes: bytes) -> AnnotationCounts:
    """Annotations by subtype in a PDF (pypdf), for the tests and the
    render check."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    counts = AnnotationCounts(Circle=0, PolyLine=0, FreeText=0, pages=len(reader.pages))
    for page in reader.pages:
        for ref in page.get("/Annots") or []:
            annot = ref.get_object()
            sub = str(annot.get("/Subtype") or "").lstrip("/")
            if sub in counts:
                counts[sub] += 1
    return counts


def count_annotations_pdfium(pdf_bytes: bytes) -> int:
    """Total annotations PDFium sees (FPDFPage_GetAnnotCount over every page);
    -1 when the raw binding is not available."""
    try:
        import pypdfium2 as pdfium
        import pypdfium2.raw as pdfium_c
    except Exception:  # noqa: BLE001 - optional check
        return -1
    doc = pdfium.PdfDocument(pdf_bytes)
    total = 0
    try:
        for i in range(len(doc)):
            page = doc[i]
            total += int(pdfium_c.FPDFPage_GetAnnotCount(page.raw))
            page.close()
    finally:
        doc.close()
    return total


def _ellipse_ops(cx: float, cy: float, r: float) -> str:
    """A closed circle path (four Bezier arcs) around (cx, cy)."""
    k = 0.5523 * r
    return (
        f"{cx + r:.3f} {cy:.3f} m "
        f"{cx + r:.3f} {cy + k:.3f} {cx + k:.3f} {cy + r:.3f} {cx:.3f} {cy + r:.3f} c "
        f"{cx - k:.3f} {cy + r:.3f} {cx - r:.3f} {cy + k:.3f} {cx - r:.3f} {cy:.3f} c "
        f"{cx - r:.3f} {cy - k:.3f} {cx - k:.3f} {cy - r:.3f} {cx:.3f} {cy - r:.3f} c "
        f"{cx + k:.3f} {cy - r:.3f} {cx + r:.3f} {cy - k:.3f} {cx + r:.3f} {cy:.3f} c h"
    )


def _circle_annotation(writer, center: tuple[float, float], radius: float, color, *, fill_alpha: float,
                       dashed: bool, contents: str):
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        FloatObject,
        NameObject,
        NumberObject,
        TextStringObject,
    )

    cx, cy = center
    pad = CIRCLE_BORDER_PT
    rect = [cx - radius - pad, cy - radius - pad, cx + radius + pad, cy + radius + pad]
    gs_name = "/GSa" if fill_alpha > 0 else None
    ops = [f"q {render._rgb(color)} RG {CIRCLE_BORDER_PT:g} w"]
    if dashed:
        ops.append(f"[{DASH_PATTERN[0]:g} {DASH_PATTERN[1]:g}] 0 d")
    if gs_name:
        ops.append(f"{gs_name} gs {render._rgb(color)} rg {_ellipse_ops(cx, cy, radius)} B Q")
    else:
        ops.append(f"{_ellipse_ops(cx, cy, radius)} S Q")
    ap = DecodedStreamObject()
    ap.set_data(" ".join(ops).encode("latin-1"))
    resources = DictionaryObject()
    if gs_name:
        resources[NameObject("/ExtGState")] = DictionaryObject(
            {
                NameObject("/GSa"): DictionaryObject(
                    {NameObject("/Type"): NameObject("/ExtGState"), NameObject("/ca"): FloatObject(fill_alpha),
                     NameObject("/CA"): FloatObject(1.0)}
                )
            }
        )
    ap.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject([FloatObject(v) for v in rect]),
            NameObject("/Resources"): resources,
        }
    )
    bs = DictionaryObject({NameObject("/Type"): NameObject("/Border"), NameObject("/W"): FloatObject(CIRCLE_BORDER_PT)})
    if dashed:
        bs[NameObject("/S")] = NameObject("/D")
        bs[NameObject("/D")] = ArrayObject([FloatObject(v) for v in DASH_PATTERN])
    annot = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Circle"),
            NameObject("/Rect"): ArrayObject([FloatObject(v) for v in rect]),
            NameObject("/C"): ArrayObject([FloatObject(v) for v in color]),
            NameObject("/BS"): bs,
            NameObject("/Border"): ArrayObject([NumberObject(0), NumberObject(0), FloatObject(CIRCLE_BORDER_PT)]),
            NameObject("/F"): NumberObject(4),
            NameObject("/CA"): FloatObject(1.0),
            NameObject("/Contents"): TextStringObject(contents),
            NameObject("/AP"): DictionaryObject({NameObject("/N"): render._indirect(writer, ap)}),
        }
    )
    if fill_alpha > 0:
        annot[NameObject("/IC")] = ArrayObject([FloatObject(v) for v in color])
    return annot


def _polyline_annotation(writer, points: list[tuple[float, float]], color, *, dashed: bool, contents: str):
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        FloatObject,
        NameObject,
        NumberObject,
        TextStringObject,
    )

    pad = POLYLINE_WIDTH_PT / 2 + 1
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    rect = [min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad]
    ops = [f"q {render._rgb(color)} RG {POLYLINE_WIDTH_PT:g} w 1 J 1 j"]
    if dashed:
        ops.append(f"[{DASH_PATTERN[0]:g} {DASH_PATTERN[1]:g}] 0 d")
    ops.append(f"{points[0][0]:.3f} {points[0][1]:.3f} m")
    for x, y in points[1:]:
        ops.append(f"{x:.3f} {y:.3f} l")
    ops.append("S Q")
    ap = DecodedStreamObject()
    ap.set_data(" ".join(ops).encode("latin-1"))
    ap.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject([FloatObject(v) for v in rect]),
        }
    )
    bs = DictionaryObject({NameObject("/Type"): NameObject("/Border"), NameObject("/W"): FloatObject(POLYLINE_WIDTH_PT)})
    if dashed:
        bs[NameObject("/S")] = NameObject("/D")
        bs[NameObject("/D")] = ArrayObject([FloatObject(v) for v in DASH_PATTERN])
    else:
        bs[NameObject("/S")] = NameObject("/S")
    return DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/PolyLine"),
            NameObject("/Rect"): ArrayObject([FloatObject(v) for v in rect]),
            NameObject("/Vertices"): ArrayObject([FloatObject(v) for p in points for v in p]),
            NameObject("/C"): ArrayObject([FloatObject(v) for v in color]),
            NameObject("/BS"): bs,
            NameObject("/Border"): ArrayObject([NumberObject(0), NumberObject(0), FloatObject(POLYLINE_WIDTH_PT)]),
            NameObject("/LE"): ArrayObject([NameObject("/None"), NameObject("/None")]),
            NameObject("/F"): NumberObject(4),
            NameObject("/CA"): FloatObject(1.0),
            NameObject("/Contents"): TextStringObject(contents),
            NameObject("/AP"): DictionaryObject({NameObject("/N"): render._indirect(writer, ap)}),
        }
    )


def _measure_viewport(box: tuple[float, float, float, float], ft_per_in: float, sheet_number: str):
    """A /VP entry whose /Measure (rectilinear) maps 72 pt to ft_per_in
    feet, so a viewer's distance tool reads feet at the sheet scale."""
    from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, TextStringObject

    def fmt(unit: str, factor: float):
        return ArrayObject(
            [
                DictionaryObject(
                    {
                        NameObject("/Type"): NameObject("/NumberFormat"),
                        NameObject("/U"): TextStringObject(unit),
                        NameObject("/C"): FloatObject(factor),
                        NameObject("/D"): FloatObject(100),
                    }
                )
            ]
        )

    feet_per_pt = ft_per_in / 72.0
    measure = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Measure"),
            NameObject("/Subtype"): NameObject("/RL"),
            NameObject("/R"): TextStringObject(render._clean(f'1" = {ft_per_in:g}\'')),
            NameObject("/X"): fmt("ft", feet_per_pt),
            NameObject("/Y"): fmt("ft", feet_per_pt),
            NameObject("/D"): fmt("ft", 1.0),
            NameObject("/A"): fmt("sq ft", 1.0),
        }
    )
    return DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Viewport"),
            NameObject("/BBox"): ArrayObject([FloatObject(v) for v in box]),
            NameObject("/Name"): TextStringObject(render._clean(f"Sheet {sheet_number} scale")),
            NameObject("/Measure"): measure,
        }
    )


def _legend_lines(sheet: Mapping[str, Any], chips: Mapping[str, str]) -> list[str]:
    """One legend line per legend row: "[i] prefix description: qty unit,
    marked m (chip text)". Index i is also each annotation's /Contents, so
    a viewer's annotation list reads against the legend."""
    lines: list[str] = []
    for i, row in enumerate(sheet.get("legend") or []):
        if not isinstance(row, Mapping):
            continue
        lid = row.get("line_id")
        prefix = render._clean(row.get("prefix"))
        desc = render._clean(row.get("description"))
        head = f"{prefix}, {desc}" if prefix and desc and not desc.startswith(prefix) else (desc or prefix)
        qty = row.get("quantity")
        unit = render._clean(row.get("unit") or "EA")
        q = f"{float(qty):g}" if _num(qty) is not None else "?"
        marked = int(row.get("marked") or 0)
        extra = chips.get(str(lid)) if lid else UNCLAIMED_LEGEND
        parts = [f"[{i + 1}] {head}: {q} {unit}, marked {marked}"]
        nl = int(row.get("not_located") or 0)
        if nl:
            parts.append(f"{nl} not located")
        if extra:
            parts.append(render._clean(extra))
        lines.append(", ".join(parts))
    if len(lines) > LEGEND_MAX_ROWS:
        rest = len(lines) - LEGEND_MAX_ROWS
        lines = lines[:LEGEND_MAX_ROWS] + [f"... {rest} more rows in the evidence tab"]
    return lines


def _mark_style(mark: Mapping[str, Any]) -> tuple[float, bool]:
    """(fill alpha, dashed) for a dot mark by its placement method."""
    method = str(mark.get("method") or "")
    if method == METHOD_UNCLAIMED:
        return 0.0, True
    if method == METHOD_APPROX:
        return _RING_ALPHA, False
    if method == METHOD_ASSIGNED:
        return _ASSIGNED_ALPHA, False
    return CIRCLE_FILL_ALPHA, False


def render_annotated_plans_pdf(
    sources: Mapping[str, bytes],
    evidence: Mapping[str, Any] | EvidenceOutput,
    *,
    include_measure: bool = True,
    max_bytes: int | None = None,
) -> bytes:
    """`Annotated Plans.pdf` (module docstring). `sources` maps a document
    id to that document's PDF bytes (the splitter segment the run reads,
    pages 1-based inside it); sheets whose document is missing from
    `sources` are left out with a warning, and a file with no sheet left
    raises ValueError. `max_bytes` (optional) raises when the result is
    larger, so a caller can refuse an oversized file instead of storing it."""
    from pypdf import PdfReader, PdfWriter

    data = _as_evidence(evidence)
    chips = {
        str(ln.get("line_id")): str(((ln.get("qty_chip") or {}).get("text")) or "")
        for ln in data.get("lines") or []
        if isinstance(ln, Mapping)
    }
    sheets = _sheet_entries(data)
    if not sheets:
        raise ValueError("The evidence has no marked sheet to annotate.")
    readers: dict[str, Any] = {}
    plans: list[dict[str, Any]] = []
    skipped = 0
    for s in sheets:
        did = str(s.get("document_id"))
        page_no = int(_num(s.get("page")) or 0)
        if did not in sources or page_no < 1:
            skipped += 1
            continue
        if did not in readers:
            try:
                readers[did] = PdfReader(io.BytesIO(sources[did]))
            except Exception:
                raise ValueError("A source drawing could not be read as a PDF.") from None
        if page_no > len(readers[did].pages):
            skipped += 1
            continue
        plans.append({"sheet": s, "doc": did, "page": page_no})
    if not plans:
        raise ValueError("None of the evidence sheets could be found in this run's documents.")
    if skipped:
        logger.warning("render_evidence: %s evidence sheet(s) skipped (document or page missing)", skipped)

    try:
        writer = PdfWriter()
        font_ref = None
        out_index: dict[tuple[str, int], int] = {}
        for plan in plans:
            key = (plan["doc"], plan["page"])
            if key not in out_index:
                writer.add_page(readers[plan["doc"]].pages[plan["page"] - 1])
                out_index[key] = len(writer.pages) - 1
        for plan in plans:
            s = plan["sheet"]
            idx = out_index[(plan["doc"], plan["page"])]
            page = writer.pages[idx]
            rotation = int(page.rotation or 0) % 360
            cb = page.cropbox
            box = (float(cb.left), float(cb.bottom), float(cb.right), float(cb.top))
            radius = CIRCLE_RADIUS_IN * 72.0  # points per paper inch
            legend_rows = list(s.get("legend") or [])
            for m in s["marks"]:
                pts_f = [(render._fraction(p[0]), render._fraction(p[1])) for p in m.get("points") or []
                         if isinstance(p, (list, tuple)) and len(p) >= 2]
                if not pts_f:
                    continue
                li = m.get("legend_index")
                li = int(li) if isinstance(li, int) and 0 <= li < len(legend_rows) else None
                color = _color(li, m.get("method"))
                label = f"[{li + 1}]" if li is not None else "[NC]"
                pts = [render._to_user_space(fx, fy, box, rotation) for fx, fy in pts_f]
                if m.get("kind") == "polyline":
                    if len(pts) < 2:
                        continue
                    dashed = str(m.get("style") or "") == "dashed"
                    text = label
                    if m.get("label"):
                        text = f"{label} {render._clean(m.get('label'))}"
                    writer.add_annotation(idx, _polyline_annotation(writer, pts, color, dashed=dashed, contents=text))
                else:
                    alpha, dashed = _mark_style(m)
                    count = int(m.get("count") or 1)
                    text = f"{label} x{count}" if count > 1 else label
                    writer.add_annotation(
                        idx, _circle_annotation(writer, pts[0], radius, color, fill_alpha=alpha, dashed=dashed, contents=text)
                    )
            # Legend: one FreeText at the top-left of the displayed page.
            lines = _legend_lines(s, chips)
            if font_ref is None:
                font_ref = render._markup_font(writer)
            anchor = render._to_user_space(0.015, 0.03, box, rotation)
            title = render._clean(f"{LEGEND_TITLE}, sheet {s.get('sheet_number') or plan['page']}")
            writer.add_annotation(
                idx,
                render._markup_label_annotation(
                    writer, font_ref, anchor, box, rotation, title, "\n".join(lines), LEGEND_FONT_SIZE
                ),
            )
            # Measure: a viewport with the sheet scale when the evidence knows it.
            ft_per_in = _num((s.get("scale") or {}).get("ft_per_in")) if isinstance(s.get("scale"), Mapping) else None
            if include_measure and ft_per_in and ft_per_in > _MEASURE_EPS and "/VP" not in page:
                from pypdf.generic import ArrayObject, NameObject

                page[NameObject("/VP")] = ArrayObject(
                    [render._indirect(writer, _measure_viewport(box, ft_per_in, str(s.get("sheet_number") or "")))]
                )
        buf = io.BytesIO()
        writer.write(buf)
    except ValueError:
        raise
    except Exception:
        logger.warning("render_evidence: the source drawing could not be processed", exc_info=True)
        raise ValueError("The source drawing could not be processed.") from None
    out = buf.getvalue()
    if max_bytes is not None and len(out) > max_bytes:
        raise ValueError(f"The annotated plans file is too large ({len(out)} bytes, limit {max_bytes}).")
    return out


# ---------------------------------------------------------------------------
# Storage: the deliverable row of kind annotated_plans
# ---------------------------------------------------------------------------


def _documents(sb: Any, run_id: str) -> dict[str, dict[str, Any]]:
    rows = sb.table("plans_reader_documents").select("*").eq("run_id", str(run_id)).execute().data or []
    return {str(d.get("id")): dict(d) for d in rows if isinstance(d, Mapping) and d.get("id")}


def _source_bytes(sb: Any, run_id: str, evidence: Mapping[str, Any], download: Any = None) -> dict[str, bytes]:
    """The PDF bytes of every document the evidence sheets reference."""
    from app.services import storage

    get = download or storage.download_file
    docs = _documents(sb, run_id)
    out: dict[str, bytes] = {}
    for s in _sheet_entries(evidence):
        did = str(s.get("document_id"))
        if did in out or did not in docs or not docs[did].get("storage_path"):
            continue
        try:
            out[did] = get(str(docs[did]["storage_path"]))
        except Exception as exc:  # noqa: BLE001 - storage.download_file retries transport errors itself
            logger.warning("render_evidence: source %s of run %s not downloadable", did, run_id, exc_info=True)
            raise AnnotatedPlansStoreError(MSG_SOURCES_FAILED) from exc
    return out


def _is_retryable_store_error(exc: BaseException) -> bool:
    """A dropped connection, or a storage error whose body is not the
    storage API's JSON (a gateway or proxy page: storage3 raises
    json.JSONDecodeError while parsing it, or StorageApiError "Unable to
    parse error message"), or a 5xx / 429 answer."""
    import httpx

    if isinstance(exc, (httpx.TransportError, httpx.HTTPStatusError, json.JSONDecodeError)):
        return True
    try:
        from storage3.exceptions import StorageApiError
    except Exception:  # noqa: BLE001 - storage3 always ships with the backend
        return False
    if isinstance(exc, StorageApiError):
        status = str(getattr(exc, "status", "") or "")
        message = str(getattr(exc, "message", "") or "")
        return status.startswith("5") or status == "429" or message.startswith("Unable to parse error message")
    return False


def _store_pdf(put: Any, path_for: Any, pdf: bytes, sleep: Any) -> str:
    """Upload `pdf` at a freshly minted path; on a retryable storage error
    wait UPLOAD_RETRY_PAUSE_S and retry once at a new path (a fresh uuid, so
    an object the failed try may have landed never answers 409). Returns the
    stored path; raises AnnotatedPlansStoreError once the retry fails too,
    or at once on any other storage error."""
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        path = path_for()
        try:
            put(path, pdf, PDF_MIME)
            return path
        except Exception as exc:  # noqa: BLE001 - classified below, never leaked as is
            if not _is_retryable_store_error(exc) or attempt == UPLOAD_ATTEMPTS:
                logger.warning(
                    "render_evidence: annotated plans upload (%d bytes) failed on attempt %d/%d: %s",
                    len(pdf), attempt, UPLOAD_ATTEMPTS, type(exc).__name__, exc_info=True,
                )
                raise AnnotatedPlansStoreError(MSG_STORE_FAILED) from exc
            logger.warning(
                "render_evidence: annotated plans upload (%d bytes) failed on attempt %d/%d (%s); retrying",
                len(pdf), attempt, UPLOAD_ATTEMPTS, type(exc).__name__,
            )
            sleep(UPLOAD_RETRY_PAUSE_S)
    raise AssertionError("unreachable")


def failure_note(exc: BaseException) -> str:
    """The evidence note for a failed end of run render: the app-authored
    reason (a storage failure, or the renderer's own ValueError such as "no
    marked sheet") and never a library's text (json.JSONDecodeError is a
    ValueError too)."""
    if isinstance(exc, AnnotatedPlansStoreError):
        reason = str(exc)
    elif type(exc) is ValueError and str(exc):
        reason = str(exc)
    else:
        reason = MSG_RENDER_FAILED
    reason = render._clean(reason)
    return f"{FAILURE_NOTE_HEAD}: {reason} Re-render makes a new copy."


def _is_failure_note(note: Any) -> bool:
    return isinstance(note, str) and note.startswith(FAILURE_NOTE_HEAD)


def _set_failure_note(sb: Any, run_id: str, note: str | None) -> bool:
    """Replace the failure note on the newest stored evidence row (None
    removes it). True when the row changed. Never raises."""
    try:
        row = _latest_evidence_row(sb, run_id)
        out = row.get("output") if row else None
        if not row or not row.get("id") or not isinstance(out, Mapping):
            return False
        notes = [n for n in (out.get("notes") or []) if not _is_failure_note(n)]
        if note:
            notes.append(note)
        if notes == list(out.get("notes") or []):
            return False
        sb.table("plans_reader_outputs").update({"output": {**dict(out), "notes": notes}}).eq("id", row["id"]).execute()
        return True
    except Exception:  # noqa: BLE001 - a note is a display hint
        logger.warning("render_evidence: evidence note of run %s not written", run_id, exc_info=True)
        return False


def record_annotated_failure(sb: Any, run_id: str, exc: BaseException) -> bool:
    """The spine's record of a failed end of run render (spec 4.1: the
    annotated plans are optional, so the run never fails for them): one
    note on the run's stored evidence (failure_note), shown on the Evidence
    tab next to the Re-render button. Never raises."""
    return _set_failure_note(sb, str(run_id), failure_note(exc))


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc)
    return getattr(exc, "code", None) == "23505" or "23505" in text or "duplicate key" in text


def _uid(user: Any) -> str | None:
    if user is None:
        return None
    if isinstance(user, Mapping):
        value = user.get("id")
    else:
        value = getattr(user, "id", None)
    return str(value) if value else None


def latest_annotated(sb: Any, run_id: str) -> dict[str, Any] | None:
    rows = (
        sb.table("plans_reader_deliverables").select("*").eq("run_id", str(run_id)).eq("kind", ANNOTATED_KIND).execute().data
        or []
    )
    return max(rows, key=lambda r: int(r.get("version") or 0), default=None)


def render_annotated_plans(
    sb: Any,
    run: Mapping[str, Any],
    *,
    evidence: Mapping[str, Any] | EvidenceOutput,
    version: int = 1,
    user: Any = None,
    upload: Any = None,
    download: Any = None,
    max_bytes: int | None = None,
    sleep: Any = None,
) -> dict[str, Any]:
    """Render the annotated plans of `run` from `evidence`, store the file
    and insert its deliverables row (kind annotated_plans). A version taken
    by a concurrent render moves to the next number. `upload`, `download`
    and `sleep` replace the storage calls and the retry pause in tests.
    Raises ValueError when there is nothing to draw or the sources are
    unreadable, AnnotatedPlansStoreError (a ValueError) when the file store
    fails after the one retry. A success removes the failure note an
    earlier end of run render left on the evidence."""
    from app.services import storage

    run_id = str(run.get("id") or "")
    if not run_id:
        raise ValueError("The run has no id.")
    data = _as_evidence(evidence)
    sources = _source_bytes(sb, run_id, data, download=download)
    pdf = render_annotated_plans_pdf(sources, data, max_bytes=max_bytes)
    spec = {
        "schema": "bdr.annotated_plans/1",
        "evidence_inputs_hash": data.get("inputs_hash"),
        "main_version": data.get("main_version"),
        "boq_version": data.get("boq_version"),
        "counts": dict(expected_counts(data)),
    }
    put = upload or storage.upload_file
    pause = sleep or time.sleep
    v = max(1, int(version))
    for _attempt in range(DELIVERABLE_VERSION_ATTEMPTS):
        folder = f"deliverables/v{v}"
        path = _store_pdf(put, lambda: storage.build_plans_reader_path(run_id, folder, ANNOTATED_FILENAME), pdf, pause)
        row = {
            "run_id": run_id,
            "kind": ANNOTATED_KIND,
            "version": v,
            "filename": ANNOTATED_FILENAME,
            "storage_path": path,
            "size_bytes": len(pdf),
            "spec_json": spec,
            "instruction": None,
            "created_by": _uid(user),
        }
        try:
            inserted = sb.table("plans_reader_deliverables").insert(row).execute().data[0]
        except Exception as exc:
            if not _is_unique_violation(exc):
                raise
            v += 1
            continue
        if any(_is_failure_note(n) for n in data.get("notes") or []):
            _set_failure_note(sb, run_id, None)
        return dict(inserted)
    raise ValueError("Another version of the annotated plans was being saved; try again.")


def rerender_from_evidence(sb: Any, run: Mapping[str, Any], *, user: Any = None, **kw: Any) -> dict[str, Any]:
    """The next annotated plans version from the stored evidence (the only
    re-render a code kind has: no model, no modify). LookupError when the
    run has no evidence yet."""
    run_id = str(run.get("id") or "")
    evidence = latest_evidence(sb, run_id)
    if evidence is None:
        raise LookupError("This run has no evidence to draw from yet.")
    latest = latest_annotated(sb, run_id)
    version = (int(latest.get("version") or 0) if latest else 0) + 1
    return render_annotated_plans(sb, run, evidence=evidence, version=version, user=user, **kw)


__all__ = [
    "ANNOTATED_FILENAME",
    "ANNOTATED_KIND",
    "AnnotatedPlansStoreError",
    "AnnotationCounts",
    "CODE_KINDS",
    "count_annotations",
    "count_annotations_pdfium",
    "deliverable_spec_from_evidence",
    "expected_counts",
    "failure_note",
    "latest_annotated",
    "latest_evidence",
    "marked_plans_spec_for_run",
    "marked_plans_spec_from_evidence",
    "record_annotated_failure",
    "render_annotated_plans",
    "render_annotated_plans_pdf",
    "rerender_from_evidence",
]

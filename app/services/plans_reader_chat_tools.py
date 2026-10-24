"""WS8 chat tools (docs/AI_ESTIMATING_BUILD_SPEC.md 5.2 `explain_row`, 5.5
chat visibility, section 6 WS8 "chat tools"): `explain_row` and the
`humanize` of D and P references.

`explain_row(line, include_marks)` answers "where does this BOQ number come
from" for the reviewer (kind RV) and chat (kind C): the quantity chip and a
derivation sentence, the cited source rows (C, R, S, M ids resolved through
the WS4 RowStore when it is registered), marks by placement method, drawn
polyline feet and the length check, the sheets, the open gate findings and
the review findings on the line, and, ONLY for a pricing actor (the tool
context's role passes `features.ai_pricing_allowed` and
AI_PRICING_CHAT_TOOLS_ENABLED is on), a `price` block made of the price
chip and the priced line's unit values and extended amounts. The tool is
not `internal_pricing` (everyone may ask how a quantity was made); when the
price block is served the handler marks the ToolContext `pricing_served`,
which the chat loop (plans_reader_chat.serve_tool) turns into the message's
`pricing_restricted` tag so readers outside the pricing roles see the
answer redacted (5.5).

`humanize(text, rows_by_id)` rewrites bare D and P row references ("D3",
"P12") into "detail D3 (E5.01: <description>)" and "spec P12 (<section>:
<title>)" when the row is known, and "detail D3" / "spec item P12" when it
is not, so a chat answer never prints a bare id the estimator cannot
follow. Other ids (C, R, S, M, F, B) are left as they are: the tools print
them with their rows.

Reads go through an `ExplainSources` provider (Supabase, or a fake set
with `set_sources_factory` in tests): the stored evidence output, the
priced lines of the latest pricing version, the gate findings and the
review findings. No model call, no network of its own, no provider SDK.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from app.services import plans_reader_tools as tools
from app.services.electrical_items import BoqRow, similarity
from app.services.plans_reader_evidence import price_chip
from app.services.plans_reader_tools import ToolContext, ToolResult, ToolSpec, kinds_from_codes, register

logger = logging.getLogger(__name__)

EXPLAIN_ROW = "explain_row"
MAX_MARKS = 200
MAX_FINDINGS = 20
MATCH_MIN = 0.55
_REF_RE = re.compile(r"\b([DP])([1-9][0-9]*)\b")
_LINE_ID_RE = re.compile(r"^B[1-9][0-9]*$")

EXPLAIN_ROW_ARGS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["line", "include_marks"],
    "properties": {
        "line": {"type": "string", "minLength": 1, "maxLength": 200},
        "include_marks": {"type": "boolean"},
    },
}
EXPLAIN_ROW_RESULT_DOC: dict[str, Any] = {
    "line_id": "B<n>",
    "description": "str",
    "quantity": "number",
    "unit": "EA or FT",
    "derivation": "one sentence: how the quantity was made",
    "basis": {"kind": "chip kind", "tone": "ok|warn|danger|neutral", "text": "chip text"},
    "sources": [{"ref": "C4", "multiplier": 1, "row": "compact row or null", "coverage": "sheet_total|..."}],
    "marks_by_method": {"anchor": "int", "assigned": "int", "snapped": "int", "registered": "int", "approx": "int"},
    "not_located": "int or null",
    "polylines": "int or null",
    "polyline_ft": "number or null",
    "length_check": "dict or null",
    "sheets": ["sheet keys"],
    "marks": [{"sheet_key": "str", "points": "[[x,y]]", "method": "str", "kind": "dot|polyline"}],
    "gate_findings": [{"gate_id": "str", "severity": "str", "status": "str", "message": "str"}],
    "review_findings": [{"kind": "str", "severity": "str", "title": "str"}],
    "price": "pricing actors only: {chip, labor_hours, material_cost, unit_labor_hours, unit_material_usd, source}",
}
EXPLAIN_ROW_DESCRIPTION = (
    "How one BOQ line's quantity was made: its basis chip, the cited rows (C, R, S, M ids) with coverage, "
    "the marks on the plans by method, the drawn polyline feet, the sheets, and the open gate and review "
    "findings on it. `line` is a line id such as B12 or the line's description text. Pass include_marks "
    "true to get every mark's position (page fractions). The price block is present only when the asker "
    "may see prices."
)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class ExplainSources(Protocol):
    def evidence(self, run_id: str) -> Mapping[str, Any] | None: ...

    def priced_line(self, run_id: str, line_id: str) -> Mapping[str, Any] | None: ...

    def gate_findings(self, run_id: str) -> list[Mapping[str, Any]]: ...

    def review_findings(self, run_id: str) -> list[Mapping[str, Any]]: ...


class FakeExplainSources:
    """Canned sources for tests."""

    def __init__(
        self,
        *,
        evidence: Mapping[str, Any] | None = None,
        priced: Mapping[str, Mapping[str, Any]] | None = None,
        gate_findings: list[Mapping[str, Any]] | None = None,
        review_findings: list[Mapping[str, Any]] | None = None,
    ) -> None:
        self._evidence = evidence
        self._priced = dict(priced or {})
        self._gates = list(gate_findings or [])
        self._reviews = list(review_findings or [])

    def evidence(self, run_id: str) -> Mapping[str, Any] | None:
        return self._evidence

    def priced_line(self, run_id: str, line_id: str) -> Mapping[str, Any] | None:
        return self._priced.get(line_id)

    def gate_findings(self, run_id: str) -> list[Mapping[str, Any]]:
        return list(self._gates)

    def review_findings(self, run_id: str) -> list[Mapping[str, Any]]:
        return list(self._reviews)


class SupabaseExplainSources:
    """Reads through the sync client of the chat job (worker thread)."""

    def __init__(self, sb: Any) -> None:
        self.sb = sb

    def evidence(self, run_id: str) -> Mapping[str, Any] | None:
        from app.services.plans_reader_render_evidence import latest_evidence

        return latest_evidence(self.sb, run_id)

    def priced_line(self, run_id: str, line_id: str) -> Mapping[str, Any] | None:
        try:
            from app.services import pricing_run

            latest = pricing_run.latest_version(self.sb, run_id)
            if not latest:
                return None
            rows = (
                self.sb.table("plans_reader_priced_lines").select("*").eq("pricing_id", str(latest["id"]))
                .eq("line_id", line_id).limit(1).execute().data or []
            )
            line = rows[0].get("line") if rows else None
            return dict(line) if isinstance(line, Mapping) else None
        except Exception:  # noqa: BLE001 - before ME, or no pricing yet
            logger.info("explain_row: priced line %s of run %s unavailable", line_id, run_id, exc_info=True)
            return None

    def gate_findings(self, run_id: str) -> list[Mapping[str, Any]]:
        try:
            rows = self.sb.table("plans_reader_gate_findings").select("*").eq("run_id", str(run_id)).execute().data or []
        except Exception:  # noqa: BLE001 - before MC
            return []
        return [r for r in rows if isinstance(r, Mapping)]

    def review_findings(self, run_id: str) -> list[Mapping[str, Any]]:
        try:
            rows = self.sb.table("plans_reader_review_findings").select("*").eq("run_id", str(run_id)).execute().data or []
        except Exception:  # noqa: BLE001 - before MF
            return []
        return [r for r in rows if isinstance(r, Mapping)]


class EmptyExplainSources(FakeExplainSources):
    """No client, no canned data (replay without evidence)."""


_SOURCES_FACTORY: Callable[[ToolContext], ExplainSources] | None = None


def set_sources_factory(factory: Callable[[ToolContext], ExplainSources] | None) -> None:
    global _SOURCES_FACTORY
    _SOURCES_FACTORY = factory


def sources_for(ctx: ToolContext) -> ExplainSources:
    if _SOURCES_FACTORY is not None:
        return _SOURCES_FACTORY(ctx)
    if ctx.sb is not None:
        return SupabaseExplainSources(ctx.sb)
    return EmptyExplainSources()


# ---------------------------------------------------------------------------
# humanize
# ---------------------------------------------------------------------------


def _row_label(row: Mapping[str, Any]) -> str:
    sheet = row.get("sheet") if isinstance(row.get("sheet"), Mapping) else {}
    number = sheet.get("sheet_number") or row.get("sheet_number") or ""
    desc = str(row.get("description") or row.get("title") or row.get("text") or "").strip()
    section = str(row.get("section") or row.get("spec_section") or "").strip()
    head = section or number
    body = desc[:80]
    if head and body:
        return f"{head}: {body}"
    return head or body


def humanize(text: str, rows_by_id: Mapping[str, Mapping[str, Any]] | None = None) -> str:
    """Bare D and P references become readable (module docstring)."""
    rows = rows_by_id or {}

    def repl(m: re.Match[str]) -> str:
        rid = m.group(0)
        noun = "detail" if m.group(1) == "D" else "spec item"
        row = rows.get(rid)
        label = _row_label(row) if isinstance(row, Mapping) else ""
        return f"{noun} {rid} ({label})" if label else f"{noun} {rid}"

    return _REF_RE.sub(repl, text or "")


# ---------------------------------------------------------------------------
# explain_row
# ---------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _boq_row(description: str, unit: str) -> BoqRow:
    return BoqRow("", "", "", "", description, 0.0, unit)


def find_line(evidence: Mapping[str, Any], query: str) -> Mapping[str, Any] | None:
    """The evidence line for an id (B12) or the best description match."""
    lines = [ln for ln in evidence.get("lines") or [] if isinstance(ln, Mapping)]
    q = (query or "").strip()
    if _LINE_ID_RE.match(q.upper()):
        for ln in lines:
            if str(ln.get("line_id") or "").upper() == q.upper():
                return ln
        return None
    best: tuple[float, Mapping[str, Any]] | None = None
    for ln in lines:
        unit = str(ln.get("unit") or "EA")
        score = similarity(_boq_row(q, unit), _boq_row(str(ln.get("description") or ""), unit))
        if best is None or score > best[0]:
            best = (score, ln)
    if best and best[0] >= MATCH_MIN:
        return best[1]
    # Fallback: every query token (2+ chars) appears in the description; the
    # shortest such description wins ("L1 troffer" finds the L1 line).
    tokens = [t for t in re.split(r"[^A-Z0-9/#]+", q.upper()) if len(t) >= 2]
    if not tokens:
        return None
    hits = []
    for ln in lines:
        desc = str(ln.get("description") or "").upper()
        words = set(re.split(r"[^A-Z0-9/#]+", desc))
        if all(t in words or t in desc for t in tokens):
            hits.append((len(desc), ln))
    return min(hits, key=lambda h: h[0])[1] if hits else None


def _resolve_row(ctx: ToolContext, ref: str) -> Mapping[str, Any] | None:
    try:
        store = ctx.svc().rows
    except Exception:  # noqa: BLE001 - no services registered
        return None
    if store is None:
        return None
    try:
        hit = store.resolve(ctx.run_id, ref)
    except Exception:  # noqa: BLE001 - an Unavailable default
        return None
    if isinstance(hit, Mapping) and not tools.is_unavailable(hit) and not hit.get("error"):
        return hit
    return None


def _compact(row: Mapping[str, Any]) -> dict[str, Any]:
    keep = ("id", "row_id", "description", "tag", "quantity", "qty", "unit", "basis", "coverage", "status", "sheet_key",
            "length_ft", "section", "title")
    out = {k: row.get(k) for k in keep if row.get(k) is not None}
    sheet = row.get("sheet")
    if isinstance(sheet, Mapping) and sheet.get("sheet_number"):
        out["sheet"] = sheet.get("sheet_number")
    return out


def derivation_text(line: Mapping[str, Any], sources: list[dict[str, Any]]) -> str:
    chip = line.get("qty_chip") if isinstance(line.get("qty_chip"), Mapping) else {}
    kind = str(chip.get("kind") or "")
    q = line.get("quantity")
    unit = str(line.get("unit") or "")
    qtext = f"{_num(q):g} {unit}" if _num(q) is not None else f"{q} {unit}"
    refs = ", ".join(f"{s['ref']} x{s['multiplier']:g}" if s.get("multiplier") not in (None, 1, 1.0) else str(s["ref"])
                     for s in sources if s.get("ref"))
    sheets = ", ".join(str(s) for s in line.get("sheets") or line.get("sheet_refs") or [])
    per = line.get("marks_by_method") or {}
    marks = sum(int(v or 0) for v in per.values())
    bits: list[str] = []
    if kind in ("counted", "census_floor", "partial", "mixed", "linear_luminaire_ea"):
        bits.append(f"{qtext} counted" + (f" from {refs}" if refs else " (legacy line without cited rows)"))
        if sheets:
            bits.append(f"on {sheets}")
        if marks:
            methods = ", ".join(f"{v} {k}" for k, v in sorted(per.items()) if v)
            bits.append(f"{marks} mark(s) placed ({methods})")
        nl = line.get("not_located")
        if nl:
            bits.append(f"{int(nl)} not located on a plan")
    elif kind in ("measured", "assumed_route"):
        bits.append(f"{qtext} {'measured' if kind == 'measured' else 'from assumed routes'}" + (f" from {refs}" if refs else ""))
        ft = line.get("polyline_ft")
        if _num(ft) is not None:
            bits.append(f"{_num(ft):g} FT drawn as {int(line.get('polylines') or 0)} polyline(s)")
        check = line.get("length_check") if isinstance(line.get("length_check"), Mapping) else None
        if check and check.get("ok") is False:
            bits.append("drawn length does not match the quantity")
    elif kind == "schedule":
        bits.append(f"{qtext} taken from a schedule" + (f" ({refs})" if refs else ""))
    elif kind == "stated":
        bits.append(f"{qtext} stated in the documents" + (f" ({refs})" if refs else ""))
    elif kind == "policy":
        bits.append(f"{qtext} set by the scope policy ({chip.get('text') or 'policy'})")
    elif kind == "manual":
        bits.append(f"{qtext} entered by a person")
    elif kind == "unsourced":
        bits.append(f"{qtext} with no cited source")
    elif kind == "unit_error":
        bits.append(f"{qtext} is a linear item counted in EA (unit error)")
    else:
        bits.append(f"{qtext}" + (f" from {refs}" if refs else ""))
    return "; ".join(bits) + "."


def _findings_for(rows: list[Mapping[str, Any]], line_id: str, *, review: bool) -> list[dict[str, Any]]:
    out = []
    for f in rows:
        ids: list[str] = []
        if review:
            ids = [str(i) for i in (f.get("line_ids") or [])]
        else:
            target = f.get("target") if isinstance(f.get("target"), Mapping) else {}
            if target.get("line_id"):
                ids.append(str(target["line_id"]))
            ids.extend(str(r) for r in (target.get("row_ids") or []))
            if f.get("status") not in ("open", "routed", None):
                continue
        if line_id not in ids:
            continue
        if review:
            out.append({"kind": f.get("kind"), "severity": f.get("severity"), "title": f.get("title"),
                        "human_label": f.get("human_label")})
        else:
            out.append({"gate_id": f.get("gate_id"), "severity": f.get("severity"), "status": f.get("status"),
                        "message": str(f.get("message") or "")[:300]})
        if len(out) >= MAX_FINDINGS:
            break
    return out


def pricing_actor(ctx: ToolContext) -> bool:
    """Whether the tool context's role may see prices in chat right now."""
    role = getattr(ctx, "role", None)
    if not role:
        return False
    try:
        from app.core.config import get_settings
        from app.core.features import ai_pricing_allowed
        from app.core.roles import Role

        if not getattr(get_settings(), "ai_pricing_chat_tools_enabled", False):
            return False
        return bool(ai_pricing_allowed(Role(str(role))))
    except Exception:  # noqa: BLE001 - fail closed
        return False


def _price_block(priced: Mapping[str, Any] | None) -> dict[str, Any]:
    chip = price_chip(priced)
    block: dict[str, Any] = {"chip": chip.to_dict()}
    if not priced:
        return block
    labor = priced.get("labor") if isinstance(priced.get("labor"), Mapping) else {}
    material = priced.get("material") if isinstance(priced.get("material"), Mapping) else {}
    block.update(
        {
            "labor_hours": priced.get("labor_hours"),
            "material_cost": priced.get("material_cost"),
            "unit_labor_hours": labor.get("value"),
            "unit_material_usd": material.get("value"),
            "labor_quality": labor.get("quality"),
            "material_quality": material.get("quality"),
            "n_obs": labor.get("n_obs") or material.get("n_obs"),
            "source": priced.get("material_source"),
            "flags": list(priced.get("flags") or []),
        }
    )
    return block


def explain_row(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    src = sources_for(ctx)
    evidence = src.evidence(ctx.run_id)
    if not isinstance(evidence, Mapping) or not evidence.get("lines"):
        return ToolResult.refusal("no_evidence", "This run has no evidence yet (it is built when the run renders).")
    line = find_line(evidence, str(args.get("line") or ""))
    if line is None:
        return ToolResult.refusal("unknown_line", f"No BOQ line matches {str(args.get('line') or '')[:80]!r}; use get_deliverable boq for the line ids.")
    line_id = str(line.get("line_id") or "")
    sources: list[dict[str, Any]] = []
    citations: list[str] = []
    rows_by_id: dict[str, Mapping[str, Any]] = {}
    for ref in line.get("source_refs") or []:
        r = ref.get("ref") if isinstance(ref, Mapping) else ref
        if not r:
            continue
        mult = _num(ref.get("multiplier")) if isinstance(ref, Mapping) else None
        row = _resolve_row(ctx, str(r))
        if row is not None:
            rows_by_id[str(r)] = row
        sources.append({"ref": str(r), "multiplier": mult if mult is not None else 1.0,
                        "row": _compact(row) if row else None,
                        "coverage": (row or {}).get("coverage")})
        citations.append(str(r))
    data: dict[str, Any] = {
        "line_id": line_id,
        "description": line.get("description"),
        "quantity": line.get("quantity"),
        "unit": line.get("unit"),
        "derivation": humanize(derivation_text(line, sources), rows_by_id),
        "basis": dict(line.get("qty_chip") or {}),
        "sources": sources,
        "marks_by_method": dict(line.get("marks_by_method") or {}),
        "not_located": line.get("not_located"),
        "polylines": line.get("polylines"),
        "polyline_ft": line.get("polyline_ft"),
        "length_check": line.get("length_check"),
        "sheets": list(line.get("sheets") or []),
        "gate_findings": _findings_for(src.gate_findings(ctx.run_id), line_id, review=False),
        "review_findings": _findings_for(src.review_findings(ctx.run_id), line_id, review=True),
    }
    if bool(args.get("include_marks")):
        marks = []
        for s in evidence.get("sheets") or []:
            if not isinstance(s, Mapping):
                continue
            for m in s.get("marks") or []:
                if isinstance(m, Mapping) and m.get("line_id") == line_id:
                    marks.append({"sheet_key": s.get("sheet_key"), "sheet_number": s.get("sheet_number"),
                                  "kind": m.get("kind"), "method": m.get("method"), "points": m.get("points"),
                                  "count": m.get("count")})
                    if len(marks) >= MAX_MARKS:
                        break
            if len(marks) >= MAX_MARKS:
                break
        data["marks"] = marks
    if pricing_actor(ctx):
        data["price"] = _price_block(src.priced_line(ctx.run_id, line_id))
        data["pricing_restricted"] = True
        ctx.pricing_served = True  # type: ignore[attr-defined] - read by the chat loop
    return ToolResult.ok(data, citations=citations)


EXPLAIN_ROW_SPEC = ToolSpec(
    name=EXPLAIN_ROW,
    description=EXPLAIN_ROW_DESCRIPTION,
    args_schema=EXPLAIN_ROW_ARGS,
    result_doc=EXPLAIN_ROW_RESULT_DOC,
    mode="read",
    kinds=kinds_from_codes("RV, C"),
    handler=explain_row,
    max_calls_per_group=40,
)


def register_tools() -> None:
    """Put explain_row in the registry (idempotent)."""
    register(EXPLAIN_ROW_SPEC, replace=True)


register_tools()


__all__ = [
    "EXPLAIN_ROW",
    "EXPLAIN_ROW_ARGS",
    "EXPLAIN_ROW_SPEC",
    "EmptyExplainSources",
    "ExplainSources",
    "FakeExplainSources",
    "SupabaseExplainSources",
    "derivation_text",
    "explain_row",
    "find_line",
    "humanize",
    "pricing_actor",
    "register_tools",
    "set_sources_factory",
    "sources_for",
]

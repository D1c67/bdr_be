"""The two pricing tools of the tool catalog (AI_ESTIMATING_BUILD_SPEC 5.2,
WS6): `price_lookup` and `get_price_summary`, registered in
`plans_reader_tools.REGISTRY` as `internal_pricing` tools, so the loop
offers them only to pricing actors (`offered_tools(pricing_allowed=True)`,
never to the external estimator) and chat redacts their answers for
readers who fail `ai_pricing_allowed` (WS8).

Handlers read the `PriceService` of the registered services
(`plans_reader_tools.services().price`): `LibraryPriceService` wraps a
`CostLibrary` and a policy for `lookup`, and an injected summary loader
for `summary`. Results carry aggregates only (medians, quartiles, counts,
months) and are scrubbed against a redaction list of project names, GC
names and addresses before they leave; a project id never enters them in
the first place (the library's rows are not exposed, only its statistics).

Pure apart from the summary loader the caller injects (pricing_run
supplies one that reads the latest version row).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from app.services import plans_reader_tools as tools
from app.services.cost_library import CostLibrary, Lookup, library_family, library_key
from app.services.electrical_items import canon_uom, parse_item
from app.services.pricing_policy import DEFAULT_POLICY, PricingPolicy, grade_seed_for
from app.services.pricing_types import SpecFact

PRICE_LOOKUP = "price_lookup"
GET_PRICE_SUMMARY = "get_price_summary"
LOOKUP_UOMS: tuple[str, ...] = ("EA", "FT", "LF", "C", "M", "CU FT", "CY", "SQ FT", "LS")
LOOKUP_KINDS = frozenset({"main", "review", "revise", "chat"})
SUMMARY_KINDS = frozenset({"review", "chat"})
TOP_LINES = 10
_FORBIDDEN_KEYS = frozenset({"project_id", "project_name", "gc_name", "gc_id", "address", "project", "gc"})
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
_SIX = Decimal("0.000001")

PRICE_LOOKUP_ARGS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["description", "uom", "quantity", "family_hint", "spec_facts"],
    "properties": {
        "description": {"type": "string", "minLength": 1, "maxLength": 300},
        "uom": {"type": "string", "enum": list(LOOKUP_UOMS)},
        "quantity": {"type": ["number", "null"]},
        "family_hint": {"type": ["string", "null"]},
        "spec_facts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "value"],
                "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
            },
        },
    },
}
GET_PRICE_SUMMARY_ARGS: dict[str, Any] = {"type": "object", "additionalProperties": False, "required": [], "properties": {}}


def _f(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _stat_doc(stat: Any) -> dict[str, Any]:
    return {
        "median": _f(stat.value), "p25": _f(stat.p25), "p75": _f(stat.p75),
        "n_obs": int(stat.n_obs), "n_projects": int(stat.n_projects), "quality": stat.quality,
        "method": stat.method, "latest_seen": stat.latest_seen,
    }


def scrub(value: Any, redact: Iterable[str] = ()) -> Any:
    """Drop forbidden keys, blank uuids and replace every redacted name
    (case-insensitive, whole phrase) with [redacted]."""
    names = [n for n in (str(x).strip() for x in redact) if len(n) >= 3]
    pattern = re.compile("|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)), re.IGNORECASE) if names else None

    def walk(obj: Any) -> Any:
        if isinstance(obj, Mapping):
            return {str(k): walk(v) for k, v in obj.items() if str(k).lower() not in _FORBIDDEN_KEYS}
        if isinstance(obj, (list, tuple)):
            return [walk(v) for v in obj]
        if isinstance(obj, str):
            text = _UUID_RE.sub("[id]", obj)
            return pattern.sub("[redacted]", text) if pattern else text
        if isinstance(obj, Decimal):
            return float(obj)
        return obj

    return walk(value)


class LibraryPriceService:
    """The PriceService of the catalog over a CostLibrary and a policy.
    `summary_loader(run_id)` returns the latest pricing version row (or
    None); `redact` lists the names that may never appear in a result."""

    def __init__(
        self,
        library: CostLibrary,
        *,
        policy: PricingPolicy = DEFAULT_POLICY,
        summary_loader: Callable[[str], Mapping[str, Any] | None] | None = None,
        lines_loader: Callable[[str], Sequence[Mapping[str, Any]]] | None = None,
        redact: Iterable[str] = (),
    ) -> None:
        self.library = library
        self.policy = policy
        self._summary = summary_loader
        self._lines = lines_loader
        self.redact = tuple(redact)

    def lookup(self, query: Mapping[str, Any]) -> dict[str, Any]:
        description = str(query.get("description") or "").strip()
        uom_raw = str(query.get("uom") or "").strip().upper()
        try:
            uom = canon_uom(uom_raw) if uom_raw else ""
        except ValueError:
            uom = ""
        if not description:
            return scrub({"status": "no_match", "matched": None, "candidates": [], "detail": "description is required"}, self.redact)
        if not uom or uom == "LS":
            return scrub({"status": "ls_not_priced", "matched": None, "candidates": [],
                          "detail": "lump sums are priced by quote or allowance, never by the library"}, self.redact)
        identity = parse_item(description, uom)
        if uom == "EA" and identity.item_class in {"conductor", "conduit", "cable", "raceway", "trench", "sawcut"}:
            return scrub({"status": "uom_conflict", "matched": None, "candidates": [],
                          "detail": f"{identity.item_class} is always FT; EA cannot be priced"}, self.redact)
        found: Lookup = self.library.lookup(identity, uom, thresholds=self.policy.quality)
        if not found.found:
            found = self.library.fallback(identity, uom)
        facts = tuple(SpecFact(key=str(f.get("key")), value=str(f.get("value"))) for f in query.get("spec_facts") or ())
        seed = grade_seed_for(self.policy, (library_key(identity), identity.item_key), facts)
        modifiers = [f"{f.key}={f.value}" for f in facts]
        if seed is not None:
            modifiers.append(f"seed:{seed[0].fact_key}={seed[0].fact_value} (needs a cited fact to apply)")
        if not found.found:
            return scrub({
                "status": "no_match", "matched": None, "labor_hours": None, "material": None,
                "candidates": list(found.candidates), "modifiers_applied": modifiers,
                "family": library_family(identity), "item_class": identity.item_class,
            }, self.redact)
        qty = query.get("quantity")
        extended = None
        if isinstance(qty, (int, float)) and not isinstance(qty, bool) and qty > 0:
            q = Decimal(str(qty))
            extended = {
                "hours": _f((found.labor.value * q).quantize(_SIX)) if found.labor.value is not None else None,
                "material_usd": _f((found.material.value * q).quantize(_SIX)) if found.material.value is not None else None,
            }
        return scrub({
            "status": "ok",
            "matched": {"item_key": found.matched_key, "uom": found.matched_uom, "method": found.method,
                        "family": library_family(identity), "item_class": identity.item_class},
            "labor_hours": _stat_doc(found.labor),
            "material": _stat_doc(found.material),
            "conversion": found.conversion,
            "modifiers_applied": modifiers,
            "flags": list(found.flags),
            "extended": extended,
            "candidates": list(found.candidates),
            "citation": f"$:{found.matched_key}@{self.library.fingerprint[:12]}" if found.matched_key else None,
        }, self.redact)

    def summary(self, run_id: str) -> dict[str, Any]:
        if self._summary is None:
            return tools.unavailable_result("price", "no pricing summary loader")
        row = self._summary(run_id)
        if not row:
            return scrub({"status": "none", "detail": "this run has no pricing version yet"}, self.redact)
        verify = row.get("verify_draft") or {}
        qs = row.get("quality_summary") or {}
        top: list[dict[str, Any]] = []
        if self._lines is not None:
            lines = []
            for ln in self._lines(str(row.get("id") or "")):
                line = ln.get("line") if isinstance(ln.get("line"), Mapping) else ln
                hours = line.get("labor_hours")
                material = line.get("material_cost")
                total = (Decimal(str(hours or 0)) * Decimal(str(verify.get("labor_rate") or 0))) + Decimal(str(material or 0))
                lines.append((total, line))
            lines.sort(key=lambda t: -t[0])
            for total, line in lines[:TOP_LINES]:
                top.append({
                    "line_id": line.get("line_id"), "description": (line.get("row") or {}).get("description"),
                    "qty": (line.get("row") or {}).get("quantity"), "unit": line.get("uom"),
                    "labor_hours": line.get("labor_hours"), "material_usd": line.get("material_cost"),
                    "quality": (line.get("labor") or {}).get("quality"), "flags": line.get("flags"),
                })
        return scrub({
            "status": row.get("status"),
            "version": row.get("version"),
            "stale": bool(row.get("stale")),
            "verify_draft": verify,
            "hours_basis": verify.get("labor_hours_basis"),
            "hours_reviewer": verify.get("labor_hours_reviewer"),
            "labor_rate": verify.get("labor_rate"),
            "labor_rate_source": verify.get("labor_rate_source"),
            "range": {"low": row.get("range_low"), "high": row.get("range_high")},
            "quality_shares": {"hours": qs.get("hours_share_by_quality"), "material": qs.get("material_share_by_quality")},
            "fallback_share_hours": qs.get("fallback_share_hours"),
            "provisional_reasons": qs.get("provisional_reasons"),
            "blockers": row.get("blockers"),
            "warnings": row.get("warnings"),
            "unpriced": row.get("unpriced"),
            "top_lines": top,
        }, self.redact)


def _price_service(ctx: tools.ToolContext) -> Any:
    return ctx.svc().price


def price_lookup_handler(ctx: tools.ToolContext, args: dict[str, Any]) -> tools.ToolResult:
    service = _price_service(ctx)
    result = service.lookup(dict(args))
    if tools.is_unavailable(result):
        return tools.ToolResult.refusal("unavailable", "the price service is not available", result)
    citations = [result["citation"]] if result.get("citation") else []
    return tools.ToolResult.ok(scrub(result), citations=citations)


def get_price_summary_handler(ctx: tools.ToolContext, args: dict[str, Any]) -> tools.ToolResult:
    service = _price_service(ctx)
    summary = getattr(service, "summary", None)
    if summary is None:
        return tools.ToolResult.refusal("unavailable", "the price service has no summary")
    result = summary(ctx.run_id)
    if tools.is_unavailable(result):
        return tools.ToolResult.refusal("unavailable", "the pricing summary is not available", result)
    return tools.ToolResult.ok(scrub(result))


PRICE_LOOKUP_SPEC = tools.ToolSpec(
    name=PRICE_LOOKUP,
    description=(
        "Unit labor hours and unit material cost of one item from the G3 cost library (medians, "
        "quartiles, project counts, quality), with strict unit matching: never EA against FT, never a "
        "lump sum. Pass spec facts (device_grade=hospital) to see which policy seeds would apply. "
        "Results are aggregates; they never name a project or a contractor."
    ),
    args_schema=PRICE_LOOKUP_ARGS,
    result_doc={
        "status": "ok | no_match | uom_conflict | ls_not_priced", "matched": "{item_key, uom, method, family, item_class}",
        "labor_hours": "{median, p25, p75, n_obs, n_projects, quality}", "material": "{..., latest_seen}",
        "conversion": "per-unit conversion note or null", "modifiers_applied": "[str]", "extended": "{hours, material_usd}",
        "candidates": "[{item_key, family, uom, n_projects, score}]", "citation": "$:<item_key>@<library>",
    },
    mode="read",
    kinds=frozenset(LOOKUP_KINDS),
    handler=price_lookup_handler,
    internal_pricing=True,
    max_calls_per_group=60,
)

GET_PRICE_SUMMARY_SPEC = tools.ToolSpec(
    name=GET_PRICE_SUMMARY,
    description=(
        "The latest pricing version of this run: status, the Verify draft (labor, materials, markups, "
        "bid total), basis and reviewer hours, the labor rate, the range, quality shares, blockers and the "
        "top lines by cost. Never names a project or a contractor."
    ),
    args_schema=GET_PRICE_SUMMARY_ARGS,
    result_doc={
        "status": "priced | provisional | blocked | none", "version": "int", "verify_draft": "{ten keys, bid_total}",
        "hours_basis": "number", "hours_reviewer": "number", "labor_rate": "number", "range": "{low, high}",
        "quality_shares": "{hours, material}", "blockers": "[...]", "top_lines": "[{line_id, description, ...}]",
    },
    mode="read",
    kinds=frozenset(SUMMARY_KINDS),
    handler=get_price_summary_handler,
    internal_pricing=True,
    max_calls_per_group=5,
)


def register_tools() -> None:
    for spec in (PRICE_LOOKUP_SPEC, GET_PRICE_SUMMARY_SPEC):
        tools.register(spec, replace=True)


register_tools()


__all__ = [
    "PRICE_LOOKUP", "GET_PRICE_SUMMARY", "LOOKUP_UOMS", "LibraryPriceService", "scrub", "register_tools",
    "PRICE_LOOKUP_SPEC", "GET_PRICE_SUMMARY_SPEC", "price_lookup_handler", "get_price_summary_handler",
]

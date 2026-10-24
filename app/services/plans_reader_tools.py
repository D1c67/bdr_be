"""Tool registry, service seams and citation rules for the AI Estimating tool
loop (docs/AI_ESTIMATING_BUILD_SPEC.md sections 4.3, 5.2 and 5.3; WS2).

This module is the S3 contract skeleton every workstream imports. It holds no
tool handlers of its own: WS1 to WS8 register their tools here at import time
with `register(ToolSpec(...))` and their service implementations with
`register_services(...)`. Until a sibling lands, `services()` returns
`Unavailable` placeholders whose every method answers the typed error result
`{"error": "unavailable", "service": <name>}`, so a handler can be written and
tested before the service behind it exists and nothing blocks on a sibling.

What lives here:

- `ToolSpec`, `ToolContext`, `ToolResult`: the three shapes of the tool loop
  (`plans_reader_toolloop.run_loop` calls `dispatch` per model tool call).
- `REGISTRY` plus `register`, `offered_tools`, `validate_args`, `dispatch`,
  `to_openai_tools`, `to_anthropic_tools`. Args schemas are strict (every
  property required, nullable via ["type", "null"], no extras), validated
  server-side with `jsonschema`; a refusal is a value, never an exception.
- Compaction: 12,000 chars per tool result (40,000 for get_output and
  get_deliverable), 80,000 per round, JSON lines rendering with nulls dropped.
- Service Protocols (TextIndex, CensusService, ...), `Unavailable` defaults and
  `Fake*` implementations with canned data for tests.
- Citation refs: `parse_ref` for the ref grammar and `validate_rows` for the
  citation validation rules of section 5.2 (basis vs ref kind, incomplete
  coverage never a sheet total, census floor on the sheet sum, linear classes
  in FT).

Rules: no network, no database, no provider SDK. Everything here is
deterministic and runs at $0. Pricing data never enters this module.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

import jsonschema

from app.services.electrical_items import LINEAR_CLASSES as LINEAR_CLASSES
from app.services.electrical_items import ItemIdentity, is_linear, parse_item
from app.services.estimating_contracts import QUANTITY_BASES, ROW_ID_PREFIXES
from app.services.plans_reader_schemas import validate_strict

logger = logging.getLogger(__name__)

# Vocabularies -----------------------------------------------------------------

MODES: tuple[str, ...] = ("read", "review", "write")
COST_CLASSES: tuple[str, ...] = ("free", "image")

KINDS: frozenset[str] = frozenset(
    {"sub", "escalation", "recount", "main", "review", "revise", "derive", "chat"}
)

# Short codes of the 5.2 catalog table. S and E exist for completeness: sub and
# escalation get no tools in this build, so a tool listing them is accepted by
# the registry but never offered by the loop (it does not run tools for them).
KIND_CODES: dict[str, str] = {
    "S": "sub",
    "E": "escalation",
    "RC": "recount",
    "M": "main",
    "RV": "review",
    "RS": "revise",
    "D": "derive",
    "C": "chat",
}

# "all" in the catalog table means these six.
ALL_TOOL_KINDS: frozenset[str] = frozenset(
    {"recount", "main", "review", "revise", "derive", "chat"}
)

RESULT_CHAR_LIMIT = 12_000
LARGE_RESULT_CHAR_LIMIT = 40_000
ROUND_CHAR_LIMIT = 80_000
LARGE_RESULT_TOOLS: frozenset[str] = frozenset({"get_output", "get_deliverable"})
DEFAULT_LIST_LIMIT = 40
QUOTE_CHAR_LIMIT = 300

# Quantity basis enum of the BOQ line v2 (section 5.1), from the contract
# module estimating_contracts.QUANTITY_BASES (alias kept for callers).
BASES: tuple[str, ...] = QUANTITY_BASES

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def kinds_from_codes(codes: str | Iterable[str]) -> frozenset[str]:
    """`kinds_from_codes("RC, M, RV")` -> frozenset of kind names; "all" means
    the six tool kinds of the catalog table."""
    if isinstance(codes, str):
        tokens = [c.strip() for c in codes.replace(";", ",").split(",")]
    else:
        tokens = [str(c).strip() for c in codes]
    out: set[str] = set()
    for tok in tokens:
        if not tok:
            continue
        if tok.lower() == "all":
            out |= ALL_TOOL_KINDS
        elif tok in KIND_CODES:
            out.add(KIND_CODES[tok])
        elif tok in KINDS:
            out.add(tok)
        else:
            raise ValueError(f"unknown kind code {tok!r}")
    return frozenset(out)


# Shapes -----------------------------------------------------------------------


@dataclass
class ToolResult:
    """What a tool hands back to the loop.

    `text` is the compact model-facing rendering (JSON lines); `data` is the
    full typed result stored in plans_reader_tool_calls; `citations` are the
    refs this result makes citable (they join the group's ledger);
    `views` are image parts (zoom crops) delivered with the result. A refusal
    (`refused=True`) carries `code` and `message` and never changes state.
    """

    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    citations: list[str] = field(default_factory=list)
    views: list[dict[str, Any]] = field(default_factory=list)
    refused: bool = False
    code: str | None = None
    message: str | None = None
    truncated: bool = False

    @classmethod
    def refusal(cls, code: str, message: str, detail: Any = None) -> ToolResult:
        data: dict[str, Any] = {"refused": True, "code": code, "message": message}
        if detail is not None:
            data["detail"] = detail
        return cls(
            text=json.dumps(data, separators=(",", ":"), default=str),
            data=data,
            refused=True,
            code=code,
            message=message,
        )

    @classmethod
    def ok(
        cls,
        data: dict[str, Any],
        *,
        text: str | None = None,
        citations: Iterable[str] = (),
        views: Iterable[dict[str, Any]] = (),
    ) -> ToolResult:
        return cls(
            text=text if text is not None else render_json(data),
            data=data,
            citations=list(citations),
            views=list(views),
        )

    def to_record(self) -> dict[str, Any]:
        """Row shape for plans_reader_tool_calls (images are not stored here)."""
        return {
            "result_text": self.text,
            "result_data": self.data,
            "citations": list(self.citations),
            "views": [{k: v for k, v in view.items() if k != "image"} for view in self.views],
            "refused": self.refused,
            "code": self.code,
            "message": self.message,
            "truncated": self.truncated,
        }


def refusal_dict(code: str, message: str, detail: Any = None) -> dict[str, Any]:
    """The structured refusal value of section 5.8."""
    out: dict[str, Any] = {"refused": True, "code": code, "message": message}
    if detail is not None:
        out["detail"] = detail
    return out


ToolHandler = Callable[["ToolContext", dict[str, Any]], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    """One registered tool (section 5.2). `kinds` lists the group kinds the
    tool may be offered to; `mode` is read (never mutates), review (writes
    only review findings) or write."""

    name: str
    description: str
    args_schema: dict[str, Any]
    result_doc: dict[str, Any]
    mode: str
    kinds: frozenset[str]
    handler: ToolHandler
    cost_class: str = "free"
    internal_pricing: bool = False
    needs_pro_max: bool = False
    max_calls_per_group: int = 20
    timeout_s: float = 30.0

    def result_limit(self) -> int:
        return result_limit(self.name)


@dataclass
class ToolContext:
    """Everything a handler may touch. `sb` is None in replay and tests;
    handlers must work from `services` and never from the database directly
    when it is None. `ledger_refs` is the group's citation ledger (refs the
    model may cite); `dispatch` adds every result's citations to it.
    `heartbeat` renews the job lease and raises when it is lost.
    `image_spec` is the llm_parts.ProviderImageSpec of the route the loop
    sends to (the loop sets it; None means the OpenAI spec), so image
    tools fit their views to that provider. `write_key` is the
    idempotency key of the current call (`group_id:call_id`, or
    `message_id:call_id` in chat): write and review handlers upsert by
    it, so a call delivered twice changes state once."""

    run_id: str
    kind: str
    sb: Any = None
    group_id: str | None = None
    message_id: str | None = None
    role: str | None = None
    pro_max: bool = False
    services: ToolServices | None = None
    ledger_refs: set[str] = field(default_factory=set)
    heartbeat: Callable[[], None] = lambda: None
    call_id: str | None = None
    call_counts: dict[str, int] = field(default_factory=dict)
    image_spec: Any = None
    # Chat callers (plans_reader_chat_adapter) set these so write tools can
    # apply the writer-role plus change-words rule of spec 5.2; loops leave
    # them unset.
    user_id: str | None = None
    message_text: str | None = None
    change_words: bool = False

    @property
    def write_key(self) -> str | None:
        owner = self.group_id or self.message_id
        if not owner or not self.call_id:
            return None
        return f"{owner}:{self.call_id}"

    def svc(self) -> ToolServices:
        return self.services if self.services is not None else services()

    def calls_made(self, name: str) -> int:
        return self.call_counts.get(name, 0)


# Compaction -------------------------------------------------------------------


def result_limit(tool_name: str) -> int:
    return LARGE_RESULT_CHAR_LIMIT if tool_name in LARGE_RESULT_TOOLS else RESULT_CHAR_LIMIT


def _truncation_note(omitted: int) -> str:
    return (
        f"\n[truncated: {omitted} chars omitted. Page with offset or narrow the"
        " query to see the rest.]"
    )


def compact_text(text: str, limit: int) -> tuple[str, bool]:
    """Cut `text` to `limit` chars, saying what was left out and how to page."""
    if len(text) <= limit:
        return text, False
    note = _truncation_note(len(text) - limit)
    keep = max(0, limit - len(note))
    # Recompute the note with the real omitted size so the numbers add up.
    note = _truncation_note(len(text) - keep)
    return text[:keep] + note, True


def compact_result(
    result: ToolResult, limit: int | None = None, *, tool_name: str = ""
) -> ToolResult:
    """Return `result` with its text within the per-result cap."""
    cap = limit if limit is not None else result_limit(tool_name)
    text, cut = compact_text(result.text, cap)
    if not cut:
        return result
    result.text = text
    result.truncated = True
    return result


def compact_round(
    results: list[ToolResult], *, round_limit: int = ROUND_CHAR_LIMIT
) -> list[ToolResult]:
    """Fit a round's results into `round_limit` chars. Water-filling: short
    results keep their text; the longest ones share what is left equally, so
    the cut is deterministic and no single result eats the round."""
    total = sum(len(r.text) for r in results)
    if total <= round_limit or not results:
        return results
    remaining = round_limit
    pending = sorted(range(len(results)), key=lambda i: (len(results[i].text), i))
    budget: dict[int, int] = {}
    while pending:
        share = remaining // len(pending)
        i = pending[0]
        if len(results[i].text) <= share:
            budget[i] = len(results[i].text)
            remaining -= budget[i]
            pending.pop(0)
            continue
        for j in pending:
            budget[j] = share
        break
    for i, cap in budget.items():
        compact_result(results[i], cap)
    return results


def _round_value(value: Any, key: str = "") -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        if key in ("bbox", "points", "region", "x", "y", "x0", "y0", "x1", "y1"):
            return round(value, 4)
        if key in ("usd", "cost", "price", "material", "amount"):
            return round(value, 2)
        return round(value, 3)
    if isinstance(value, dict):
        return {k: _round_value(v, k) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_round_value(v, key) for v in value]
    if isinstance(value, str) and key in ("text", "quote", "snippet"):
        if len(value) > QUOTE_CHAR_LIMIT:
            return value[:QUOTE_CHAR_LIMIT] + " [cut]"
    return value


def render_json(data: Any) -> str:
    """One compact JSON document: nulls dropped, numbers rounded, document
    text capped at 300 chars per item."""
    return json.dumps(_round_value(data), separators=(",", ":"), ensure_ascii=False, default=str)


def json_lines(
    items: list[dict[str, Any]],
    *,
    offset: int = 0,
    limit: int | None = None,
) -> tuple[str, int, int | None]:
    """Render `items[offset:offset + limit]` as one JSON object per line.
    Returns (text, omitted, next_offset). Every listing tool pages this way."""
    cap = DEFAULT_LIST_LIMIT if limit is None else max(1, int(limit))
    start = max(0, int(offset))
    page = items[start : start + cap]
    rest = max(0, len(items) - start - len(page))
    lines = [render_json(item) for item in page]
    if rest:
        lines.append(f"[{rest} more; next_offset={start + len(page)}]")
    return "\n".join(lines), rest, (start + len(page)) if rest else None


# Registry ---------------------------------------------------------------------

REGISTRY: dict[str, ToolSpec] = {}


def _register_prompt_part(spec: ToolSpec) -> None:
    """Every tool definition is part of the prompt hash (section 5.2). The
    WS0 module may not exist yet in this tree; skip quietly until it does."""
    try:
        from app.services import plans_reader_prompt_hash  # type: ignore[attr-defined]
    except ImportError:
        return
    register = getattr(plans_reader_prompt_hash, "register_prompt_part", None)
    if register is None:
        return
    try:
        register(f"tool:{spec.name}", render_json(to_openai_tools([spec])[0]))
    except Exception:  # noqa: BLE001 - never block a registration on the hash
        logger.exception("plans_reader_tools: prompt hash registration failed for %s", spec.name)


def register(spec: ToolSpec, *, replace: bool = False) -> ToolSpec:
    """Validate and add `spec` to REGISTRY. Raises ValueError on a bad spec
    (non-strict args schema, cost_class other than free or image, unknown
    kind or mode, a kind outside ALL_TOOL_KINDS, duplicate name unless
    replace=True)."""
    if not isinstance(spec, ToolSpec):
        raise ValueError("register expects a ToolSpec")
    if not _NAME_RE.match(spec.name or ""):
        raise ValueError(f"tool name {spec.name!r} must match {_NAME_RE.pattern}")
    if not spec.description or not spec.description.strip():
        raise ValueError(f"{spec.name}: description is required")
    if spec.mode not in MODES:
        raise ValueError(f"{spec.name}: mode must be one of {MODES}, got {spec.mode!r}")
    if spec.cost_class not in COST_CLASSES:
        raise ValueError(
            f"{spec.name}: cost_class must be free or image, got {spec.cost_class!r}"
            " (a tool that calls a model is not allowed)"
        )
    if not spec.kinds or not set(spec.kinds) <= KINDS:
        raise ValueError(f"{spec.name}: kinds must be a non-empty subset of {sorted(KINDS)}")
    if not set(spec.kinds) <= ALL_TOOL_KINDS:
        extra = sorted(set(spec.kinds) - ALL_TOOL_KINDS)
        raise ValueError(
            f"{spec.name}: kinds {extra} get no tools; sub and escalation get no tools in"
            f" this build (allowed: {sorted(ALL_TOOL_KINDS)})"
        )
    if not callable(spec.handler):
        raise ValueError(f"{spec.name}: handler must be callable")
    if int(spec.max_calls_per_group) < 1:
        raise ValueError(f"{spec.name}: max_calls_per_group must be >= 1")
    if float(spec.timeout_s) <= 0:
        raise ValueError(f"{spec.name}: timeout_s must be > 0")
    if not isinstance(spec.args_schema, dict) or spec.args_schema.get("type") != "object":
        raise ValueError(f"{spec.name}: args_schema must be an object schema")
    try:
        validate_strict(spec.args_schema)
        jsonschema.Draft202012Validator.check_schema(spec.args_schema)
    except (ValueError, jsonschema.SchemaError) as exc:
        raise ValueError(f"{spec.name}: args_schema is not strict: {exc}") from exc
    if spec.name in REGISTRY and not replace:
        raise ValueError(f"tool {spec.name!r} is already registered")
    REGISTRY[spec.name] = spec
    _register_prompt_part(spec)
    return spec


def unregister(name: str) -> None:
    REGISTRY.pop(name, None)


def get_spec(name: str) -> ToolSpec | None:
    return REGISTRY.get(name)


def offered_tools(
    kind: str,
    *,
    role: str | None = None,
    pro_max: bool = False,
    pricing_allowed: bool = False,
    mode: str | None = None,
) -> list[ToolSpec]:
    """The tools offered to a group of `kind`, sorted by name (cache
    stability). Hides internal_pricing tools unless `pricing_allowed` (and
    always for the external estimator), hides needs_pro_max tools in chat
    unless `pro_max`. With `mode`, read groups get only read tools, review
    groups read plus review, write groups everything. Kinds outside
    ALL_TOOL_KINDS (sub, escalation) get no tools in this build."""
    if kind not in KINDS:
        raise ValueError(f"unknown group kind {kind!r}")
    if mode is not None and mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    if kind not in ALL_TOOL_KINDS:
        return []
    allowed_modes = {
        None: set(MODES),
        "read": {"read"},
        "review": {"read", "review"},
        "write": set(MODES),
    }[mode]
    estimator = (role or "").lower() == "estimator"
    out: list[ToolSpec] = []
    for spec in REGISTRY.values():
        if kind not in spec.kinds:
            continue
        if spec.mode not in allowed_modes:
            continue
        if spec.internal_pricing and (not pricing_allowed or estimator):
            continue
        if kind == "chat" and spec.needs_pro_max and not pro_max:
            continue
        out.append(spec)
    out.sort(key=lambda s: s.name)
    return out


def _json_path(path: Iterable[Any]) -> str:
    return "$" + "".join(f"[{p!r}]" if isinstance(p, int) else f".{p}" for p in path)


def validate_args(spec: ToolSpec, args: Any) -> dict[str, Any] | None:
    """None when `args` satisfies the strict schema, else the structured
    refusal {refused, code: "invalid_args", message, detail}."""
    if not isinstance(args, dict):
        return refusal_dict(
            "invalid_args",
            f"{spec.name}: arguments must be a JSON object",
            [{"path": "$", "message": f"got {type(args).__name__}"}],
        )
    validator = jsonschema.Draft202012Validator(spec.args_schema)
    errors = sorted(validator.iter_errors(args), key=lambda e: (list(e.absolute_path), e.message))
    if not errors:
        return None
    detail = [{"path": _json_path(e.absolute_path), "message": e.message} for e in errors[:20]]
    first = detail[0]
    return refusal_dict(
        "invalid_args",
        f"{spec.name}: invalid arguments at {first['path']}: {first['message']}",
        detail,
    )


def dispatch(spec: ToolSpec, ctx: ToolContext, args: Any) -> ToolResult:
    """Run one tool call: validate args, enforce max_calls_per_group through
    the context counter, heartbeat, call the handler, compact the text and add
    the result's citations to the ledger. Handler exceptions become a
    refusal with code "tool_error". A heartbeat failure (lost lease) is NOT
    caught: the loop must stop without further writes."""
    problem = validate_args(spec, args)
    if problem is not None:
        return ToolResult.refusal(problem["code"], problem["message"], problem.get("detail"))
    made = ctx.calls_made(spec.name)
    if made >= spec.max_calls_per_group:
        return ToolResult.refusal(
            "max_calls",
            f"{spec.name}: call limit of {spec.max_calls_per_group} reached for this group;"
            " use what you have",
            {"calls_made": made, "max_calls_per_group": spec.max_calls_per_group},
        )
    ctx.call_counts[spec.name] = made + 1
    ctx.heartbeat()
    try:
        result = spec.handler(ctx, args)
    except Exception as exc:  # noqa: BLE001 - a tool bug must not kill the loop
        logger.exception("plans_reader_tools: %s raised", spec.name)
        result = ToolResult.refusal(
            "tool_error",
            f"{spec.name} failed: {type(exc).__name__}: {exc}",
            {"exception": type(exc).__name__},
        )
    if not isinstance(result, ToolResult):
        result = ToolResult.refusal(
            "tool_error",
            f"{spec.name} returned {type(result).__name__}, expected ToolResult",
        )
    compact_result(result, spec.result_limit())
    if result.citations and not result.refused:
        ctx.ledger_refs.update(result.citations)
    return result


# Provider tool definitions ------------------------------------------------------


def to_openai_tools(specs: Iterable[ToolSpec]) -> list[dict[str, Any]]:
    """Strict function tools for the OpenAI Responses API."""
    return [
        {
            "type": "function",
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.args_schema,
            "strict": True,
        }
        for spec in specs
    ]


def to_anthropic_tools(specs: Iterable[ToolSpec], *, strict: bool = True) -> list[dict[str, Any]]:
    """Anthropic Messages API tools. `strict` adds the strict flag where the
    SDK supports it (WS2 step 1 verifies; pass strict=False to omit it)."""
    out: list[dict[str, Any]] = []
    for spec in specs:
        tool: dict[str, Any] = {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.args_schema,
        }
        if strict:
            tool["strict"] = True
        out.append(tool)
    return out


# Service Protocols (section 5.3) -------------------------------------------------
#
# Results are plain dicts shaped per the 5.2 catalog. A service that cannot
# answer returns an error dict ({"error": ..., ...}) rather than raising, so a
# handler can relay it to the model as a one-sentence hint.


class TextIndex(Protocol):  # WS1
    def runs(self, document_id: str, page: int) -> list[dict[str, Any]] | dict[str, Any]: ...

    def search(
        self,
        query: str,
        *,
        document_ids: list[str] | None,
        pages: list[int] | None,
        mode: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any]: ...


class CensusService(Protocol):  # WS1 (text tags); symbols later
    def census(
        self,
        document_id: str,
        page: int,
        pattern: dict[str, Any],
        region: list[float] | None,
    ) -> dict[str, Any]: ...


class ScheduleIndex(Protocol):  # WS1
    def schedules(
        self,
        *,
        document_id: str | None,
        kind: str,
        name: str | None,
        row_key: str | None,
    ) -> dict[str, Any]: ...


class LegendIndex(Protocol):  # WS1
    def lookup(self, query: str | None, *, effect: str | None) -> dict[str, Any]: ...


class MeasureService(Protocol):  # WS3
    def measure_polyline(
        self,
        document_id: str,
        page: int,
        points: list[list[float]],
        vertical_ft: float | None,
    ) -> dict[str, Any]: ...

    def synthesize_routes(
        self,
        document_id: str | None,
        page: int | None,
        circuits: list[str] | None,
    ) -> dict[str, Any]: ...


class PriceService(Protocol):  # WS6
    def lookup(self, query: dict[str, Any]) -> dict[str, Any]: ...


class ScopePolicyService(Protocol):  # WS5
    def rules(
        self, *, topic: str | None, item_description: str | None, run_id: str
    ) -> dict[str, Any]: ...


class RowStore(Protocol):  # WS4
    def rows(
        self,
        run_id: str,
        *,
        kind: str | None,
        sheet_key: str | None,
        query: str | None,
        status: str | None,
        offset: int | None,
    ) -> dict[str, Any]: ...

    def row(self, run_id: str, row_id: str) -> dict[str, Any]: ...

    def census(self, run_id: str, sheet_key: str | None) -> dict[str, Any]: ...

    def resolve(self, run_id: str, ref: str) -> dict[str, Any] | None: ...


class DocumentAccess(Protocol):  # WS2
    def documents(self, run_id: str) -> list[dict[str, Any]] | dict[str, Any]: ...

    def page_size(self, document_id: str, page: int) -> dict[str, Any]: ...

    def pdf_bytes(self, document_id: str) -> bytes | dict[str, Any]: ...


SERVICE_NAMES: tuple[str, ...] = (
    "text",
    "census",
    "schedules",
    "legend",
    "measure",
    "price",
    "policy",
    "rows",
    "documents",
)


def unavailable_result(service: str, detail: str | None = None) -> dict[str, Any]:
    out = {
        "error": "unavailable",
        "service": service,
        "message": f"the {service} service is not available on this server yet",
    }
    if detail:
        out["detail"] = detail
    return out


def is_unavailable(result: Any) -> bool:
    return isinstance(result, dict) and result.get("error") == "unavailable"


class Unavailable:
    """Default for every service slot: any method call returns the typed
    error result, so handlers written before a sibling lands still run."""

    def __init__(self, service: str) -> None:
        self.service = service

    def __getattr__(self, name: str) -> Callable[..., dict[str, Any]]:
        if name.startswith("__"):
            raise AttributeError(name)

        def _method(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return unavailable_result(self.service, f"{self.service}.{name}")

        return _method

    def __repr__(self) -> str:
        return f"Unavailable({self.service!r})"


@dataclass
class ToolServices:
    text: Any = field(default_factory=lambda: Unavailable("text"))
    census: Any = field(default_factory=lambda: Unavailable("census"))
    schedules: Any = field(default_factory=lambda: Unavailable("schedules"))
    legend: Any = field(default_factory=lambda: Unavailable("legend"))
    measure: Any = field(default_factory=lambda: Unavailable("measure"))
    price: Any = field(default_factory=lambda: Unavailable("price"))
    policy: Any = field(default_factory=lambda: Unavailable("policy"))
    rows: Any = field(default_factory=lambda: Unavailable("rows"))
    documents: Any = field(default_factory=lambda: Unavailable("documents"))

    def available(self) -> list[str]:
        return [n for n in SERVICE_NAMES if not isinstance(getattr(self, n), Unavailable)]


_SERVICES = ToolServices()


def services() -> ToolServices:
    """The process-wide service registry handlers read through ctx.svc()."""
    return _SERVICES


def register_services(**impls: Any) -> ToolServices:
    """Providers call this at import: `register_services(text=MyTextIndex())`.
    Unknown names raise so a typo never silently leaves the Unavailable default."""
    unknown = sorted(set(impls) - set(SERVICE_NAMES))
    if unknown:
        raise ValueError(f"unknown service name(s) {unknown}; expected {SERVICE_NAMES}")
    for name, impl in impls.items():
        setattr(_SERVICES, name, impl)
    return _SERVICES


def reset_services() -> ToolServices:
    """Back to Unavailable defaults (tests)."""
    for name in SERVICE_NAMES:
        setattr(_SERVICES, name, Unavailable(name))
    return _SERVICES


# Fakes (canned data for tests and replay) ---------------------------------------


def doc8(document_id: str) -> str:
    """First 8 hex chars of the document uuid (collision falls back to the
    full uuid at run start; that check is the loop's)."""
    return (document_id or "")[:8]


class FakeTextIndex:
    """`runs` keyed by (document_id, page) -> list of {text, bbox}. Refs are
    `t:{doc8}:{page}:{index}`."""

    def __init__(self, runs_by_page: dict[tuple[str, int], list[dict[str, Any]]] | None = None):
        self._runs = runs_by_page or {}

    def runs(self, document_id: str, page: int) -> list[dict[str, Any]]:
        out = []
        for i, run in enumerate(self._runs.get((document_id, page), [])):
            out.append({"ref": f"t:{doc8(document_id)}:{page}:{i}", **run})
        return out

    def search(
        self,
        query: str,
        *,
        document_ids: list[str] | None = None,
        pages: list[int] | None = None,
        mode: str = "token",
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        q = (query or "").strip().lower()
        hits: list[dict[str, Any]] = []
        for (doc_id, page), runs in sorted(self._runs.items()):
            if document_ids and doc_id not in document_ids:
                continue
            if pages and page not in pages:
                continue
            for i, run in enumerate(runs):
                text = str(run.get("text", ""))
                low = text.lower()
                tokens = low.split()
                if mode == "exact":
                    ok = low == q
                elif mode == "prefix":
                    ok = any(t.startswith(q) for t in tokens)
                else:
                    ok = q in tokens or q in low
                if ok:
                    hits.append(
                        {
                            "ref": f"t:{doc8(doc_id)}:{page}:{i}",
                            "document_id": doc_id,
                            "page": page,
                            "sheet": run.get("sheet"),
                            "text": text,
                            "bbox": run.get("bbox"),
                            "region": run.get("region"),
                        }
                    )
        total = len(hits)
        page_hits = hits[offset : offset + limit]
        omitted = max(0, total - offset - len(page_hits))
        return {
            "hits": page_hits,
            "total": total,
            "omitted": omitted,
            "next_offset": (offset + len(page_hits)) if omitted else None,
        }


class FakeCensusService:
    """Canned censuses keyed by (document_id, page, pattern value)."""

    def __init__(self, counts: dict[tuple[str, int, str], dict[str, Any]] | None = None):
        self._counts = counts or {}
        self._issued = 0

    def census(
        self,
        document_id: str,
        page: int,
        pattern: dict[str, Any],
        region: list[float] | None = None,
    ) -> dict[str, Any]:
        key = (document_id, page, str(pattern.get("value", "")))
        canned = self._counts.get(key)
        self._issued += 1
        census_id = f"{doc8(document_id)}p{page}n{self._issued}"
        if canned is None or pattern.get("kind") == "symbol":
            return {
                "census_id": census_id,
                "ref": f"c:{census_id}",
                "count": None,
                "method": "unavailable",
                "occurrences": [],
                "clusters": [],
                "family_specific": False,
                "note": "no text layer census for this pattern",
            }
        count = int(canned["count"])
        return {
            "census_id": census_id,
            "ref": f"c:{census_id}",
            "count": count,
            "method": canned.get("method", "text_layer"),
            "occurrences": [
                {"ref": f"c:{census_id}:{k}", "bbox": b}
                for k, b in enumerate(canned.get("bboxes", [])[:count])
            ],
            "clusters": canned.get("clusters", []),
            "family_specific": bool(canned.get("family_specific", region is not None)),
            "note": canned.get("note"),
        }


class FakeScheduleIndex:
    def __init__(self, schedules: list[dict[str, Any]] | None = None):
        self._schedules = schedules or []

    def schedules(
        self,
        *,
        document_id: str | None = None,
        kind: str = "any",
        name: str | None = None,
        row_key: str | None = None,
    ) -> dict[str, Any]:
        out = []
        for sch in self._schedules:
            if document_id and sch.get("document_id") != document_id:
                continue
            if kind != "any" and sch.get("kind") != kind:
                continue
            if name and str(sch.get("name", "")).lower() != name.lower():
                continue
            rows = sch.get("rows", [])
            if row_key:
                rows = [r for r in rows if str(r.get("key", "")).lower() == row_key.lower()]
            out.append({**sch, "rows": rows})
        return {"schedules": out}


class FakeLegendIndex:
    def __init__(self, entries: list[dict[str, Any]] | None = None):
        self._entries = entries or []

    def lookup(self, query: str | None = None, *, effect: str | None = None) -> dict[str, Any]:
        q = (query or "").lower()
        hits = [
            e
            for e in self._entries
            if not q
            or q in str(e.get("tag", "")).lower()
            or q in str(e.get("description", "")).lower()
        ]
        return {"entries": hits, "modifiers": []}


class FakeMeasureService:
    """Lengths from page fractions with a fixed sheet size and scale."""

    def __init__(self, *, w_in: float = 36.0, h_in: float = 24.0, ft_per_in: float = 8.0):
        self.w_in = w_in
        self.h_in = h_in
        self.ft_per_in = ft_per_in
        self._issued = 0
        self.routes: list[dict[str, Any]] = []

    def measure_polyline(
        self,
        document_id: str,
        page: int,
        points: list[list[float]],
        vertical_ft: float | None = None,
    ) -> dict[str, Any]:
        self._issued += 1
        inches = 0.0
        for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
            inches += math.hypot((x1 - x0) * self.w_in, (y1 - y0) * self.h_in)
        length = inches * self.ft_per_in + float(vertical_ft or 0)
        measure_id = f"{doc8(document_id)}p{page}m{self._issued}"
        return {
            "measure_id": measure_id,
            "ref": f"m:{measure_id}",
            "length_ft": round(length, 1),
            "scale": {
                "text": f'1" = {self.ft_per_in:g}\'',
                "ft_per_in": self.ft_per_in,
                "view_id": None,
                "source": "fake",
            },
            "flags": [],
        }

    def synthesize_routes(
        self,
        document_id: str | None = None,
        page: int | None = None,
        circuits: list[str] | None = None,
    ) -> dict[str, Any]:
        routes = [r for r in self.routes if not circuits or r.get("circuit") in circuits]
        return {"routes": routes, "totals": {}, "caveats": []}


class FakePriceService:
    """Synthetic numbers only: never load real library values into a fake."""

    def __init__(self, table: dict[str, dict[str, Any]] | None = None):
        self._table = table or {}

    def lookup(self, query: dict[str, Any]) -> dict[str, Any]:
        key = str(query.get("description", "")).strip().lower()
        hit = self._table.get(key)
        if hit is None:
            return {"status": "no_match", "matched": None, "candidates": []}
        if hit.get("uom") and query.get("uom") and hit["uom"] != query["uom"]:
            return {"status": "uom_conflict", "matched": hit, "candidates": []}
        return {"status": "ok", "matched": hit, "candidates": []}


class FakeScopePolicyService:
    def __init__(self, rules: list[dict[str, Any]] | None = None):
        self._rules = rules or []

    def rules(
        self, *, topic: str | None = None, item_description: str | None = None, run_id: str = ""
    ) -> dict[str, Any]:
        hits = [r for r in self._rules if not topic or r.get("topic") == topic]
        return {
            "rules": [{"ref": f"p:{r['rule_id']}", **r} for r in hits],
            "bid_conditions": {"wage_type": None, "basis_refs": [], "needs_review": []},
            "standards": {},
            "decisions": [],
        }


class FakeRowStore:
    """Rows keyed by id; `resolve` answers refs from a side table of metadata
    (what the issuing service would know: census counts, route basis, view
    coverage)."""

    def __init__(
        self,
        rows: dict[str, dict[str, Any]] | None = None,
        refs: dict[str, dict[str, Any]] | None = None,
    ):
        self._rows = rows or {}
        self._refs = refs or {}

    def rows(
        self,
        run_id: str,
        *,
        kind: str | None = None,
        sheet_key: str | None = None,
        query: str | None = None,
        status: str | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        out = []
        for rid, row in sorted(self._rows.items()):
            if kind and not rid.startswith(kind):
                continue
            if sheet_key and row.get("sheet_key") != sheet_key:
                continue
            if status and row.get("status", "active") != status:
                continue
            if query and query.lower() not in str(row.get("description", "")).lower():
                continue
            out.append({"id": rid, **row})
        start = offset or 0
        page = out[start : start + DEFAULT_LIST_LIMIT]
        return {"rows": page, "total": len(out), "next_offset": None}

    def row(self, run_id: str, row_id: str) -> dict[str, Any]:
        row = self._rows.get(row_id)
        if row is None:
            return {"error": "unknown_row", "row_id": row_id}
        return {"id": row_id, **row}

    def census(self, run_id: str, sheet_key: str | None = None) -> dict[str, Any]:
        return {"floors": []}

    def resolve(self, run_id: str, ref: str) -> dict[str, Any] | None:
        if ref in self._refs:
            return self._refs[ref]
        if ref in self._rows:
            return {"id": ref, **self._rows[ref]}
        return None


class FakeDocumentAccess:
    def __init__(self, docs: list[dict[str, Any]] | None = None):
        self._docs = docs or []

    def documents(self, run_id: str) -> list[dict[str, Any]]:
        return list(self._docs)

    def page_size(self, document_id: str, page: int) -> dict[str, Any]:
        for d in self._docs:
            if d.get("id") == document_id:
                return {"w_in": d.get("w_in", 36.0), "h_in": d.get("h_in", 24.0), "rotation": 0}
        return {"error": "unknown_document", "document_id": document_id}

    def pdf_bytes(self, document_id: str) -> bytes | dict[str, Any]:
        for d in self._docs:
            if d.get("id") == document_id and d.get("pdf") is not None:
                return d["pdf"]
        return {"error": "unknown_document", "document_id": document_id}


def fake_services(**overrides: Any) -> ToolServices:
    """A ToolServices with every slot a fake (empty canned data unless given)."""
    base = ToolServices(
        text=FakeTextIndex(),
        census=FakeCensusService(),
        schedules=FakeScheduleIndex(),
        legend=FakeLegendIndex(),
        measure=FakeMeasureService(),
        price=FakePriceService(),
        policy=FakeScopePolicyService(),
        rows=FakeRowStore(),
        documents=FakeDocumentAccess(),
    )
    for name, impl in overrides.items():
        if name not in SERVICE_NAMES:
            raise ValueError(f"unknown service name {name!r}")
        setattr(base, name, impl)
    return base


# Citation refs (section 5.2) ----------------------------------------------------

REF_PREFIXES: tuple[str, ...] = ("t", "c", "s", "l", "sp", "v", "m", "r", "p", "$", "f")
# The contract row id grammar (estimating_contracts.parse_row_id): a prefix
# letter plus a positive integer without leading zeros ("B012" is not a ref).
ROW_ID_LETTERS: str = "".join(ROW_ID_PREFIXES)
_ROW_ID_RE = re.compile(rf"^([{ROW_ID_LETTERS}])([1-9][0-9]*)$")
_PREFIX_RE = re.compile(r"^(sp|t|c|s|l|v|m|r|p|f|\$):(.+)$")


def parse_ref(ref: Any) -> tuple[str, tuple[str, ...]]:
    """`parse_ref("t:1a2b3c4d:9:12")` -> ("t", ("1a2b3c4d", "9", "12"));
    `parse_ref("C12")` -> ("row", ("C", "12")); `parse_ref("$:conduit_emt_34@v3")`
    -> ("$", ("conduit_emt_34", "v3")). Raises ValueError on anything else."""
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError(f"empty citation ref {ref!r}")
    s = ref.strip()
    m = _ROW_ID_RE.match(s)
    if m:
        return "row", (m.group(1), m.group(2))
    m = _PREFIX_RE.match(s)
    if not m:
        raise ValueError(f"unknown citation ref {ref!r}")
    kind, rest = m.group(1), m.group(2)
    if kind == "$":
        item, _, version = rest.partition("@")
        if not item or not version:
            raise ValueError(f"library ref {ref!r} must be $:{{item_key}}@{{library_version}}")
        return kind, (item, version)
    if kind in ("s", "l"):
        # schedule ids are "doc8:sN" (5.2), so the row or entry is the LAST part.
        head, sep, tail = rest.rpartition(":")
        if not sep or not head or not tail:
            raise ValueError(f"citation ref {ref!r} must be {kind}:{{id}}:{{row}}")
        return kind, (head, tail)
    if kind == "c":
        head, sep, tail = rest.rpartition(":")
        if not sep:
            return kind, (rest,)
        if not head or not tail.isdigit():
            raise ValueError(f"census ref {ref!r} must be c:{{census_id}}[:k]")
        return kind, (head, tail)
    parts = tuple(rest.split(":"))
    if any(p == "" for p in parts):
        raise ValueError(f"malformed citation ref {ref!r}")
    expected = {"t": 3, "sp": 3, "v": 1, "m": 1, "r": 1, "p": 1, "f": 1}
    if len(parts) != expected[kind]:
        raise ValueError(f"citation ref {ref!r} has {len(parts)} parts, expected {expected[kind]}")
    return kind, parts


def is_ref(ref: Any) -> bool:
    try:
        parse_ref(ref)
    except ValueError:
        return False
    return True


def census_id_of(ref: str) -> str | None:
    """`c:X:3` and `c:X` -> "X"; None for any other ref."""
    try:
        kind, parts = parse_ref(ref)
    except ValueError:
        return None
    return parts[0] if kind == "c" else None


# Linear classes (always FT): LINEAR_CLASSES, parse_item and is_linear come
# from the WS6 item-identity module electrical_items (spec 5.1).


def _row_identity(description: str, unit: str, material_category: str | None) -> ItemIdentity:
    """The WS6 identity of a row (material_category is a hint only when the
    description alone classifies as other)."""
    identity = parse_item(description, unit)
    if identity.item_class == "other" and material_category:
        identity = parse_item(f"{material_category} {description}", unit)
    return identity


@dataclass(frozen=True)
class RowProblem:
    row_id: str
    code: str
    message: str
    severity: str = "block"  # block | warn

    def as_dict(self) -> dict[str, Any]:
        return {
            "row_id": self.row_id,
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }


# Which ref kinds support which quantity basis (5.2 "Citation validation").
# Row ids: C count rows support counted, S schedule rows support schedule,
# M measure rows support measured or assumed_route (per the row's own basis),
# P and O (specs, scope) support stated. "measured" through r: needs a route
# the resolver says is measured; "assumed_route" through r: needs an assumed
# one (an r: with unknown basis counts as assumed, never as measured).
_BASIS_REFS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    # basis: (ref prefixes, row letters)
    "counted": (frozenset({"v", "c", "t"}), frozenset({"C"})),
    "schedule": (frozenset({"s"}), frozenset({"S"})),
    "measured": (frozenset({"m", "r"}), frozenset({"M"})),
    "assumed_route": (frozenset({"r"}), frozenset({"M"})),
    "stated": (frozenset({"t", "sp"}), frozenset({"P", "O"})),
    "census_floor": (frozenset({"c"}), frozenset()),
    "policy": (frozenset({"p"}), frozenset()),
}

# Fallback relative census tolerance when settings are unavailable
# (settings.plans_reader_census_tol_strong is the source of truth).
_CENSUS_TOLERANCE = 0.05
Resolver = Callable[[str], dict[str, Any] | None]


def _row_id(row: dict[str, Any], index: int) -> str:
    for key in ("line_id", "row_id", "id", "row_key"):
        val = row.get(key)
        if val:
            return str(val)
    return f"#{index}"


def _row_refs(row: dict[str, Any]) -> list[str]:
    raw = row.get("source_refs")
    if raw is None:
        raw = row.get("refs")
    if raw is None:
        raw = row.get("citations")
    if raw is None:
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            ref = item.get("ref")
        else:
            ref = item
        if isinstance(ref, str) and ref.strip():
            out.append(ref.strip())
    return out


def _row_quantity(row: dict[str, Any]) -> float | None:
    for key in ("quantity", "qty", "qty_base"):
        val = row.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val)
    return None


def _resolve(resolver: Resolver | None, ref: str) -> dict[str, Any] | None:
    if resolver is None:
        return None
    try:
        meta = resolver(ref)
    except Exception:  # noqa: BLE001 - a resolver bug reads as "unknown ref"
        logger.exception("plans_reader_tools: resolver failed for %s", ref)
        return None
    return meta if isinstance(meta, dict) else None


def _route_is_measured(meta: dict[str, Any] | None) -> bool:
    if not meta:
        return False
    basis = str(meta.get("basis") or meta.get("method") or "").lower()
    return basis in ("measured", "measured_route")


def _meta_basis(meta: dict[str, Any] | None) -> str | None:
    if not meta:
        return None
    basis = meta.get("basis")
    return str(basis).lower() if basis else None


def _coverage_complete(meta: dict[str, Any] | None) -> bool | None:
    """True, False or None (unknown) from a view or census result."""
    if not meta:
        return None
    cov = meta.get("coverage")
    if isinstance(cov, dict) and "complete" in cov:
        return bool(cov["complete"])
    if isinstance(cov, str):
        return cov not in ("partial", "tile_subtotal", "inside_tiles_only")
    if "complete" in meta:
        return bool(meta["complete"])
    return None


def _supports(basis: str, kind: str, parts: tuple[str, ...], meta: dict[str, Any] | None) -> bool:
    prefixes, letters = _BASIS_REFS[basis]
    if kind == "row":
        letter = parts[0]
        if letter not in letters:
            return False
        if letter == "M":
            row_basis = _meta_basis(meta)
            if row_basis is None:
                return True
            if basis == "measured":
                return row_basis in ("measured", "measured_route")
            return row_basis.startswith("assumed")
        return True
    if kind not in prefixes:
        return False
    if kind == "r":
        measured = _route_is_measured(meta)
        return measured if basis == "measured" else not measured
    return True


def _default_census_tol() -> float:
    """settings.plans_reader_census_tol_strong, else _CENSUS_TOLERANCE."""
    try:
        from app.core.config import get_settings

        tol = getattr(get_settings(), "plans_reader_census_tol_strong", _CENSUS_TOLERANCE)
        return float(tol)
    except Exception:  # noqa: BLE001 - settings unavailable reads as the default
        return _CENSUS_TOLERANCE


def validate_rows(
    rows: list[dict[str, Any]],
    ledger_refs: set[str] | frozenset[str] | Iterable[str],
    resolver: Resolver | None = None,
    *,
    census_tol: float | None = None,
) -> list[RowProblem]:
    """The citation validation of section 5.2 for recount output and
    consolidator writes. `rows` are dicts with (any of) line_id or row_id,
    description, quantity, unit, basis, source_refs or refs (strings or
    {ref, multiplier}), coverage, sheet_key, caveat, override_reason,
    material_category, item_class. `ledger_refs` is the group's ledger;
    `resolver(ref)` returns the issuer's metadata for a ref carried in from
    inputs (None when unknown): census {count, family_specific}, route
    {basis}, view {coverage: {complete}}, measure row {basis}.

    Returns problems (severity "block" or "warn"); an empty list means the
    rows pass. Rules: every ref parses and is in the ledger or resolvable;
    basis vs ref kind; a count supported only by incomplete coverage cannot
    be a sheet total; the census floor applies to the sum of the rows on the
    same sheet citing the same family-specific census; linear classes are FT.
    Rows with no refs and a basis of manual or mixed pass the ref checks
    (manual needs a human id; mixed needs at least one ref). `census_tol` is
    the relative census tolerance (minimum 1); None reads
    settings.plans_reader_census_tol_strong (0.05 when unset).
    """
    tol_rel = _default_census_tol() if census_tol is None else float(census_tol)
    ledger = set(ledger_refs or ())
    problems: list[RowProblem] = []
    census_groups: dict[tuple[str, str], list[tuple[str, float | None, dict[str, Any]]]] = {}
    census_meta: dict[str, dict[str, Any]] = {}

    for index, row in enumerate(rows):
        rid = _row_id(row, index)
        basis = str(row.get("basis") or "").lower()
        refs = _row_refs(row)
        parsed: list[tuple[str, str, tuple[str, ...], dict[str, Any] | None]] = []

        for ref in refs:
            try:
                kind, parts = parse_ref(ref)
            except ValueError as exc:
                problems.append(RowProblem(rid, "malformed_ref", f"{ref}: {exc}"))
                continue
            meta = _resolve(resolver, ref)
            if ref not in ledger and meta is None:
                problems.append(
                    RowProblem(
                        rid,
                        "unknown_ref",
                        f"citation {ref} was not returned by any tool in this step",
                    )
                )
                continue
            parsed.append((ref, kind, parts, meta))

        if basis in _BASIS_REFS:
            if not refs:
                problems.append(
                    RowProblem(rid, "unsourced_line", f"basis {basis} needs at least one citation")
                )
            elif parsed and not any(_supports(basis, k, p, m) for _, k, p, m in parsed):
                prefixes, letters = _BASIS_REFS[basis]
                want = ", ".join(sorted(f"{p}:" for p in prefixes) + sorted(letters))
                problems.append(
                    RowProblem(
                        rid,
                        "basis_ref_mismatch",
                        f"basis {basis} needs a citation of kind {want}; cited "
                        + ", ".join(k if k != "row" else p[0] for _, k, p, _ in parsed),
                    )
                )
        elif basis == "mixed" and not refs:
            problems.append(RowProblem(rid, "unsourced_line", "basis mixed needs citations"))
        elif basis == "manual" and not (row.get("user_id") or row.get("created_by")):
            problems.append(RowProblem(rid, "manual_needs_user", "basis manual needs a user id"))
        elif basis and basis not in BASES:
            problems.append(RowProblem(rid, "unknown_basis", f"unknown basis {basis!r}"))

        # Incomplete coverage cannot be a sheet total (E11).
        coverage = str(row.get("coverage") or "").lower()
        if coverage == "sheet_total":
            count_refs = [(r, m) for r, k, _, m in parsed if k in ("v", "c")]
            text_refs = [r for r, k, _, _ in parsed if k == "t"]
            if count_refs and not text_refs:
                flags = [_coverage_complete(m) for _, m in count_refs]
                if all(f is False for f in flags):
                    problems.append(
                        RowProblem(
                            rid,
                            "partial_as_total",
                            "every count citation covers only part of the sheet; the row cannot"
                            " be a sheet total (use coverage partial or cite a complete census)",
                        )
                    )

        # Census floor groups: same sheet, same census id.
        sheet_key = str(row.get("sheet_key") or "")
        for ref, kind, parts, meta in parsed:
            if kind != "c":
                continue
            cid = parts[0]
            if meta and "count" in meta:
                census_meta[cid] = meta
            census_groups.setdefault((sheet_key, cid), []).append((rid, _row_quantity(row), row))

        # Linear classes are FT (E10, E15).
        # An explicit item_class wins; otherwise the WS6 identity decides, so the
        # demolition of a linear item (REMOVAL OF LIGHTING TRACK) is linear too.
        unit = str(row.get("unit") or "").upper()
        cls = row.get("item_class")
        if cls:
            linear, label = cls in LINEAR_CLASSES, str(cls)
        else:
            identity = _row_identity(
                str(row.get("description") or ""), unit, row.get("material_category")
            )
            linear, label = is_linear(identity), identity.item_class
            if identity.item_class == "demolition" and identity.attrs.get("subject"):
                label = f"demolition of {identity.attrs['subject']}"
        if linear and unit and unit != "FT":
            problems.append(
                RowProblem(
                    rid,
                    "linear_in_ea",
                    f"{label} is a linear class and must be FT, not {unit}",
                )
            )

    for (sheet_key, cid), members in census_groups.items():
        meta = census_meta.get(cid)
        if not meta or not meta.get("family_specific"):
            continue
        count = meta.get("count")
        if not isinstance(count, (int, float)) or count is None:
            continue
        quantities = [q for _, q, _ in members if q is not None]
        if not quantities:
            continue
        total = sum(quantities)
        tol = max(1, math.ceil(tol_rel * float(count)))
        ids = [rid for rid, _, _ in members]
        where = f"sheet {sheet_key}" if sheet_key else "the sheet"
        if total < float(count) - tol and not any(r.get("override_reason") for _, _, r in members):
            problems.append(
                RowProblem(
                    ids[0],
                    "census_floor",
                    f"rows {', '.join(ids)} citing census {cid} on {where} sum to {total:g},"
                    f" below the census floor of {count:g} (tolerance {tol}); give an"
                    " override_reason or account for the rest",
                )
            )
        elif total > float(count) + tol and not any(r.get("caveat") for _, _, r in members):
            problems.append(
                RowProblem(
                    ids[0],
                    "census_over",
                    f"rows {', '.join(ids)} citing census {cid} on {where} sum to {total:g},"
                    f" above the census count of {count:g} (tolerance {tol}) with no caveat",
                    severity="warn",
                )
            )
    return problems


def blocking(problems: Iterable[RowProblem]) -> list[RowProblem]:
    return [p for p in problems if p.severity == "block"]


def problems_refusal(problems: Iterable[RowProblem], code: str = "citation") -> dict[str, Any]:
    """The structured refusal for a write whose rows failed validation."""
    items = [p.as_dict() for p in blocking(problems)]
    first = items[0]["message"] if items else "rows did not validate"
    return refusal_dict(code, first, items)

"""Registry tools for the existing AI Estimating chat loop (WS2 wave 3;
docs/AI_ESTIMATING_BUILD_SPEC.md section 6 WS2 "Chat keeps its current
`_tool_loop` on `complete_parts` in this build; registry tools are added
through an adapter", 5.2 kinds C and Pro Max, 5.5 chat visibility).

The chat loop (`plans_reader_chat._tool_loop`, owned by WS8) keeps its own
seven legacy tools (CHAT_TOOLS) and its own round, cap and spend logic.
This module only adds the registry tools of kind `chat` to it:

- `tool_definitions(ctx)`: the OpenAI function tools of the registry
  tools this message may use, sorted by name (cache stability), appended
  after CHAT_TOOLS by the chat loop.
- `handles(ctx, name)` and `serve(ctx, name, args)`: a registry tool call
  is run through `plans_reader_tools.dispatch` (strict argument
  validation, per-message call limits, the job lease heartbeat, result
  compaction) and comes back in the chat loop's own `(output, summary)`
  shape. Image views (zoom_region, find_linear_items) are attached to the
  next call the way read_pages attaches pages (Pro Max only, counted
  against the per-message page cap and the per-step image bytes).
- `pricing_restricted(ctx)`: True once an `internal_pricing` tool ran in
  this message, so WS8 tags the message `pricing_restricted` (5.5).
- `pricing_allowed_for_message(ctx)` and `sender_role(sb, message)`: the
  SENDER's role (from the message's `sender_role`, else the profile of
  `created_by`) decides whether pricing tools are offered at all.

What is offered (all must hold):

1. The tool is registered for kind `chat` and is not a review tool
   (chat records no review findings) and does not shadow a legacy chat
   tool name (the legacy tool wins).
2. Its parent flag is on (`TOOL_PARENTS`; section 5.7: a behavior reaches
   people only when its parent is on). A tool without an entry is never
   offered (fail closed) until its owner adds one with `set_tool_parents`.
   With every parent off (prod today) chat is byte-identical to before.
3. The chat route allows tools (`plans_reader_routes` step `chat`).
4. `internal_pricing` tools only when `features.ai_pricing_allowed(sender
   role)` and `ai_pricing_chat_tools_enabled` (so never for the Accountant,
   never for the external estimator, and under `ai_pricing_state=test`
   never for an engineer).
5. `needs_pro_max` tools only when the user turned Pro Max on for the
   message.
6. `write` tools (set_linear_item, set_panel_location) only for a writer
   role whose own message asks for a change (`ctx.modify_allowed`, the
   same change-word rule that unlocks modify_deliverable).

Rules: no network, no provider SDK, no model call (registry tools are free
or image tools by contract). The only database reads are the sender's
profile (once per message, and only when some tool passed the flag gates)
and the run's services, through the same sync client the chat job already
uses (queue worker threads, never inside async def). For a pricing actor
the message's `price` slot is a RunPriceService (the process registry has
none): the cost library and policy load on the first price_lookup, the
summary reads the run's latest pricing version.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.services import plans_reader_tools as tools

logger = logging.getLogger(__name__)

KIND = "chat"
STATE_ATTR = "_plans_reader_registry_tools"

# Image bytes held for one chat step: the chat loop's own rule
# (plans_reader_chat.PAGE_IMAGES_MAX_BYTES), restated so this module never
# imports the chat module (the chat module imports this one).
IMAGES_MAX_BYTES_DEFAULT = 120 * 1024 * 1024
SUMMARY_MAX_CITATIONS = 20

_PAGE_FACTS = ("plans_reader_page_facts_enabled",)
_MEASURE = ("plans_reader_derive_enabled",)
_SCOPE = ("ai_scope_policy_enforce",)
_ROWS = ("plans_reader_gates_blocking", "plans_reader_code_regions")
_PRICING = ("ai_pricing_enabled",)

# Parent flags per registry tool (any one on enables the tool in chat).
# Section 5.7 owners: WS1 page facts (and WS2 zoom, which targets from the
# text layer), WS3 derive and measure, WS5 scope policy, WS4 rows, WS6
# pricing, WS8 explain_row (its sources are WS4 rows and WS3 routes).
TOOL_PARENTS: dict[str, tuple[str, ...]] = {
    "sheet_index": _PAGE_FACTS,
    "text_search": _PAGE_FACTS,
    "census": _PAGE_FACTS,
    "get_schedule": _PAGE_FACTS,
    "legend_lookup": _PAGE_FACTS,
    "keynote_lookup": _PAGE_FACTS,
    "spec_lookup": _PAGE_FACTS,
    "zoom_region": _PAGE_FACTS,
    "measure_polyline": _MEASURE,
    "synthesize_routes": _MEASURE,
    "find_linear_items": _MEASURE,
    "explain_quantity": _MEASURE,
    "set_linear_item": _MEASURE,
    "set_panel_location": _MEASURE,
    "get_scope_policy": _SCOPE,
    "get_facts": _SCOPE,
    "get_rows": _ROWS,
    "get_row": _ROWS,
    "get_census": _ROWS,
    "price_lookup": _PRICING,
    "get_price_summary": _PRICING,
    "explain_row": _ROWS + _MEASURE,
}

# Optional override of how the run's ToolServices are built (tests, offline
# tools). Signature: factory(ctx) -> plans_reader_tools.ToolServices.
_SERVICES_FACTORY: Callable[[Any], Any] | None = None


def set_tool_parents(name: str, parents: Iterable[str]) -> None:
    """Name the parent flags of a registry tool (its owner calls this next
    to its registration when it is not in TOOL_PARENTS)."""
    flags = tuple(str(p) for p in parents if str(p).strip())
    if not flags:
        raise ValueError("a chat tool needs at least one parent flag")
    TOOL_PARENTS[str(name)] = flags


def set_services_factory(factory: Callable[[Any], Any] | None) -> None:
    """Replace (or with None restore) how a message's ToolServices are built."""
    global _SERVICES_FACTORY
    _SERVICES_FACTORY = factory


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def _settings(ctx: Any) -> Any:
    return getattr(ctx, "s", None) or get_settings()


def _flag(s: Any, name: str) -> bool:
    return bool(getattr(s, name, False))


def tool_enabled(name: str, settings: Any = None) -> bool:
    """True when one of the tool's parent flags is on (False for a tool
    with no TOOL_PARENTS entry)."""
    s = settings or get_settings()
    parents = TOOL_PARENTS.get(name)
    if not parents:
        return False
    return any(_flag(s, p) for p in parents)


def chat_route_allows_tools(settings: Any = None) -> bool:
    """The `tools` field of the chat step route (false when it cannot be
    resolved, so a broken route never widens what chat offers)."""
    s = settings or get_settings()
    try:
        from app.services import plans_reader_model_routes as model_routes

        return bool(model_routes.resolve_step("chat", s).tools)
    except Exception:  # noqa: BLE001 - fail closed
        logger.debug("plans_reader_chat_adapter: chat route not resolvable", exc_info=True)
        return False


def _legacy_names() -> frozenset[str]:
    from app.services.plans_reader_schemas import CHAT_TOOLS

    return frozenset(str(t.get("name")) for t in CHAT_TOOLS)


def _role(value: Any) -> Any:
    from app.core.roles import Role

    if value is None or value == "":
        return None
    if isinstance(value, Role):
        return value
    try:
        return Role(str(value))
    except ValueError:
        return None


def sender_role(sb: Any, message: dict[str, Any] | None) -> Any:
    """The Role of the user who sent the message: the message's persisted
    `sender_role` when set (migration 0165), else the `profiles.role` of
    its `created_by`. None when neither can be read (fails closed: no
    pricing tools, no write tools)."""
    message = message or {}
    role = _role(message.get("sender_role"))
    if role is not None:
        return role
    uid = message.get("created_by")
    if not uid or sb is None:
        return None
    try:
        rows = sb.table("profiles").select("role").eq("id", str(uid)).limit(1).execute().data or []
    except Exception:  # noqa: BLE001 - an unreadable profile reads as no role
        logger.warning("plans_reader_chat_adapter: profile of %s not readable", uid)
        return None
    return _role(rows[0].get("role")) if rows else None


def pricing_allowed_for_role(role: Any, settings: Any = None) -> bool:
    """Pricing tools in chat: the role may see AI prices right now
    (features.ai_pricing_allowed: AI_PRICING_ENABLED, AI_PRICING_STATE,
    never the Accountant or the estimator) and AI_PRICING_CHAT_TOOLS_ENABLED."""
    s = settings or get_settings()
    if not _flag(s, "ai_pricing_chat_tools_enabled"):
        return False
    from app.core import features

    try:
        return bool(features.ai_pricing_allowed(role))
    except Exception:  # noqa: BLE001 - fail closed
        return False


def _is_writer(role: Any) -> bool:
    from app.core.roles import WRITER_ROLES

    return role is not None and role in WRITER_ROLES


# ---------------------------------------------------------------------------
# Per-message state
# ---------------------------------------------------------------------------


@dataclass
class ChatToolState:
    """What one assistant message's registry tools share: the sender's role
    and pricing permission, the offered specs, the ToolContext (citation
    ledger and call counters across the message's rounds) and whether a
    pricing tool ran."""

    role: Any = None
    role_resolved: bool = False
    pricing_allowed: bool = False
    specs: dict[str, tools.ToolSpec] = field(default_factory=dict)
    offered_computed: bool = False
    tool_ctx: tools.ToolContext | None = None
    pricing_used: bool = False
    calls: int = 0
    images_attached: int = 0


def state_for(ctx: Any) -> ChatToolState:
    state = getattr(ctx, STATE_ATTR, None)
    if not isinstance(state, ChatToolState):
        state = ChatToolState()
        try:
            setattr(ctx, STATE_ATTR, state)
        except AttributeError:  # a slotted stand-in; state then lives per call
            pass
    return state


def _resolve_role(ctx: Any, state: ChatToolState) -> None:
    if state.role_resolved:
        return
    state.role = sender_role(getattr(ctx, "sb", None), getattr(ctx, "message", None))
    state.pricing_allowed = pricing_allowed_for_role(state.role, _settings(ctx))
    state.role_resolved = True


def offered_specs(ctx: Any) -> list[tools.ToolSpec]:
    """The registry ToolSpecs this message may use (gates in the module
    docstring), sorted by name. Computed once per message."""
    state = state_for(ctx)
    if state.offered_computed:
        return [state.specs[n] for n in sorted(state.specs)]
    s = _settings(ctx)
    state.offered_computed = True
    state.specs = {}
    if not _flag(s, "plans_reader_enabled"):
        return []
    legacy = _legacy_names()
    candidates = [
        spec
        for spec in tools.REGISTRY.values()
        if KIND in spec.kinds and spec.mode != "review" and spec.name not in legacy and tool_enabled(spec.name, s)
    ]
    if not candidates or not chat_route_allows_tools(s):
        return []
    _resolve_role(ctx, state)
    role = state.role
    offered = tools.offered_tools(
        KIND,
        role=str(role.value) if role is not None else None,
        pro_max=bool(getattr(ctx, "pro_max", False)),
        pricing_allowed=state.pricing_allowed,
    )
    allowed_names = {spec.name for spec in candidates}
    may_write = _is_writer(role) and bool(getattr(ctx, "modify_allowed", False))
    for spec in offered:
        if spec.name not in allowed_names:
            continue
        if spec.mode == "write" and not may_write:
            continue
        state.specs[spec.name] = spec
    return [state.specs[n] for n in sorted(state.specs)]


def tool_definitions(ctx: Any) -> list[dict[str, Any]]:
    """OpenAI function tools to append after CHAT_TOOLS (empty with every
    parent flag off)."""
    return tools.to_openai_tools(offered_specs(ctx))


def handles(ctx: Any, name: str) -> bool:
    """True when `name` is a registry tool offered in this message."""
    offered_specs(ctx)
    return str(name or "") in state_for(ctx).specs


def pricing_allowed_for_message(ctx: Any) -> bool:
    """Whether the sender may see pricing in this message (WS8 makes the
    system prompt's pricing clause conditional on it)."""
    state = state_for(ctx)
    _resolve_role(ctx, state)
    return state.pricing_allowed


def pricing_restricted(ctx: Any) -> bool:
    """True once an internal_pricing tool ran in this message (WS8 tags the
    message and its tool-call records pricing_restricted)."""
    return state_for(ctx).pricing_used


def sender_role_value(ctx: Any) -> str | None:
    """The sender's role as stored on the message row (`sender_role`)."""
    state = state_for(ctx)
    _resolve_role(ctx, state)
    return str(state.role.value) if state.role is not None else None


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------


def _build_services(ctx: Any) -> Any:
    if _SERVICES_FACTORY is not None:
        return _SERVICES_FACTORY(ctx)
    from app.services import plans_reader_evidence_inputs as evidence_inputs
    from app.services import plans_reader_tool_handlers as tool_handlers

    sb = getattr(ctx, "sb", None)
    run_id = str((getattr(ctx, "run", None) or {}).get("id") or "")
    rows: list[dict[str, Any]] | None = None
    documents = getattr(ctx, "documents", None)
    if callable(documents):
        try:
            rows = list(documents())
        except Exception:  # noqa: BLE001 - RunDocuments reads them itself
            rows = None
    services = tool_handlers.services_for_run(
        sb, run_id, inputs=evidence_inputs.inputs_for(sb), document_rows=rows
    )
    try:
        from app.services.pagefacts import services as pagefacts_services

        services = pagefacts_services.bind_services(services, sb, run_id, documents=rows)
    except Exception:  # noqa: BLE001 - the unbound registry still answers
        logger.exception("plans_reader_chat_adapter: binding page facts services failed for run %s", run_id)
    _bind_price(ctx, services)
    return services


class RunPriceService:
    """The `price` slot of one chat message (pricing_tools.PriceService
    shape: `lookup(query)` and `summary(run_id)`). The process registry has
    no price service (pricing_run builds its library per job), so chat
    binds one per message, lazily: the cost library and the policy load on
    the first price_lookup, the summary reads the run's latest pricing
    version (pricing_run.latest_version and version_lines). The library
    excludes the run's own project and its eval exclusions
    (pricing_run.load_library). Sync client only (chat jobs run on queue
    worker threads, never inside async def)."""

    def __init__(self, sb: Any, run: dict[str, Any]) -> None:
        self.sb = sb
        self.run = dict(run or {})
        self.run_id = str(self.run.get("id") or "")
        self._lookup_service: Any = None
        self._summary_service: Any = None

    def _loaders(self) -> dict[str, Any]:
        from app.services import pricing_run

        return {
            "summary_loader": lambda rid: pricing_run.latest_version(self.sb, str(rid)),
            "lines_loader": lambda pid: pricing_run.version_lines(self.sb, str(pid)),
        }

    def _policy(self) -> Any:
        from app.services import pricing_run
        from app.services.pricing_policy import DEFAULT_POLICY

        try:
            policy, _meta = pricing_run.load_policy(self.sb)
            return policy
        except Exception:  # noqa: BLE001 - the default policy still prices
            logger.warning("plans_reader_chat_adapter: pricing policy not readable; using the default")
            return DEFAULT_POLICY

    def lookup(self, query: Any) -> dict[str, Any]:
        if self._lookup_service is None:
            from app.services import pricing_run, pricing_tools

            try:
                library = pricing_run.load_library(self.sb, self.run, warnings=[])
            except Exception as exc:  # noqa: BLE001 - reported as unavailable, never raised into chat
                logger.warning("plans_reader_chat_adapter: cost library not loadable for run %s", self.run_id)
                return tools.unavailable_result("price", f"the cost library is not available: {type(exc).__name__}")
            self._lookup_service = pricing_tools.LibraryPriceService(library, policy=self._policy(), **self._loaders())
        return self._lookup_service.lookup(query)

    def summary(self, run_id: str) -> dict[str, Any]:
        if str(run_id) != self.run_id:
            return tools.unavailable_result("price", "only this run's pricing summary is available")
        service = self._lookup_service
        if service is None:
            if self._summary_service is None:
                from app.services import pricing_tools
                from app.services.cost_library import CostLibrary

                # The summary needs no library rows: an empty library keeps
                # the first summary call from paging the cost observations.
                self._summary_service = pricing_tools.LibraryPriceService(CostLibrary(()), **self._loaders())
            service = self._summary_service
        return service.summary(str(run_id))


def _bind_price(ctx: Any, services: Any) -> None:
    """Fill an Unavailable `price` slot with a RunPriceService, only when
    the sender may see prices and an internal_pricing tool is offered in
    this message (so no other answer ever holds one)."""
    try:
        if not isinstance(getattr(services, "price", None), tools.Unavailable):
            return
        state = state_for(ctx)
        if not state.pricing_allowed or not any(spec.internal_pricing for spec in state.specs.values()):
            return
        sb = getattr(ctx, "sb", None)
        run = getattr(ctx, "run", None) or {}
        if sb is None or not run.get("id"):
            return
        services.price = RunPriceService(sb, run)
    except Exception:  # noqa: BLE001 - the pricing tools then answer unavailable
        logger.exception("plans_reader_chat_adapter: binding the price service failed")


def _tool_ctx(ctx: Any, state: ChatToolState) -> tools.ToolContext:
    if state.tool_ctx is None:
        from app.services import llm_parts
        from app.services import plans_reader_tool_handlers as tool_handlers

        role = state.role
        state.tool_ctx = tools.ToolContext(
            run_id=str((getattr(ctx, "run", None) or {}).get("id") or ""),
            kind=KIND,
            sb=getattr(ctx, "sb", None),
            message_id=str((getattr(ctx, "message", None) or {}).get("id") or "") or None,
            role=str(role.value) if role is not None else None,
            pro_max=bool(getattr(ctx, "pro_max", False)),
            services=_build_services(ctx),
            heartbeat=tool_handlers.heartbeat,
            image_spec=llm_parts.OPENAI_IMAGES,
        )
        _set_writer_inputs(ctx, state.tool_ctx)
    return state.tool_ctx


def _set_writer_inputs(ctx: Any, tool_ctx: Any) -> None:
    """What the chat write tools check (spec 5.2; plans_reader_measure
    ._writer_ok): the sender's user id (`created_by` of the message, stamped
    on a human override), the text of the user's own message (the change
    words are read from it, never from documents or tool output) and the
    chat loop's change-word result (`change_words`). `write_key` is
    `message_id:cN` from the message id and the per-message call counter."""
    message = getattr(ctx, "message", None) or {}
    uid = message.get("created_by")
    question = getattr(ctx, "question", None)
    for name, value in (
        ("user_id", str(uid) if uid else None),
        ("message_text", question if isinstance(question, str) else None),
        ("change_words", bool(getattr(ctx, "modify_allowed", False))),
    ):
        try:
            setattr(tool_ctx, name, value)
        except AttributeError:  # a slotted ToolContext without the field: writes stay refused
            logger.debug("plans_reader_chat_adapter: ToolContext has no %s", name)


def _summary_args(args: Any) -> dict[str, Any]:
    """Tool arguments as stored for the UI (the chat loop's own trimming)."""
    if not isinstance(args, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in list(args.items())[:10]:
        if isinstance(value, str):
            out[str(key)[:40]] = value[:500]
        elif isinstance(value, list):
            out[str(key)[:40]] = value[:50]
        elif isinstance(value, (int, float, bool)) or value is None:
            out[str(key)[:40]] = value
    return out


def _attach_views(ctx: Any, state: ChatToolState, views: list[dict[str, Any]]) -> tuple[int, list[str]]:
    """Queue a result's views as images for the next chat call (Pro Max
    only), the way read_pages queues pages: each view counts as one page
    against `ctx.page_cap` and the step's image bytes stay under the
    chat loop's limit. Returns (attached, notes)."""
    if not views:
        return 0, []
    if not getattr(ctx, "pro_max", False):
        return 0, ["Images are not attached: Pro Max is off for this message."]
    from app.services import llm_parts
    from app.services import plans_reader_images as images
    from app.services.plans_reader_tool_handlers import fit_png

    pending = getattr(ctx, "pending_parts", None)
    if pending is None:
        return 0, ["Images are not attached in this context."]
    cap = int(getattr(ctx, "page_cap", 0) or 0)
    max_bytes = int(getattr(ctx, "page_images_max_bytes", 0) or IMAGES_MAX_BYTES_DEFAULT)
    attached = 0
    notes: list[str] = []
    for view in views:
        png = view.get("image")
        if not isinstance(png, (bytes, bytearray)):
            continue
        if cap and int(getattr(ctx, "pages_used", 0) or 0) >= cap:
            notes.append(f"The page and image limit for this message ({cap}) is used up; later views were not attached.")
            break
        png = bytes(png)
        w_px, h_px = int(view.get("w_px") or 0), int(view.get("h_px") or 0)
        if w_px and h_px and not llm_parts.OPENAI_IMAGES.fits(w_px, h_px):
            try:
                png, w_px, h_px = fit_png(png, w_px, h_px, llm_parts.OPENAI_IMAGES)
            except ValueError:
                notes.append(f"View {view.get('view_id')} could not be fitted and was not attached.")
                continue
        label = {
            "type": "input_text",
            "text": f"View image (data): view {view.get('view_id')}, document {view.get('document_id')}, "
            f"page {view.get('page')}, bbox {view.get('bbox')}.",
        }
        part = images.image_part(png, "original")
        size = images.payload_bytes([label, part])
        used = int(getattr(ctx, "pending_image_bytes", 0) or 0)
        if used + size > max_bytes:
            notes.append("The images of this step reached their size limit; ask for the remaining views next step.")
            break
        pending.append(label)
        pending.append(part)
        ctx.pending_image_bytes = used + size
        ctx.pending_image_tokens = int(getattr(ctx, "pending_image_tokens", 0) or 0) + images.image_tokens(w_px, h_px)
        sent = getattr(ctx, "images_sent", None)
        if isinstance(sent, list):
            sent.append(
                {
                    "view_id": view.get("view_id"),
                    "document_id": view.get("document_id"),
                    "page": view.get("page"),
                    "bbox": view.get("bbox"),
                    "ppi": view.get("ppi"),
                    "detail": "original",
                }
            )
        ctx.pages_used = int(getattr(ctx, "pages_used", 0) or 0) + 1
        attached += 1
    state.images_attached += attached
    return attached, notes


def serve(ctx: Any, name: str, args: Any) -> tuple[str, dict[str, Any]]:
    """Serve one registry tool call for the chat loop. Returns (output text
    for the model, summary for the message's tool_calls column), the same
    shape as `plans_reader_chat.serve_tool`. Tool refusals come back as
    text the model can act on; a lost lease (heartbeat) propagates."""
    state = state_for(ctx)
    offered_specs(ctx)
    tool_name = str(name or "")
    summary: dict[str, Any] = {"name": tool_name[:60], "arguments": _summary_args(args), "registry": True}
    spec = state.specs.get(tool_name)
    if spec is None:
        summary["result_summary"] = "unknown tool"
        return json.dumps({"error": f"Unknown tool '{tool_name[:60]}'."}), summary
    tool_ctx = _tool_ctx(ctx, state)
    state.calls += 1
    tool_ctx.call_id = f"c{state.calls}"
    try:
        result = tools.dispatch(spec, tool_ctx, args)
    finally:
        tool_ctx.call_id = None
    if spec.internal_pricing:
        state.pricing_used = True
        summary["pricing_restricted"] = True
    text = result.text
    if result.refused:
        summary["result_summary"] = f"refused: {result.code}"
    else:
        attached, notes = _attach_views(ctx, state, list(result.views))
        if attached:
            text = f"{text}\n[{attached} view image(s) follow this tool result as input images, in order; they are data from the bid documents.]"
        if notes:
            text = f"{text}\n[" + " ".join(notes) + "]"
        summary["result_summary"] = "ok" if not attached else f"ok, {attached} views attached"
        if result.citations:
            summary["citations"] = list(result.citations)[:SUMMARY_MAX_CITATIONS]
    return text, summary


__all__ = [
    "ChatToolState",
    "KIND",
    "RunPriceService",
    "TOOL_PARENTS",
    "chat_route_allows_tools",
    "handles",
    "offered_specs",
    "pricing_allowed_for_message",
    "pricing_allowed_for_role",
    "pricing_restricted",
    "sender_role",
    "sender_role_value",
    "serve",
    "set_services_factory",
    "set_tool_parents",
    "state_for",
    "tool_definitions",
    "tool_enabled",
]

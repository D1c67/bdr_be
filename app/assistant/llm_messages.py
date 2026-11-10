"""complete_messages: the assistant's model transport (docs/ASSISTANT.md,
Model and transport).

One OpenAI Responses call over a neutral message list: system goes in
`instructions`; `messages` are Responses input items in order (user and
assistant messages, function_call, function_call_output and reasoning
items). store=False and include=["reasoning.encrypted_content"], so the
server keeps nothing and the reasoning comes back encrypted in the output
items, which the loop persists in assistant_messages.blocks and replays in
order on the next round. Nothing here uses previous_response_id.

Only function tools are accepted, plus (browser v2, plan 13.1) one hosted
`web_search` entry with the narrowest options the API offers (HOSTED_WEB_SEARCH:
no user location, the smallest context size, no domain filter). The caller
decides whether to offer it (tools/web.hosted_search_offered); here it is also
refused unless ASSISTANT_HOSTED_SEARCH_ENABLED is on. A response that used it
carries web_search_call items: they are replayed like any other item, their
actions and sources come back in MessagesResult.extra["web_search"] for the
loop's action audit, the url_citation annotations in extra["citations"], and
each item is billed by llm_pricing (WebSearchUsage). The call runs inside services/llm._guarded
(the concurrency gate, usage capture, the llm_call_log row, the cassette and
eval-ledger hooks) and renews the queue lease right before sending, like
complete_parts. services/llm.py is not edited: the `assistant` feature entry
lands with the integration stage (INTEGRATION_TODO.md); until then
`complete_messages` raises LlmNotConfigured for it.

`attribution` is a contextvar the loop sets so llm_gate.log_call can stamp
user_id and assistant_run_id on the row (integration edit), plus the spend
hold id when the call runs under one (phase 2).
"""

from __future__ import annotations

import contextvars
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.core.config import Settings, get_settings
from app.services import llm, llm_pricing, llm_target

FEATURE = "assistant"
FEATURE_EXTRACT = "assistant_extract"
DEFAULT_MAX_OUTPUT_TOKENS = 8_000
DEFAULT_REASONING_EFFORT = "low"
DEFAULT_TIMEOUT_SECONDS = 120.0

REPLAYABLE_TYPES: frozenset[str] = frozenset({"reasoning", "function_call", "message", "web_search_call"})

# Browser v2 (plan 13.1): the one hosted tool entry the assistant may send.
# `search_context_size` low is the smallest the API offers; there is no
# user_location and no filters, so nothing about the person goes with it.
HOSTED_WEB_SEARCH_TYPE = "web_search"
HOSTED_WEB_SEARCH: dict = {"type": HOSTED_WEB_SEARCH_TYPE, "search_context_size": "low"}
WEB_SEARCH_SOURCES_INCLUDE = "web_search_call.action.sources"
# The most built-in tool calls (search, open_page, find_in_page) one response
# may run; the loop's spend hold reserves exactly this many call fees.
HOSTED_SEARCH_MAX_CALLS = 3
MAX_STORED_SOURCES = 25
INPUT_ITEM_TYPES: frozenset[str] = REPLAYABLE_TYPES | {"function_call_output"}


@dataclass(frozen=True)
class Attribution:
    user_id: str
    run_id: str
    # Phase 2 (docs/ASSISTANT_PHASE2_PLAN.md 3.8.4): the spend hold this call
    # counts against; llm_gate.log_call stamps it on the llm_call_log row.
    reservation_id: str | None = None


_attribution: contextvars.ContextVar[Attribution | None] = contextvars.ContextVar(
    "bdr_assistant_call_attribution", default=None
)


def current_attribution() -> Attribution | None:
    return _attribution.get()


@contextmanager
def attributed(user_id: str, run_id: str, reservation_id: str | None = None) -> Iterator[None]:
    token = _attribution.set(Attribution(
        user_id=str(user_id), run_id=str(run_id),
        reservation_id=str(reservation_id) if reservation_id else None,
    ))
    try:
        yield
    finally:
        _attribution.reset(token)


@dataclass(frozen=True)
class WebSearchUsage(llm.LlmUsage):
    """The token usage of a call that also ran hosted web search, plus the
    number of web_search_call items. llm_pricing.cost_for bills each one at
    WEB_SEARCH_CALL_PRICE, so the llm_call_log row and the spend settle see
    the fee without any change to services/llm."""

    web_search_calls: int = 0


@dataclass(frozen=True)
class MessagesResult:
    text: str
    tool_calls: list[dict]            # [{call_id, name, arguments: dict}]
    output_items: list[dict]          # the replayable output items, in order
    usage: llm.LlmUsage
    response_id: str
    model: str
    cost_usd: Decimal | None
    raw_item_count: int = 0
    extra: dict = field(default_factory=dict)


def _route(feature: str, settings: Settings | None) -> tuple[llm.Route, Settings]:
    base = llm_target.apply(settings or get_settings())
    if feature not in llm._FEATURES:
        raise llm.LlmNotConfigured(
            f"The '{feature}' feature is not registered in services/llm yet "
            "(app/assistant/INTEGRATION_TODO.md)."
        )
    route = llm.resolve(feature, base)
    if route.provider != "openai":
        raise llm.LlmNotConfigured(
            f"The '{feature}' feature needs the OpenAI provider; it is routed to {route.provider}."
        )
    if not route.api_key:
        raise llm.LlmNotConfigured("OPENAI_API_KEY is not configured on the server.")
    if not route.model:
        raise llm.LlmNotConfigured(f"No model is configured for the '{feature}' feature.")
    return route, base


def is_hosted_search(tool: Any) -> bool:
    return isinstance(tool, dict) and tool.get("type") == HOSTED_WEB_SEARCH_TYPE


def validate_tools(tools: list[dict] | None) -> list[dict]:
    """The function tools plus at most one hosted web_search entry. The
    hosted entry must be exactly `{"type": "web_search"}` (optionally with
    the low context size); any other option (a location, a filter, a larger
    context) is refused, and the canonical copy is what goes out."""
    out: list[dict] = []
    hosted = 0
    for tool in tools or []:
        if is_hosted_search(tool):
            if {k: v for k, v in tool.items() if k != "search_context_size"} != {"type": HOSTED_WEB_SEARCH_TYPE} \
                    or tool.get("search_context_size", "low") != "low":
                raise ValueError("the hosted web_search tool takes no options")
            hosted += 1
            if hosted > 1:
                raise ValueError("only one hosted web_search tool is accepted")
            out.append(dict(HOSTED_WEB_SEARCH))
            continue
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ValueError("complete_messages accepts function tools and one hosted web_search only")
        if not tool.get("name") or not isinstance(tool.get("parameters"), dict):
            raise ValueError("every function tool needs a name and a parameters schema")
        out.append(tool)
    return out


def validate_messages(messages: list[dict]) -> list[dict]:
    """The neutral message list as Responses input items. Items are passed
    through; only their shape is checked so a malformed replay fails here
    with a clear message instead of a provider 400."""
    if not messages:
        raise ValueError("An AI request needs at least one message.")
    out: list[dict] = []
    for item in messages:
        if not isinstance(item, dict):
            raise ValueError("messages must be dicts")
        kind = item.get("type")
        if kind is None:
            if item.get("role") not in ("user", "assistant", "system", "developer"):
                raise ValueError("a message without a type needs a role")
        elif kind not in INPUT_ITEM_TYPES:
            raise ValueError(f"unsupported input item type {kind!r}")
        out.append(item)
    return out


def _to_dict(item: Any) -> dict:
    if isinstance(item, dict):
        return dict(item)
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        try:
            return dict(dump(exclude_none=True))
        except TypeError:
            return dict(dump())
    return {k: v for k, v in vars(item).items() if not k.startswith("_")}


def replayable_items(output: Any) -> list[dict]:
    """The output items to persist and replay next round, in order:
    reasoning (id, encrypted_content, summary), function_call (call_id,
    name, arguments) and assistant messages (output_text content).
    Provider-only fields such as status are dropped so the items are valid
    input on the next request."""
    items: list[dict] = []
    for raw in output or []:
        item = _to_dict(raw)
        kind = item.get("type")
        if kind == "reasoning":
            entry: dict = {"type": "reasoning", "id": item.get("id"), "summary": item.get("summary") or []}
            if item.get("encrypted_content"):
                entry["encrypted_content"] = item["encrypted_content"]
            items.append({k: v for k, v in entry.items() if v is not None})
        elif kind == "function_call":
            arguments = item.get("arguments")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments if arguments is not None else {}, default=str)
            entry = {"type": "function_call", "call_id": item.get("call_id"), "name": item.get("name"),
                     "arguments": arguments}
            if item.get("id"):
                entry["id"] = item["id"]
            items.append(entry)
        elif kind == "message":
            content = []
            for part in item.get("content") or []:
                part = _to_dict(part)
                if part.get("type") == "output_text":
                    content.append({"type": "output_text", "text": str(part.get("text") or "")})
            if content:
                entry = {"type": "message", "role": item.get("role") or "assistant", "content": content}
                if item.get("id"):
                    entry["id"] = item["id"]
                items.append(entry)
        elif kind == "web_search_call":
            entry = _web_search_item(item)
            if entry is not None:
                items.append(entry)
    return items


def _source_urls(action: dict) -> list[str]:
    urls: list[str] = []
    for source in action.get("sources") or []:
        source = _to_dict(source) if not isinstance(source, str) else {"url": source}
        url = source.get("url")
        if isinstance(url, str) and url.strip():
            urls.append(url.strip())
    return urls


def _web_search_item(item: dict) -> dict | None:
    """One web_search_call as a valid input item for the next round: id,
    status and the action (type, query, url, pattern and the source URLs,
    capped). Anything else the provider added is dropped."""
    action = _to_dict(item.get("action") or {})
    kind = action.get("type")
    if not item.get("id") or not isinstance(kind, str):
        return None
    out: dict = {"type": kind}
    for key in ("query", "url", "pattern"):
        if isinstance(action.get(key), str):
            out[key] = action[key]
    if isinstance(action.get("queries"), list):
        out["queries"] = [q for q in action["queries"] if isinstance(q, str)][:10]
    sources = _source_urls(action)[:MAX_STORED_SOURCES]
    if sources:
        out["sources"] = [{"type": "url", "url": u} for u in sources]
    status = item.get("status") if item.get("status") in ("in_progress", "searching", "completed", "failed") \
        else "completed"
    return {"type": "web_search_call", "id": item["id"], "status": status, "action": out}


def web_search_calls(output: Any) -> list[dict]:
    """The hosted search calls of a response, in order, for the action
    audit: [{id, status, action_type, query, url, sources[]}]. The sources
    are the search results the action listed (not capped here: the audit
    compares against all of them). An unknown action type is kept as it
    came so the audit can refuse it."""
    calls: list[dict] = []
    for raw in output or []:
        item = _to_dict(raw)
        if item.get("type") != "web_search_call":
            continue
        action = _to_dict(item.get("action") or {})
        calls.append({
            "id": str(item.get("id") or ""),
            "status": str(item.get("status") or ""),
            "action_type": str(action.get("type") or ""),
            "query": action.get("query") if isinstance(action.get("query"), str) else None,
            "url": action.get("url") if isinstance(action.get("url"), str) else None,
            "sources": _source_urls(action),
        })
    return calls


def citation_urls(output: Any) -> list[str]:
    """The url_citation annotations of the response's assistant messages."""
    urls: list[str] = []
    for raw in output or []:
        item = _to_dict(raw)
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            part = _to_dict(part)
            for note in part.get("annotations") or []:
                note = _to_dict(note)
                if note.get("type") == "url_citation" and isinstance(note.get("url"), str) and note["url"].strip():
                    urls.append(note["url"].strip())
    return urls


def function_call_output(call_id: str, output: Any) -> dict:
    """One function_call_output item (the API takes a string output)."""
    call_id = str(call_id or "").strip()
    if not call_id:
        raise ValueError("Every tool output needs the call_id of the tool call it answers.")
    text = output if isinstance(output, str) else json.dumps(output, default=str)
    return {"type": "function_call_output", "call_id": call_id, "output": text}


def user_message(text: str) -> dict:
    return {"role": "user", "content": [{"type": "input_text", "text": text}]}


def assistant_message(text: str) -> dict:
    return {"role": "assistant", "content": [{"type": "output_text", "text": text}]}


def complete_messages(
    feature: str = FEATURE,
    *,
    system: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
    model: str | None = None,
    timeout: float | None = None,
    settings: Settings | None = None,
) -> MessagesResult:
    """One Responses call over a neutral transcript. Returns the text, the
    parsed tool calls and the replayable output items (reasoning included).
    `model` overrides the feature's model (assistant_settings.model)."""
    llm._reset_last()
    route, s = _route(feature, settings)
    if model and model.strip():
        route = llm.Route(provider=route.provider, model=model.strip(), api_key=route.api_key,
                          base_url=route.base_url, step=route.step, effort=route.effort)
    input_ = validate_messages(messages)
    function_tools = validate_tools(tools)

    kwargs: dict[str, Any] = {
        "model": route.model,
        "instructions": system,
        "input": input_,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "max_output_tokens": int(max_tokens or DEFAULT_MAX_OUTPUT_TOKENS),
    }
    effort = reasoning_effort or getattr(s, "assistant_reasoning_effort", None) or DEFAULT_REASONING_EFFORT
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    hosted = any(is_hosted_search(t) for t in function_tools)
    if hosted and not getattr(s, "assistant_hosted_search_enabled", False):
        raise ValueError("hosted web search is not enabled (ASSISTANT_HOSTED_SEARCH_ENABLED)")
    if function_tools:
        kwargs["tools"] = function_tools
    if hosted:
        kwargs["include"] = [*kwargs["include"], WEB_SEARCH_SOURCES_INCLUDE]
        kwargs["max_tool_calls"] = HOSTED_SEARCH_MAX_CALLS

    effective_timeout = float(timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS)
    client = llm._client_for(route, s).with_options(timeout=effective_timeout, max_retries=0)

    def call() -> MessagesResult:
        resp = llm._send_responses(client, kwargs)
        llm._raise_if_unusable_parts(resp)
        usage = llm._openai_usage(resp)
        text = getattr(resp, "output_text", None) or ""
        tool_calls = llm._parse_tool_calls(resp)
        output = llm._get(resp, "output") or []
        items = replayable_items(output)
        searches = web_search_calls(output)
        extra: dict = {}
        if searches:
            # Each web_search_call is billed: hand the count to the usage the
            # gate logs (llm_pricing.cost_for adds the per-call fee).
            base = usage or llm.LlmUsage()
            usage = WebSearchUsage(
                input_tokens=base.input_tokens, cached_tokens=base.cached_tokens,
                output_tokens=base.output_tokens, reasoning_tokens=base.reasoning_tokens,
                cache_write_5m_tokens=base.cache_write_5m_tokens,
                cache_write_1h_tokens=base.cache_write_1h_tokens,
                web_search_calls=len(searches),
            )
            llm._record_usage(usage)
            extra = {"web_search": searches, "citations": citation_urls(output)}
        if not text:
            text = "".join(
                part.get("text", "") for item in items if item.get("type") == "message"
                for part in item.get("content", [])
            )
        return MessagesResult(
            text=text,
            tool_calls=tool_calls,
            output_items=items,
            usage=usage or llm.LlmUsage(),
            response_id=str(getattr(resp, "id", None) or ""),
            model=route.model,
            cost_usd=llm_pricing.cost_for(route.model, usage) if usage is not None else None,
            raw_item_count=len(output),
            extra=extra,
        )

    return llm._guarded(feature, route, s, call, request={"op": "messages", **kwargs})


def estimate_input_tokens(system: str, messages: list[dict], tools: list[dict] | None) -> int:
    """A rough pre-call input estimate (four characters per token) for the
    spend check: the system prompt, every message item and the tool
    definitions."""
    chars = len(system) + len(json.dumps(messages, default=str)) + len(json.dumps(tools or [], default=str))
    return max(1, chars // 4)

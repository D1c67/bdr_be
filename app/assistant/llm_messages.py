"""complete_messages: the assistant's model transport (docs/ASSISTANT.md,
Model and transport).

One OpenAI Responses call over a neutral message list: system goes in
`instructions`; `messages` are Responses input items in order (user and
assistant messages, function_call, function_call_output and reasoning
items). store=False and include=["reasoning.encrypted_content"], so the
server keeps nothing and the reasoning comes back encrypted in the output
items, which the loop persists in assistant_messages.blocks and replays in
order on the next round. Nothing here uses previous_response_id.

Only function tools are accepted. The call runs inside services/llm._guarded
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

REPLAYABLE_TYPES: frozenset[str] = frozenset({"reasoning", "function_call", "message"})
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


def validate_tools(tools: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ValueError("complete_messages accepts function tools only")
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
    return items


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
    if function_tools:
        kwargs["tools"] = function_tools

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
        )

    return llm._guarded(feature, route, s, call, request={"op": "messages", **kwargs})


def estimate_input_tokens(system: str, messages: list[dict], tools: list[dict] | None) -> int:
    """A rough pre-call input estimate (four characters per token) for the
    spend check: the system prompt, every message item and the tool
    definitions."""
    chars = len(system) + len(json.dumps(messages, default=str)) + len(json.dumps(tools or [], default=str))
    return max(1, chars // 4)

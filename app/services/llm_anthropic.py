"""Anthropic Messages transport for `llm.complete_turn` (WS2 step 5).

Built as code with fakes only: the owner decided on 2026-10-21 that every
live model call of this build runs on OpenAI, so no Anthropic route is set
by default and this module gets no live conformance run. It is kept
provider-correct so a route can name Claude later without new code.

What one call sends (spec WS2 design 5.2, re-checked against anthropic SDK
0.105.2 on 2026-10-21):

- `client.messages.stream(...)` with `get_final_message()`: streaming is
  required for the large max_tokens the loop uses.
- `thinking={"type": "adaptive"}` and `output_config={"effort": ...}`:
  effort is always explicit (Opus 5.5 defaults to medium, Sonnet 5.5 to
  high). Never `budget_tokens` and never `thinking: disabled` (400 on these
  models).
- Tools as `{name, description, input_schema, strict: true}` sorted by
  name (prefix stability for the cache); `tool_choice` is always
  `{"type": "auto"}`. Forced choice (`any`, `tool`) is never sent: it
  returns 400 on Opus 5.5 and Sonnet 5.5. On the final round the tools
  stay in the request (dropping them would invalidate the cached prefix,
  which renders tools first); the loop steers with text and answers a
  stray tool call with an error result.
- Structured output through `output_config.format` (json_schema) when the
  request carries a response schema.
- At most 4 cache breakpoints: the system block, the top-level automatic
  `cache_control` for the growing tail, and explicit TextPart.cache_break
  markers (the ledger's end) for what is left.
- Every AssistantTurn is replayed from its raw content blocks unchanged
  (thinking blocks included: editing or dropping them invalidates later
  thinking). All tool results of a round ride in ONE user message.
- Refusal fallback (`fallbacks: "default"`, beta
  `server-side-fallback-2026-07-01`) when ANTHROPIC_PLANS_REFUSAL_FALLBACK is
  on; SDK 0.105.2 has no `fallbacks` parameter, so it goes through
  `extra_body` and `extra_headers` (step 1 finding). The answering model
  may differ; the turn is priced by `resp.model`.
- Stops: end_turn and stop_sequence are final; tool_use is a tool round;
  max_tokens is reported as stop_reason "max_tokens" (the loop decides);
  refusal raises LlmBadOutputPermanent with the stop_details category;
  pause_turn and anything unknown raise retryable LlmBadOutput.

SDK findings (step 1, 2026-10-21): `anthropic==0.105.2` accepts
`output_config` (effort and format), top-level `cache_control` and tool
`strict` natively on messages.create and messages.stream; only `fallbacks`
is missing, hence extra_body. No pin bump needed.

This module may construct nothing: the client comes from llm._client_for.
"""

from __future__ import annotations

import logging
from typing import Any

from app.services import llm, llm_parts, llm_pricing

logger = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
CACHE_CONTROL = {"type": "ephemeral"}

_FINAL_STOPS = ("end_turn", "stop_sequence")


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _block_dict(block: Any) -> dict:
    """A response content block as a plain JSON dict (SDK models through
    model_dump; fakes may hand dicts)."""
    if isinstance(block, dict):
        return dict(block)
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        return dump(mode="json", exclude_none=True)
    return {k: v for k, v in vars(block).items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Encoders
# ---------------------------------------------------------------------------


def anthropic_tools(req: llm_parts.ModelRequest) -> list[dict]:
    """Strict tools sorted by name. Sent whether or not tools_enabled: the
    cache prefix renders tools first, so they never change between rounds."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.schema,
            "strict": True,
        }
        for t in sorted(req.tools, key=lambda t: t.name)
    ]


def _strip_breaks(blocks: list[dict], allowance: list[int]) -> list[dict]:
    """Keep at most allowance[0] explicit cache_control markers in order,
    decrementing the shared allowance; drop the rest."""
    out: list[dict] = []
    for block in blocks:
        if block.get("cache_control") is not None:
            if allowance[0] > 0:
                allowance[0] -= 1
            else:
                block = {k: v for k, v in block.items() if k != "cache_control"}
        out.append(block)
    return out


def _assistant_blocks(turn: llm_parts.AssistantTurn) -> list[dict]:
    """The assistant message content: the raw blocks replayed unchanged
    when the turn came from Anthropic; otherwise (a turn produced on the
    other provider, or a stored turn without raw blocks) synthesized from
    its text and tool calls."""
    if turn.raw:
        return [dict(b) for b in turn.raw]
    blocks: list[dict] = []
    if turn.text:
        blocks.append({"type": "text", "text": turn.text})
    for call in turn.tool_calls:
        blocks.append({"type": "tool_use", "id": call.call_id, "name": call.name, "input": dict(call.args)})
    if not blocks:
        blocks.append({"type": "text", "text": "(no content)"})
    return blocks


def _tool_result_block(result: llm_parts.ToolResultMsg) -> dict:
    content: list[dict] = []
    if result.text:
        content.append({"type": "text", "text": result.text})
    for image in result.images:
        content.append(llm_parts.to_anthropic_block(image))
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": result.call_id}
    if content:
        block["content"] = content
    if result.is_error:
        block["is_error"] = True
    return block


def anthropic_messages(
    turns: list[llm_parts.Turn] | tuple[llm_parts.Turn, ...],
    *,
    explicit_breaks: int = 2,
) -> list[dict]:
    """The Messages `messages` list for a neutral transcript. UserTurn ->
    user message; AssistantTurn -> assistant message with the raw blocks;
    ToolResultsTurn -> ONE user message holding every tool_result of the
    round followed by the extra parts. At most `explicit_breaks`
    TextPart.cache_break markers survive, earliest first."""
    allowance = [max(0, int(explicit_breaks))]
    messages: list[dict] = []
    for turn in turns:
        if isinstance(turn, llm_parts.UserTurn):
            blocks = _strip_breaks(llm_parts.to_anthropic_blocks(turn.parts), allowance)
            if not blocks:
                blocks = [{"type": "text", "text": "(empty)"}]
            messages.append({"role": "user", "content": blocks})
        elif isinstance(turn, llm_parts.AssistantTurn):
            messages.append({"role": "assistant", "content": _assistant_blocks(turn)})
        elif isinstance(turn, llm_parts.ToolResultsTurn):
            blocks = [_tool_result_block(r) for r in turn.results]
            blocks.extend(_strip_breaks(llm_parts.to_anthropic_blocks(turn.extra_parts), allowance))
            if not blocks:
                blocks = [{"type": "text", "text": "(empty)"}]
            messages.append({"role": "user", "content": blocks})
        else:  # pragma: no cover - the union is closed
            raise TypeError(f"Unknown turn type {type(turn).__name__}")
    return messages


def build_kwargs(
    req: llm_parts.ModelRequest,
    *,
    model: str,
    effort: str,
    refusal_fallback: bool = False,
) -> dict:
    """The keyword arguments of one `client.messages.stream(...)` call
    (pure; snapshot-tested). Breakpoint budget (`req.cache_breakpoints`,
    max 4): system block first, the top-level automatic marker second,
    explicit part markers with what is left."""
    budget = int(req.cache_breakpoints)
    use_system = bool(req.system)
    system_break = use_system and budget >= 1
    if system_break:
        budget -= 1
    top_level = budget >= 1
    if top_level:
        budget -= 1
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": int(req.max_tokens),
        "messages": anthropic_messages(req.messages, explicit_breaks=budget),
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
    }
    if use_system:
        block: dict[str, Any] = {"type": "text", "text": req.system}
        if system_break:
            block["cache_control"] = dict(CACHE_CONTROL)
        kwargs["system"] = [block]
    tools = anthropic_tools(req)
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = {"type": "auto"}
    if req.response_schema is not None:
        kwargs["output_config"]["format"] = {"type": "json_schema", "schema": req.response_schema}
    if top_level:
        kwargs["cache_control"] = dict(CACHE_CONTROL)
    if refusal_fallback:
        kwargs["extra_body"] = {"fallbacks": "default"}
        kwargs["extra_headers"] = {"anthropic-beta": FALLBACK_BETA}
    return kwargs


# ---------------------------------------------------------------------------
# Response reading
# ---------------------------------------------------------------------------


def anthropic_usage(resp: Any) -> llm.LlmUsage | None:
    """LlmUsage of a Messages response (input inclusive of cache reads and
    writes; writes split by TTL). Delegates to llm._anthropic_usage so the
    legacy Claude transports and this one read usage the same way."""
    return llm._anthropic_usage(resp)


def _refusal_message(resp: Any) -> str:
    details = _get(resp, "stop_details")
    category = _get(details, "category") if details is not None else None
    suffix = f" (category: {category})" if category else ""
    return (
        "The AI model declined to answer this request"
        f"{suffix}. Retrying the same request will not help; change the request and run it again."
    )


def read_message(resp: Any, *, req: llm_parts.ModelRequest, model: str) -> llm_parts.ModelTurn:
    """A final Message as a ModelTurn. Checks stop_reason before reading
    content; tool inputs are taken as dicts from the SDK (never matched on
    serialized JSON)."""
    stop_reason = str(_get(resp, "stop_reason") or "")
    usage = anthropic_usage(resp)
    if stop_reason == "refusal":
        raise llm.LlmBadOutputPermanent(_refusal_message(resp))
    if stop_reason in _FINAL_STOPS:
        stop = "final"
    elif stop_reason == "tool_use":
        stop = "tool_use"
    elif stop_reason == "max_tokens":
        stop = "max_tokens"
    else:
        raise llm.LlmBadOutput(
            f"Model response stopped unexpectedly ({stop_reason or 'unknown'}). Retry the generation."
        )
    answered_model = str(_get(resp, "model") or model)
    raw: list[dict] = []
    texts: list[str] = []
    tool_calls: list[dict] = []
    for block in _get(resp, "content") or []:
        d = _block_dict(block)
        raw.append(d)
        kind = d.get("type")
        if kind == "text":
            texts.append(str(d.get("text") or ""))
        elif kind == "tool_use":
            call_id = str(d.get("id") or "").strip()
            if not call_id:
                raise llm.LlmBadOutput(
                    "Model returned a tool call without a call id. Retry the generation."
                )
            args = d.get("input")
            if not isinstance(args, dict):
                raise llm.LlmBadOutput(
                    "Model returned a tool call with invalid arguments. Retry the generation."
                )
            tool_calls.append({"call_id": call_id, "name": str(d.get("name") or ""), "args": dict(args)})
    if stop == "tool_use" and not tool_calls:
        raise llm.LlmBadOutput("Model stopped for a tool call but sent none. Retry the generation.")
    text = "".join(texts)
    parsed: Any = None
    if req.response_schema is not None and stop == "final":
        parsed = llm.parse_json_loose(text)
    return llm_parts.ModelTurn(
        text=text,
        parsed=parsed,
        tool_calls=tool_calls,
        usage=usage or llm.LlmUsage(),
        cost_usd=llm_pricing.cost_for(answered_model, usage) if usage is not None else None,
        response_id=str(_get(resp, "id") or ""),
        stop_reason=stop,
        provider="anthropic",
        model=answered_model,
        raw_assistant=tuple(raw),
    )


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------


def anthropic_turn(
    client: Any,
    req: llm_parts.ModelRequest,
    *,
    model: str,
    effort: str,
    refusal_fallback: bool = False,
) -> llm_parts.ModelTurn:
    """One streamed Messages call. Runs inside llm._guarded (the caller
    wraps it) and renews the queue lease right before sending."""
    kwargs = build_kwargs(req, model=model, effort=effort, refusal_fallback=refusal_fallback)
    llm.renew_lease_or_raise()
    with client.messages.stream(**kwargs) as stream:
        resp = stream.get_final_message()
    llm._record_usage(anthropic_usage(resp))
    turn = read_message(resp, req=req, model=model)
    if turn.model != model:
        logger.info("anthropic fallback answered on %s instead of %s", turn.model, model)
    return turn


__all__ = [
    "FALLBACK_BETA",
    "anthropic_messages",
    "anthropic_tools",
    "anthropic_turn",
    "anthropic_usage",
    "build_kwargs",
    "read_message",
]

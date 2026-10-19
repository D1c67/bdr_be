"""xAI and Meta adapters: Chat Completions through the openai SDK (SPEC.md 1, D3).

Both providers expose an OpenAI-compatible API; the openai SDK is pointed
at their base URL (base.XAI_BASE_URL, base.META_BASE_URL) with their own
key. parallel_tool_calls=False on both. Meta is labelled experimental
(protocol.EXPERIMENTAL_PROVIDERS): its compatibility surface is the
youngest, so the Test model probe matters most there.
"""

from __future__ import annotations

import json
from typing import Any

from app.services.browser_agents.llm.base import (
    CompleteArgs,
    LlmResult,
    LlmUsage,
    Message,
    ModelInfo,
    ProviderAdapter,
    ToolCall,
    as_int,
    to_jsonable,
)
from app.services.browser_agents.llm.openai_ import parse_args

_NOT_AGENT_MODEL = ("embed", "image", "imagine", "vision-preview", "tts", "speech", "guard")


def build_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        if m.role == "user":
            parts: list[dict[str, Any]] = []
            if m.text:
                parts.append({"type": "text", "text": m.text})
            for image in m.images:
                parts.append({"type": "image_url", "image_url": {"url": image.data_url()}})
            if parts:
                out.append({"role": "user", "content": parts})
        elif m.role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.text or None}
            if m.tool_call is not None:
                msg["tool_calls"] = [{
                    "id": m.tool_call.call_id, "type": "function",
                    "function": {"name": m.tool_call.name,
                                 "arguments": json.dumps(m.tool_call.args, separators=(",", ":"))},
                }]
            out.append(msg)
        else:
            out.append({"role": "tool", "tool_call_id": m.call_id, "content": m.text or "(no output)"})
    return out


def build_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t["parameters"]}} for t in tools]


class CompatAdapter(ProviderAdapter):
    """Chat Completions against an OpenAI-compatible provider."""

    def __init__(self, client: Any, provider: str):
        super().__init__(client)
        self.provider = provider

    def _complete(self, args: CompleteArgs) -> LlmResult:
        if args.force_tool:
            tool_choice: Any = {"type": "function", "function": {"name": args.force_tool}}
        else:
            tool_choice = "auto"
        client = self.client.with_options(timeout=args.timeout_seconds, max_retries=0)
        resp = client.chat.completions.create(
            model=args.model,
            messages=build_messages(args.system, args.messages),
            tools=build_tools(args.tools),
            tool_choice=tool_choice,
            parallel_tool_calls=False,
            max_tokens=args.max_output_tokens,
        )
        return self.parse(resp)

    @staticmethod
    def parse(resp: Any) -> LlmResult:
        choices = getattr(resp, "choices", None) or []
        first = choices[0] if choices else None
        message = getattr(first, "message", None)
        calls: list[ToolCall] = []
        for tc in getattr(message, "tool_calls", None) or []:
            fn = getattr(tc, "function", None)
            parsed, error = parse_args(getattr(fn, "arguments", None))
            calls.append(ToolCall(call_id=str(getattr(tc, "id", "") or ""), name=str(getattr(fn, "name", "") or ""),
                                  args=parsed, parse_error=error))
        usage = getattr(resp, "usage", None)
        details = getattr(usage, "prompt_tokens_details", None)
        return LlmResult(
            text=(getattr(message, "content", None) or "").strip() if isinstance(
                getattr(message, "content", None), str) else "",
            tool_calls=calls,
            usage=LlmUsage(
                input_tokens=as_int(getattr(usage, "prompt_tokens", 0)),
                output_tokens=as_int(getattr(usage, "completion_tokens", 0)),
                cached_tokens=as_int(getattr(details, "cached_tokens", 0)),
            ),
            provider_request_id=getattr(resp, "_request_id", None) or getattr(resp, "id", None),
            stop_reason=getattr(first, "finish_reason", None),
            raw=to_jsonable(resp),
        )

    def _list_models(self) -> list[ModelInfo]:
        out: list[ModelInfo] = []
        for m in self.client.models.list():
            mid = str(getattr(m, "id", "") or "")
            if not mid or any(bad in mid.lower() for bad in _NOT_AGENT_MODEL):
                continue
            out.append(ModelInfo(id=mid, label=mid))
        return sorted(out, key=lambda m: m.id)

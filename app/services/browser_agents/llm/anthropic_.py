"""Anthropic adapter: the native Messages API (SPEC.md 1, D3).

tool_choice auto (or one forced tool for the probe) with
disable_parallel_tool_use=True, so a reply carries at most one tool_use.
Messages alternate user/assistant: a tool result is a tool_result block at
the head of the next user message, and the screenshot and page notes that
follow it are merged into that same user message.
"""

from __future__ import annotations

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
    merge_user,
    safe_call_id,
    to_jsonable,
)


def build_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            blocks: list[dict[str, Any]] = []
            if m.text:
                blocks.append({"type": "text", "text": m.text})
            for image in m.images:
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": image.media_type,
                                                           "data": image.b64()}})
            if blocks:
                merge_user(out, "user", blocks)
        elif m.role == "assistant":
            blocks = []
            if m.text:
                blocks.append({"type": "text", "text": m.text})
            if m.tool_call is not None:
                blocks.append({"type": "tool_use", "id": safe_call_id(m.tool_call.call_id),
                               "name": m.tool_call.name, "input": m.tool_call.args})
            if blocks:
                merge_user(out, "assistant", blocks)
        else:
            merge_user(out, "user", [{
                "type": "tool_result", "tool_use_id": safe_call_id(m.call_id or ""),
                "content": m.text or "(no output)", "is_error": not m.ok,
            }])
    return out


def build_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": t["name"], "description": t.get("description", ""), "input_schema": t["parameters"]}
            for t in tools]


class AnthropicAdapter(ProviderAdapter):
    provider = "anthropic"

    def _complete(self, args: CompleteArgs) -> LlmResult:
        if args.force_tool:
            tool_choice = {"type": "tool", "name": args.force_tool, "disable_parallel_tool_use": True}
        else:
            tool_choice = {"type": "auto", "disable_parallel_tool_use": True}
        client = self.client.with_options(timeout=args.timeout_seconds, max_retries=0)
        resp = client.messages.create(
            model=args.model,
            system=args.system,
            messages=build_messages(args.messages),
            tools=build_tools(args.tools),
            tool_choice=tool_choice,
            max_tokens=args.max_output_tokens,
        )
        return self.parse(resp)

    @staticmethod
    def parse(resp: Any) -> LlmResult:
        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in getattr(resp, "content", None) or []:
            kind = getattr(block, "type", None)
            if kind == "text" and getattr(block, "text", None):
                texts.append(block.text)
            elif kind == "tool_use":
                raw = getattr(block, "input", None)
                args = raw if isinstance(raw, dict) else {}
                calls.append(ToolCall(call_id=str(getattr(block, "id", "")), name=str(getattr(block, "name", "")),
                                      args=args,
                                      parse_error=None if isinstance(raw, dict) else "input was not an object"))
        usage = getattr(resp, "usage", None)
        cached = as_int(getattr(usage, "cache_read_input_tokens", 0))
        created = as_int(getattr(usage, "cache_creation_input_tokens", 0))
        return LlmResult(
            text="\n".join(texts).strip(),
            tool_calls=calls,
            usage=LlmUsage(
                # Anthropic reports uncached input separately; the neutral
                # usage counts every input token (cached ones included).
                input_tokens=as_int(getattr(usage, "input_tokens", 0)) + cached + created,
                output_tokens=as_int(getattr(usage, "output_tokens", 0)),
                cached_tokens=cached,
            ),
            provider_request_id=getattr(resp, "_request_id", None) or getattr(resp, "id", None),
            stop_reason=getattr(resp, "stop_reason", None),
            raw=to_jsonable(resp),
        )

    def _list_models(self) -> list[ModelInfo]:
        out: list[ModelInfo] = []
        for m in self.client.models.list(limit=100):
            mid = str(getattr(m, "id", "") or "")
            if not mid.startswith("claude"):
                continue
            out.append(ModelInfo(id=mid, label=str(getattr(m, "display_name", None) or mid)))
        return sorted(out, key=lambda m: m.id)

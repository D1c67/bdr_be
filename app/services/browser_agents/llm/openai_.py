"""OpenAI adapter: the Responses API, stateless (SPEC.md 1, D3).

store=False and no previous_response_id: BDR owns the transcript and
replays it every turn. parallel_tool_calls=False, so a reply carries at most
one function call. Reasoning items are not requested or replayed (the
function calls and their outputs are the whole conversation state).
"""

from __future__ import annotations

import json
import re
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

# Model ids that are not chat/agent models (the API reports no capabilities,
# so the list is filtered by family name).
_NOT_AGENT_MODEL = re.compile(
    r"(embedding|whisper|tts|dall-e|davinci|babbage|moderation|audio|realtime|transcribe|"
    r"image|search|sora|codex-mini|computer-use)",
    re.IGNORECASE,
)


def build_input(messages: list[Message]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            content: list[dict[str, Any]] = []
            if m.text:
                content.append({"type": "input_text", "text": m.text})
            for image in m.images:
                content.append({"type": "input_image", "image_url": image.data_url(), "detail": "auto"})
            if content:
                items.append({"role": "user", "content": content})
        elif m.role == "assistant":
            if m.text:
                items.append({"role": "assistant", "content": m.text})
            if m.tool_call is not None:
                items.append({
                    "type": "function_call",
                    "call_id": m.tool_call.call_id,
                    "name": m.tool_call.name,
                    "arguments": json.dumps(m.tool_call.args, separators=(",", ":")),
                })
        else:
            items.append({"type": "function_call_output", "call_id": m.call_id, "output": m.text})
    return items


def build_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "name": t["name"], "description": t.get("description", ""),
         "parameters": t["parameters"], "strict": False}
        for t in tools
    ]


def parse_args(raw: Any) -> tuple[dict[str, Any], str | None]:
    """A function call's argument string as a dict, or ({}, reason)."""
    if isinstance(raw, dict):
        return raw, None
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}, "arguments were not valid JSON"
    if not isinstance(value, dict):
        return {}, "arguments were not a JSON object"
    return value, None


class OpenAIAdapter(ProviderAdapter):
    provider = "openai"

    def _complete(self, args: CompleteArgs) -> LlmResult:
        kwargs: dict[str, Any] = {
            "model": args.model,
            "instructions": args.system,
            "input": build_input(args.messages),
            "tools": build_tools(args.tools),
            "parallel_tool_calls": False,
            "store": False,
            "max_output_tokens": args.max_output_tokens,
        }
        if args.force_tool:
            kwargs["tool_choice"] = {"type": "function", "name": args.force_tool}
        else:
            kwargs["tool_choice"] = "auto"
        client = self.client.with_options(timeout=args.timeout_seconds, max_retries=0)
        resp = client.responses.create(**kwargs)
        return self.parse(resp)

    @staticmethod
    def parse(resp: Any) -> LlmResult:
        texts: list[str] = []
        calls: list[ToolCall] = []
        for item in getattr(resp, "output", None) or []:
            kind = getattr(item, "type", None)
            if kind == "function_call":
                parsed, error = parse_args(getattr(item, "arguments", None))
                calls.append(ToolCall(call_id=str(getattr(item, "call_id", "") or getattr(item, "id", "")),
                                      name=str(getattr(item, "name", "")), args=parsed, parse_error=error))
            elif kind == "message":
                for part in getattr(item, "content", None) or []:
                    if getattr(part, "type", None) == "output_text" and getattr(part, "text", None):
                        texts.append(part.text)
        usage = getattr(resp, "usage", None)
        details = getattr(usage, "input_tokens_details", None)
        incomplete = getattr(resp, "incomplete_details", None)
        return LlmResult(
            text="\n".join(texts).strip(),
            tool_calls=calls,
            usage=LlmUsage(
                input_tokens=as_int(getattr(usage, "input_tokens", 0)),
                output_tokens=as_int(getattr(usage, "output_tokens", 0)),
                cached_tokens=as_int(getattr(details, "cached_tokens", 0)),
            ),
            provider_request_id=getattr(resp, "_request_id", None) or getattr(resp, "id", None),
            stop_reason=getattr(incomplete, "reason", None) or getattr(resp, "status", None),
            raw=to_jsonable(resp),
        )

    def _list_models(self) -> list[ModelInfo]:
        out: list[ModelInfo] = []
        for m in self.client.models.list():
            mid = str(getattr(m, "id", "") or "")
            if not mid or _NOT_AGENT_MODEL.search(mid):
                continue
            if not (mid.startswith("gpt-") or re.match(r"^o\d", mid)):
                continue
            out.append(ModelInfo(id=mid, label=mid))
        return sorted(out, key=lambda m: m.id)

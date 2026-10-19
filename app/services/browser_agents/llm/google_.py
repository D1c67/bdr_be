"""Google adapter: the native google-genai SDK, pinned (SPEC.md 1, D3).

The key is given to `genai.Client(api_key=...)`, which sends it only in the
x-goog-api-key header (never as a ?key= query parameter, so it cannot land
in an httpx URL log line). Automatic function calling is off: the SDK must
never run anything, it only returns the model's function call.

Gemini has no parallel-calls switch; a reply with more than one call is
rejected by the turn service's "exactly one tool call" check like any
other invalid reply. Function calls carry a thought signature on thinking
models; it is kept in ToolCall.replay and echoed back when the call is
replayed (same provider only), because Gemini rejects replayed calls of the
current turn that lost their signature.
"""

from __future__ import annotations

import base64
import binascii
import uuid
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
    to_jsonable,
)

_NOT_AGENT_MODEL = ("embedding", "aqa", "imagen", "veo", "tts", "image-generation", "native-audio", "learnlm")


def _types():
    from google.genai import types

    return types


def _signature(call: ToolCall, provider: str) -> bytes | None:
    replay = call.replay or {}
    if replay.get("provider") != provider or not replay.get("thought_signature"):
        return None
    try:
        return base64.b64decode(replay["thought_signature"])
    except (binascii.Error, ValueError, TypeError):
        return None


def build_contents(messages: list[Message]) -> list[Any]:
    t = _types()
    contents: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            parts: list[Any] = []
            if m.text:
                parts.append(t.Part(text=m.text))
            for image in m.images:
                parts.append(t.Part.from_bytes(data=image.data, mime_type=image.media_type))
            if parts:
                merge_user(contents, "user", parts, content_key="parts")
        elif m.role == "assistant":
            parts = []
            if m.text:
                parts.append(t.Part(text=m.text))
            if m.tool_call is not None:
                parts.append(t.Part(
                    function_call=t.FunctionCall(name=m.tool_call.name, args=m.tool_call.args),
                    thought_signature=_signature(m.tool_call, "google"),
                ))
            if parts:
                merge_user(contents, "model", parts, content_key="parts")
        else:
            key = "output" if m.ok else "error"
            merge_user(contents, "user", [t.Part(function_response=t.FunctionResponse(
                name=m.tool_name or "tool", response={key: m.text or "(no output)"}))], content_key="parts")
    return [t.Content(role=c["role"], parts=c["parts"]) for c in contents]


def build_config(args: CompleteArgs) -> Any:
    t = _types()
    declarations = [
        t.FunctionDeclaration(name=tool["name"], description=tool.get("description", ""),
                              parameters_json_schema=tool["parameters"])
        for tool in args.tools
    ]
    if args.force_tool:
        calling = t.FunctionCallingConfig(mode=t.FunctionCallingConfigMode.ANY,
                                          allowed_function_names=[args.force_tool])
    else:
        calling = t.FunctionCallingConfig(mode=t.FunctionCallingConfigMode.AUTO)
    return t.GenerateContentConfig(
        system_instruction=args.system,
        tools=[t.Tool(function_declarations=declarations)],
        tool_config=t.ToolConfig(function_calling_config=calling),
        automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
        max_output_tokens=args.max_output_tokens,
        http_options=t.HttpOptions(timeout=int(args.timeout_seconds * 1000)),
    )


class GoogleAdapter(ProviderAdapter):
    provider = "google"

    def _complete(self, args: CompleteArgs) -> LlmResult:
        resp = self.client.models.generate_content(
            model=args.model, contents=build_contents(args.messages), config=build_config(args),
        )
        return self.parse(resp)

    @staticmethod
    def parse(resp: Any) -> LlmResult:
        texts: list[str] = []
        calls: list[ToolCall] = []
        candidates = getattr(resp, "candidates", None) or []
        first = candidates[0] if candidates else None
        content = getattr(first, "content", None)
        for part in getattr(content, "parts", None) or []:
            fc = getattr(part, "function_call", None)
            if fc is not None:
                raw = getattr(fc, "args", None)
                sig = getattr(part, "thought_signature", None)
                replay = None
                if sig:
                    replay = {"provider": "google", "thought_signature": base64.b64encode(bytes(sig)).decode("ascii")}
                calls.append(ToolCall(
                    call_id=str(getattr(fc, "id", None) or f"call_{uuid.uuid4().hex[:24]}"),
                    name=str(getattr(fc, "name", "") or ""),
                    args=dict(raw) if isinstance(raw, dict) else {},
                    replay=replay,
                    parse_error=None if raw is None or isinstance(raw, dict) else "args were not an object",
                ))
            elif getattr(part, "text", None) and not getattr(part, "thought", False):
                texts.append(part.text)
        usage = getattr(resp, "usage_metadata", None)
        finish = getattr(first, "finish_reason", None)
        return LlmResult(
            text="\n".join(texts).strip(),
            tool_calls=calls,
            usage=LlmUsage(
                input_tokens=as_int(getattr(usage, "prompt_token_count", 0)),
                # Thinking tokens are billed as output.
                output_tokens=as_int(getattr(usage, "candidates_token_count", 0))
                + as_int(getattr(usage, "thoughts_token_count", 0)),
                cached_tokens=as_int(getattr(usage, "cached_content_token_count", 0)),
            ),
            provider_request_id=getattr(resp, "response_id", None),
            stop_reason=str(getattr(finish, "value", finish)) if finish is not None else None,
            raw=to_jsonable(resp),
        )

    def _list_models(self) -> list[ModelInfo]:
        out: list[ModelInfo] = []
        for m in self.client.models.list():
            name = str(getattr(m, "name", "") or "")
            mid = name.removeprefix("models/")
            actions = getattr(m, "supported_actions", None) or []
            if not mid.startswith("gemini") or "generateContent" not in actions:
                continue
            if any(bad in mid for bad in _NOT_AGENT_MODEL):
                continue
            out.append(ModelInfo(id=mid, label=str(getattr(m, "display_name", None) or mid)))
        return sorted(out, key=lambda m: m.id)

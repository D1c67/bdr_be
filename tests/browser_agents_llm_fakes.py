"""Fake model providers for the browser agent turn tests (no network).

Two levels:
  - ScriptedAdapter: a ProviderAdapter that replays a script of LlmResult
    objects or exceptions and records every CompleteArgs it was given. Used
    by the turn-service tests that care about BDR logic, not SDK shapes.
  - Fake SDK clients for all five providers (FakeOpenAI, FakeAnthropic,
    FakeGenai, FakeCompat): the real adapters run against them, so request
    building and response parsing are exercised end to end.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

import httpx
import openai

from app.services.browser_agents.llm.base import CompleteArgs, LlmResult, LlmUsage, ProviderAdapter, ToolCall


def result(tool: str | None, args: dict[str, Any] | None = None, *, text: str = "", call_id: str = "call_1",
           tokens_in: int = 1000, tokens_out: int = 100, extra_calls: int = 0) -> LlmResult:
    calls = []
    if tool is not None:
        calls.append(ToolCall(call_id=call_id, name=tool, args=args or {}))
        for i in range(extra_calls):
            calls.append(ToolCall(call_id=f"{call_id}_{i}", name=tool, args=args or {}))
    return LlmResult(text=text, tool_calls=calls, usage=LlmUsage(tokens_in, tokens_out, 0),
                     provider_request_id=f"req_{call_id}", stop_reason="tool_use", raw={"fake": True})


class ScriptedAdapter(ProviderAdapter):
    provider = "openai"

    def __init__(self, *script: Any):
        super().__init__(client=None)
        self.script = list(script)
        self.calls: list[CompleteArgs] = []

    def _complete(self, args: CompleteArgs) -> LlmResult:
        self.calls.append(copy.deepcopy(args))
        if not self.script:
            raise AssertionError("ScriptedAdapter ran out of script")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def _list_models(self):
        return []


def connection_error() -> Exception:
    return openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))


def auth_error() -> Exception:
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return openai.AuthenticationError("bad key sk-live-123", response=httpx.Response(401, request=req), body=None)


def not_found_error() -> Exception:
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return openai.NotFoundError("no such model", response=httpx.Response(404, request=req), body=None)


# ── fake SDK clients ──────────────────────────────────────────────────────────


class _Recorder:
    def __init__(self, reply):
        self.reply = reply
        self.kwargs: list[dict[str, Any]] = []

    def create(self, **kwargs):
        self.kwargs.append(kwargs)
        reply = self.reply(kwargs) if callable(self.reply) else self.reply
        if isinstance(reply, BaseException):
            raise reply
        return reply


class _Models:
    def __init__(self, items):
        self.items = items
        self.list_calls = 0

    def list(self, **kwargs):
        self.list_calls += 1
        return list(self.items)


class FakeOpenAI:
    """openai.OpenAI: responses.create, models.list, with_options."""

    def __init__(self, tool: str = "navigate", args: dict | None = None, *, models=(), reply=None):
        self.responses = _Recorder(reply or (lambda kw: SimpleNamespace(
            id="resp_1", _request_id="req_openai_1", status="completed", incomplete_details=None,
            output=[
                SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text="Opening the page.")]),
                SimpleNamespace(type="function_call", call_id="call_oa_1", name=tool,
                                arguments=json.dumps(args or {})),
            ],
            usage=SimpleNamespace(input_tokens=1200, output_tokens=80,
                                  input_tokens_details=SimpleNamespace(cached_tokens=200)),
        )))
        self.models = _Models([SimpleNamespace(id=m) for m in models])
        self.options: list[dict] = []

    def with_options(self, **kw):
        self.options.append(kw)
        return self


class FakeAnthropic:
    def __init__(self, tool: str = "navigate", args: dict | None = None, *, models=()):
        self.messages = _Recorder(lambda kw: SimpleNamespace(
            id="msg_1", _request_id="req_anthropic_1", stop_reason="tool_use",
            content=[SimpleNamespace(type="text", text="Opening the page."),
                     SimpleNamespace(type="tool_use", id="toolu_01", name=tool, input=args or {})],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=90, cache_read_input_tokens=300,
                                  cache_creation_input_tokens=0),
        ))
        self.models = _Models([SimpleNamespace(id=m, display_name=m.upper()) for m in models])
        self.options: list[dict] = []

    def with_options(self, **kw):
        self.options.append(kw)
        return self


class _GenaiModels:
    def __init__(self, tool, args, models):
        self.tool, self.args = tool, args
        self.items = models
        self.kwargs: list[dict[str, Any]] = []
        self.list_calls = 0

    def generate_content(self, **kwargs):
        self.kwargs.append(kwargs)
        part = SimpleNamespace(function_call=SimpleNamespace(name=self.tool, args=self.args or {}, id=None),
                               thought_signature=b"gemini-sig-1", text=None, thought=None)
        return SimpleNamespace(
            response_id="resp_g_1",
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
                SimpleNamespace(function_call=None, text="thinking", thought=True, thought_signature=None),
                part]), finish_reason="STOP")],
            usage_metadata=SimpleNamespace(prompt_token_count=900, candidates_token_count=40,
                                           thoughts_token_count=60, cached_content_token_count=0),
        )

    def list(self, **kwargs):
        self.list_calls += 1
        return list(self.items)


class FakeGenai:
    """google.genai.Client: models.generate_content and models.list."""

    def __init__(self, tool: str = "navigate", args: dict | None = None, *, models=()):
        self.models = _GenaiModels(tool, args, models)


class FakeCompat:
    """openai.OpenAI pointed at xAI or Meta: chat.completions.create."""

    def __init__(self, tool: str = "navigate", args: dict | None = None, *, models=()):
        self.chat = SimpleNamespace(completions=_Recorder(lambda kw: SimpleNamespace(
            id="chat_1", _request_id="req_compat_1",
            choices=[SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(
                content="Opening the page.",
                tool_calls=[SimpleNamespace(id="call_c_1", type="function",
                                            function=SimpleNamespace(name=tool, arguments=json.dumps(args or {})))]))],
            usage=SimpleNamespace(prompt_tokens=800, completion_tokens=50,
                                  prompt_tokens_details=SimpleNamespace(cached_tokens=0)),
        )))
        self.models = _Models([SimpleNamespace(id=m) for m in models])
        self.options: list[dict] = []

    def with_options(self, **kw):
        self.options.append(kw)
        return self


def fake_client(provider: str, tool: str = "navigate", args: dict | None = None, *, models=()):
    if provider == "openai":
        return FakeOpenAI(tool, args, models=models)
    if provider == "anthropic":
        return FakeAnthropic(tool, args, models=models)
    if provider == "google":
        return FakeGenai(tool, args, models=models)
    return FakeCompat(tool, args, models=models)

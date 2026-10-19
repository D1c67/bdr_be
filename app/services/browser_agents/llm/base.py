"""Provider-neutral shapes for the browser agent model calls (SPEC.md 1, D3).

BDR proxies every agent model call (D1): the worker sends observations to
POST /agent-worker/v1/jobs/{id}/turns and BDR asks the type's configured
provider for exactly one tool call. Each provider has one adapter in this
package; all of them take the same inputs and return the same `LlmResult`:

    complete(model, system, messages, tools, max_output_tokens, force_tool)

`messages` is a neutral transcript (`Message`): user text and images, an
assistant turn carrying at most one tool call, and tool results. Adapters
translate only the envelope; the tool schemas are protocol.TOOL_SCHEMAS
(neutral JSON Schema) for every provider.

Every adapter is stateless toward the provider (no stored conversations, no
previous_response_id), sends parallel tool calls off where the API has the
switch, and never logs a key or a request body. SDK clients are built per
(provider, key) with max_retries=0: the turn service owns the retry policy
(one retry on a transient error, inside the 120 s thinking window).

Errors: an adapter lets SDK exceptions propagate; `classify()` buckets them
into the llm_errors kinds (google-genai errors carry `.code` instead of
`.status_code`, so they get a small mapping here first).
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from app.services import llm_errors

# Provider base URLs for the OpenAI-compatible providers (compat.py).
XAI_BASE_URL = "https://api.x.ai/v1"
META_BASE_URL = "https://api.meta.ai/v1"

# The Settings attribute that holds each provider's key (env only; the keys
# never reach the PC).
PROVIDER_KEY_ATTR: dict[str, str] = {
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "google": "google_api_key",
    "xai": "xai_api_key",
    "meta": "meta_api_key",
}

# Configuration problems: retrying the same request cannot help, an IT Admin
# has to fix the key, the model id or the account (SPEC.md 1, step 7).
CONFIG_KINDS = frozenset({
    llm_errors.KIND_NOT_CONFIGURED,
    llm_errors.KIND_UNAUTHORIZED,
    llm_errors.KIND_OUT_OF_TOKENS,
    "model_not_found",
})

_CALL_ID_BAD = re.compile(r"[^A-Za-z0-9_-]")


class ProviderNotConfigured(RuntimeError):
    """No API key (or no model) for the provider the type is set to."""

    llm_error_kind = llm_errors.KIND_NOT_CONFIGURED


class UnknownProvider(ValueError):
    """A provider name outside protocol.PROVIDERS."""


@dataclass(frozen=True)
class ImagePart:
    media_type: str                      # image/png | image/jpeg
    data: bytes

    def b64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    def data_url(self) -> str:
        return f"data:{self.media_type};base64,{self.b64()}"


@dataclass(frozen=True)
class ToolCall:
    """One tool call the model made. `replay` carries opaque provider state
    that must be echoed back when the call is replayed in a later request
    (Gemini thought signatures); it never reaches the worker. `parse_error`
    is set when the provider's argument string was not a JSON object."""
    call_id: str
    name: str
    args: dict[str, Any]
    replay: dict[str, Any] | None = None
    parse_error: str | None = None


@dataclass(frozen=True)
class Message:
    """One entry of the neutral transcript.

    user       text (+ images)
    assistant  text and/or one tool_call
    tool       the result of the tool call `call_id` (`tool_name`), as text;
               images never ride on a tool message (the turn service puts
               the screenshot in the user message that follows)
    """
    role: Literal["user", "assistant", "tool"]
    text: str = ""
    images: tuple[ImagePart, ...] = ()
    tool_call: ToolCall | None = None
    call_id: str | None = None
    tool_name: str | None = None
    ok: bool = True


@dataclass(frozen=True)
class LlmUsage:
    """input_tokens includes cached_tokens; output_tokens includes any
    reasoning/thinking tokens (both are billed as output)."""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


@dataclass
class LlmResult:
    text: str
    tool_calls: list[ToolCall]
    usage: LlmUsage
    provider_request_id: str | None
    stop_reason: str | None
    raw: dict[str, Any] | None = None     # provider response as JSON (redacted before storage)


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str


@dataclass
class CompleteArgs:
    """Everything one provider call needs (kept for the request artifact)."""
    model: str
    system: str
    messages: list[Message]
    tools: list[dict[str, Any]]
    max_output_tokens: int
    force_tool: str | None = None
    timeout_seconds: float = 90.0
    extra: dict[str, Any] = field(default_factory=dict)


class ProviderAdapter:
    """Base class. Subclasses set `provider` and implement `_complete` and
    `_list_models`; `client` is the provider SDK client (tests inject fakes)."""

    provider: str = ""

    def __init__(self, client: Any):
        self.client = client

    def complete(self, args: CompleteArgs) -> LlmResult:
        return self._complete(args)

    def list_models(self) -> list[ModelInfo]:
        return self._list_models()

    def _complete(self, args: CompleteArgs) -> LlmResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def _list_models(self) -> list[ModelInfo]:  # pragma: no cover - abstract
        raise NotImplementedError


# ── helpers shared by the adapters ────────────────────────────────────────────


def safe_call_id(call_id: str) -> str:
    """A call id every provider accepts (Anthropic: ^[A-Za-z0-9_-]+$, <= 64).
    Applied the same way to the call and to its result, so they still pair."""
    cleaned = _CALL_ID_BAD.sub("_", call_id or "call")[:64]
    return cleaned or "call"


def merge_user(blocks: list[dict[str, Any]], role: str, content: list[Any],
               role_key: str = "role", content_key: str = "content") -> None:
    """Append `content` to the last message when it has the same role, else
    start a new message (Anthropic and Gemini want alternating roles)."""
    if blocks and blocks[-1].get(role_key) == role:
        blocks[-1][content_key].extend(content)
    else:
        blocks.append({role_key: role, content_key: list(content)})


def to_jsonable(obj: Any) -> Any:
    """Best effort JSON form of an SDK response object (pydantic models in
    all three SDKs). Bytes become base64 text."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (bytes, bytearray)):
        return base64.b64encode(bytes(obj)).decode("ascii")
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return to_jsonable(dump(mode="json", exclude_none=True))
        except TypeError:
            return to_jsonable(dump())
    return str(obj)


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def as_int(value: Any) -> int:
    return _int(value)


def classify(exc: BaseException) -> str:
    """The llm_errors kind for a provider exception, plus `model_not_found`
    for a 404 (a model id the account cannot use: a configuration problem,
    unlike other 4xx bad_input)."""
    status = getattr(exc, "status_code", None)
    if status is None:
        try:
            from google.genai import errors as genai_errors

            if isinstance(exc, genai_errors.APIError):
                status = getattr(exc, "code", None)
                if isinstance(status, int):
                    if status == 404:
                        return "model_not_found"
                    if status in (401, 403):
                        return llm_errors.KIND_UNAUTHORIZED
                    if status == 429:
                        text = f"{getattr(exc, 'status', '')} {getattr(exc, 'message', '')}".lower()
                        if "quota" in text and "exceeded" in text and "billing" in text:
                            return llm_errors.KIND_OUT_OF_TOKENS
                        return llm_errors.KIND_RATE_LIMITED
                    if status in (503, 529):
                        return llm_errors.KIND_OVERLOADED
                    if status >= 500:
                        return llm_errors.KIND_SERVER_ERROR
                    if 400 <= status < 500:
                        return llm_errors.KIND_BAD_INPUT
        except ImportError:  # pragma: no cover - google-genai is a dependency
            pass
    if status == 404:
        return "model_not_found"
    return llm_errors.classify(exc)  # type: ignore[arg-type]


def is_transient(kind: str) -> bool:
    return kind != "model_not_found" and llm_errors.is_transient_kind(kind)


def safe_error_text(kind: str) -> str:
    """Fixed text per kind for storage and the probe result: never the SDK
    message (it can carry URLs, request echoes or keys)."""
    return {
        llm_errors.KIND_UNREACHABLE: "Could not connect to the AI provider.",
        llm_errors.KIND_TIMEOUT: "The AI provider did not answer in time.",
        llm_errors.KIND_OVERLOADED: "The AI provider is overloaded right now.",
        llm_errors.KIND_RATE_LIMITED: "The AI provider is rate limiting requests.",
        llm_errors.KIND_SERVER_ERROR: "The AI provider returned an internal error.",
        llm_errors.KIND_OUT_OF_TOKENS: "The AI provider account is out of credits.",
        llm_errors.KIND_NOT_CONFIGURED: "No API key is configured for this provider.",
        llm_errors.KIND_UNAUTHORIZED: "The AI provider rejected the API key.",
        llm_errors.KIND_BAD_INPUT: "The AI provider rejected the request.",
        llm_errors.KIND_INVALID_OUTPUT: "The model answered, but not with a usable tool call.",
        "model_not_found": "The AI provider does not offer this model to this account.",
    }.get(kind, "The AI provider call failed.")

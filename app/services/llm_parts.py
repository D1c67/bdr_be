"""Provider-neutral content parts, transcript turns and the request and
turn types of `llm.complete_turn` (AI Estimating tool loop, WS2).

The tool loop (services/plans_reader_toolloop, wave 2) and the group
builders never speak a provider wire format. They build parts (text, image
with a detail level, PDF file), turns (user, assistant, tool results) and a
ModelRequest; `llm.complete_turn` resolves the route and hands the request
to the OpenAI Responses transport or the Anthropic Messages transport
(services/llm_anthropic). Both return a ModelTurn.

Pure module: no settings, no network, no SDK. Encoders here only shape
dicts; the limits a provider enforces on images live in ProviderImageSpec so
the planner can pack a request for either provider before rendering.

Invariants:
- Legacy Responses parts (`input_text`, `input_image` with a data URL and
  detail, `input_file`) round-trip through `from_responses_part` and
  `to_responses_parts` byte for byte, so chat and the sub builders can keep
  producing dicts and still ride the neutral path.
- An AssistantTurn carries the provider's raw content blocks (`raw`), which
  the Anthropic transport replays unchanged (thinking blocks included) and
  the OpenAI transport ignores (it chains on previous_response_id).
- ToolResultsTurn holds ALL results of one round; the Anthropic encoder
  puts them in one user message, the OpenAI encoder in one batch of
  function_call_output items.
- `tool_choice` is "auto" only. Forced tool choice returns 400 on Claude
  Opus 5.5 and Sonnet 5.5, and the loop never needs it: the final round
  steers with text and, on OpenAI, omits the tools (`tools_enabled`).
"""

from __future__ import annotations

import base64
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Sequence

from app.services.plans_reader_images import (
    IMAGE_DETAILS,
    MAX_PATCHES_ORIGINAL,
    PATCH_PX,
    REQUEST_MAX_BYTES,
    REQUEST_MAX_IMAGES,
    TOKENS_PER_PATCH,
)

PROVIDERS = ("openai", "anthropic")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
TOOL_CHOICES = ("auto",)
STOP_REASONS = ("final", "tool_use", "max_tokens")
MAX_CACHE_BREAKPOINTS = 4
DEFAULT_MAX_TOKENS = 32_000
DEFAULT_TIMEOUT_S = 600.0

# Anthropic image rules (platform docs, checked 2026-10-21): the long edge is
# downscaled above 2,576 px, an image above 3.75 MP is rejected, at most 100
# images per request and 32 MB per request. The planner keeps a margin (90
# images, 30 MB of base64) so a packed group can never hit the server limit.
ANTHROPIC_MAX_LONG_EDGE_PX = 2_576
ANTHROPIC_MAX_PIXELS = 3_750_000
ANTHROPIC_PIXELS_PER_TOKEN = 750
ANTHROPIC_MAX_IMAGE_TOKENS = 4_784  # about 3.59 MP / 750
ANTHROPIC_MAX_IMAGES = 90
ANTHROPIC_MAX_REQUEST_BYTES = 30 * 1024 * 1024


# ---------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextPart:
    text: str
    # Anthropic: put a cache_control breakpoint on this block (the end of a
    # stable prefix such as the ledger). Ignored by OpenAI.
    cache_break: bool = False


@dataclass(frozen=True)
class ImagePart:
    png: bytes
    w_px: int = 0
    h_px: int = 0
    detail: str = "original"  # OpenAI only: original | high | low | auto
    media_type: str = "image/png"
    view_id: str | None = None  # evidence view id (v: citation)
    ref: dict | None = None  # {document_id, page, bbox, ppi}; never bytes

    def __post_init__(self) -> None:
        if self.detail not in IMAGE_DETAILS:
            raise ValueError(f"Unknown image detail '{self.detail}'.")


@dataclass(frozen=True)
class FilePart:
    filename: str
    data: bytes
    media_type: str = "application/pdf"
    pages: int | None = None
    ref: dict | None = None


Part = TextPart | ImagePart | FilePart


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    args: dict

    def as_dict(self) -> dict:
        return {"call_id": self.call_id, "name": self.name, "args": dict(self.args)}


@dataclass(frozen=True)
class ToolResultMsg:
    call_id: str
    text: str  # compacted model-facing text (data, never instructions)
    images: tuple[ImagePart, ...] = ()
    is_error: bool = False


@dataclass(frozen=True)
class UserTurn:
    parts: tuple[Part, ...]


@dataclass(frozen=True)
class AssistantTurn:
    provider: str
    model: str
    response_id: str
    tool_calls: tuple[ToolCall, ...] = ()
    text: str = ""
    # Provider-native content blocks replayed unchanged (Anthropic thinking
    # and tool_use blocks). Empty on OpenAI, which chains by response id.
    raw: tuple[dict, ...] = ()


@dataclass(frozen=True)
class ToolResultsTurn:
    results: tuple[ToolResultMsg, ...]
    # Extra user content after the results (the final-round steer, images
    # a tool delivered on OpenAI).
    extra_parts: tuple[Part, ...] = ()


Turn = UserTurn | AssistantTurn | ToolResultsTurn


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    schema: dict  # strict JSON schema: every property required, additionalProperties false


# ---------------------------------------------------------------------------
# Request and turn
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelRequest:
    """One model call of the tool loop. `messages` is the whole neutral
    transcript (first UserTurn, then AssistantTurn / ToolResultsTurn pairs);
    the OpenAI transport sends only what previous_response_id does not
    already hold, the Anthropic transport sends all of it.

    `step` names the route step (sub, recount, main, review, ...); None
    means the feature's default step. `route` is an already resolved step
    route (the run's snapshot: {provider, model, effort, ...}) that wins
    over the live settings, so a run keeps its routes after the env
    changes. `effort` wins over the route's effort when set."""

    feature: str
    system: str
    messages: tuple[Turn, ...]
    tools: tuple[ToolDef, ...] = ()
    tool_choice: str = "auto"
    tools_enabled: bool = True
    response_schema: dict | None = None
    schema_name: str = "response"
    max_tokens: int = DEFAULT_MAX_TOKENS
    effort: str | None = None
    previous_response_id: str | None = None
    cache_breakpoints: int = MAX_CACHE_BREAKPOINTS
    timeout: float | None = None
    step: str | None = None
    route: dict | None = None
    cache_key: str | None = None

    def __post_init__(self) -> None:
        if self.tool_choice not in TOOL_CHOICES:
            raise ValueError(
                "tool_choice must be 'auto': forced tool choice is never sent "
                "(it returns 400 on Claude Opus 5.5 and Sonnet 5.5)."
            )
        if self.effort is not None and self.effort not in EFFORTS:
            raise ValueError(f"effort must be one of {', '.join(EFFORTS)}.")
        if not isinstance(self.max_tokens, int) or isinstance(self.max_tokens, bool) or self.max_tokens <= 0:
            raise ValueError("max_tokens must be a positive integer.")
        if not 0 <= self.cache_breakpoints <= MAX_CACHE_BREAKPOINTS:
            raise ValueError(f"cache_breakpoints must be between 0 and {MAX_CACHE_BREAKPOINTS}.")
        if not self.messages:
            raise ValueError("A model request needs at least one turn.")
        if not isinstance(self.messages[0], UserTurn):
            raise ValueError("The first turn must be a UserTurn.")
        names = [t.name for t in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("Tool names must be unique.")

    @property
    def last_turn(self) -> Turn:
        return self.messages[-1]

    @property
    def tools_offered(self) -> tuple[ToolDef, ...]:
        """Tools in offer order (sorted by name, cache stability) when
        enabled, else none."""
        if not self.tools_enabled:
            return ()
        return tuple(sorted(self.tools, key=lambda t: t.name))


@dataclass(frozen=True)
class ModelTurn:
    """What complete_turn returns. `stop_reason` is final (answered),
    tool_use (tool_calls to serve) or max_tokens (cut off; the loop decides
    whether to force a final round or fail). `parsed` is the JSON object
    when a schema was given and the turn is final; `usage` is zeros when
    the provider reported none; `cost_usd` is None without a price row;
    `raw_assistant` is what the next AssistantTurn must replay."""

    text: str
    parsed: Any
    tool_calls: list[dict]  # [{call_id, name, args}]
    usage: Any
    cost_usd: Decimal | None
    response_id: str
    stop_reason: str
    provider: str
    model: str
    raw_assistant: tuple[dict, ...] = ()

    def assistant_turn(self) -> AssistantTurn:
        return AssistantTurn(
            provider=self.provider,
            model=self.model,
            response_id=self.response_id,
            tool_calls=tuple(
                ToolCall(call_id=c["call_id"], name=c["name"], args=dict(c.get("args") or {}))
                for c in self.tool_calls
            ),
            text=self.text,
            raw=tuple(self.raw_assistant),
        )


# ---------------------------------------------------------------------------
# Provider image specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderImageSpec:
    provider: str
    max_patches: int | None  # OpenAI: 30,000 patches of 32 px
    max_long_edge_px: int | None  # Anthropic: 2,576
    max_pixels: int | None  # Anthropic: 3.75 MP
    max_images_per_request: int
    max_request_bytes: int  # base64-expanded payload

    def image_tokens(self, w_px: int, h_px: int) -> int:
        """Input tokens one image costs at this provider."""
        w = max(1, int(w_px))
        h = max(1, int(h_px))
        if self.provider == "openai":
            patches = math.ceil(w / PATCH_PX) * math.ceil(h / PATCH_PX)
            if self.max_patches is not None:
                patches = min(patches, self.max_patches)
            return math.ceil(patches * TOKENS_PER_PATCH)
        tokens = math.ceil(w * h / ANTHROPIC_PIXELS_PER_TOKEN)
        return min(ANTHROPIC_MAX_IMAGE_TOKENS, tokens)

    def fits(self, w_px: int, h_px: int) -> bool:
        """True when one image is sent as is (no server-side downscale and
        no rejection)."""
        w = max(1, int(w_px))
        h = max(1, int(h_px))
        if self.max_patches is not None:
            if math.ceil(w / PATCH_PX) * math.ceil(h / PATCH_PX) > self.max_patches:
                return False
        if self.max_long_edge_px is not None and max(w, h) > self.max_long_edge_px:
            return False
        if self.max_pixels is not None and w * h > self.max_pixels:
            return False
        return True

    def scale_to_fit(self, w_px: int, h_px: int) -> tuple[int, int]:
        """The largest (w, h) at the same aspect that fits this provider."""
        w = max(1, int(w_px))
        h = max(1, int(h_px))
        if self.fits(w, h):
            return w, h
        scale = 1.0
        if self.max_long_edge_px is not None and max(w, h) > self.max_long_edge_px:
            scale = min(scale, self.max_long_edge_px / max(w, h))
        if self.max_pixels is not None and w * h > self.max_pixels:
            scale = min(scale, math.sqrt(self.max_pixels / (w * h)))
        if self.max_patches is not None:
            patches = math.ceil(w / PATCH_PX) * math.ceil(h / PATCH_PX)
            if patches > self.max_patches:
                scale = min(scale, math.sqrt(self.max_patches / patches) * 0.995)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        # Patch rounding can leave the result one patch over; step down.
        for _ in range(8):
            if self.fits(nw, nh):
                break
            nw, nh = max(1, int(nw * 0.99)), max(1, int(nh * 0.99))
        return nw, nh


OPENAI_IMAGES = ProviderImageSpec(
    provider="openai",
    max_patches=MAX_PATCHES_ORIGINAL,
    max_long_edge_px=None,
    max_pixels=None,
    max_images_per_request=REQUEST_MAX_IMAGES,
    max_request_bytes=REQUEST_MAX_BYTES,
)
ANTHROPIC_IMAGES = ProviderImageSpec(
    provider="anthropic",
    max_patches=None,
    max_long_edge_px=ANTHROPIC_MAX_LONG_EDGE_PX,
    max_pixels=ANTHROPIC_MAX_PIXELS,
    max_images_per_request=ANTHROPIC_MAX_IMAGES,
    max_request_bytes=ANTHROPIC_MAX_REQUEST_BYTES,
)


def image_spec_for(provider: str) -> ProviderImageSpec:
    if provider == "openai":
        return OPENAI_IMAGES
    if provider == "anthropic":
        return ANTHROPIC_IMAGES
    raise ValueError(f"No image spec for provider '{provider}'.")


# ---------------------------------------------------------------------------
# Encoders
# ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def data_url(media_type: str, data: bytes) -> str:
    return f"data:{media_type};base64,{_b64(data)}"


def _split_data_url(url: str) -> tuple[str, bytes]:
    """(media_type, bytes) of a base64 data URL; raises on any other shape."""
    if not isinstance(url, str) or not url.startswith("data:"):
        raise ValueError("Only base64 data URLs are accepted as image or file sources.")
    head, sep, payload = url[5:].partition(",")
    if not sep or not head.endswith(";base64"):
        raise ValueError("Only base64 data URLs are accepted as image or file sources.")
    media_type = head[: -len(";base64")] or "application/octet-stream"
    try:
        return media_type, base64.b64decode(payload, validate=True)
    except Exception as exc:  # noqa: BLE001 - one app-authored error
        raise ValueError("The data URL payload is not valid base64.") from exc


def from_responses_part(part: dict) -> Part:
    """A legacy OpenAI Responses content part as a neutral Part."""
    kind = part.get("type")
    if kind == "input_text":
        return TextPart(text=str(part.get("text") or ""))
    if kind == "input_image":
        media_type, data = _split_data_url(part.get("image_url") or "")
        return ImagePart(png=data, media_type=media_type, detail=str(part.get("detail") or "original"))
    if kind == "input_file":
        media_type, data = _split_data_url(part.get("file_data") or "")
        return FilePart(filename=str(part.get("filename") or "document.pdf"), data=data, media_type=media_type)
    raise ValueError(f"Unknown Responses content part type '{kind}'.")


def to_responses_part(part: Part) -> dict:
    if isinstance(part, TextPart):
        return {"type": "input_text", "text": part.text}
    if isinstance(part, ImagePart):
        return {
            "type": "input_image",
            "image_url": data_url(part.media_type, part.png),
            "detail": part.detail,
        }
    if isinstance(part, FilePart):
        return {
            "type": "input_file",
            "filename": part.filename,
            "file_data": data_url(part.media_type, part.data),
        }
    raise TypeError(f"Not a content part: {type(part).__name__}")


def to_responses_parts(parts: Sequence[Part]) -> list[dict]:
    """Neutral parts as OpenAI Responses content, in order."""
    return [to_responses_part(p) for p in parts]


def to_anthropic_block(part: Part) -> dict:
    if isinstance(part, TextPart):
        block: dict[str, Any] = {"type": "text", "text": part.text}
        if part.cache_break:
            block["cache_control"] = {"type": "ephemeral"}
        return block
    if isinstance(part, ImagePart):
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": part.media_type, "data": _b64(part.png)},
        }
    if isinstance(part, FilePart):
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": part.media_type, "data": _b64(part.data)},
        }
    raise TypeError(f"Not a content part: {type(part).__name__}")


def to_anthropic_blocks(parts: Sequence[Part]) -> list[dict]:
    """Neutral parts as Anthropic Messages content blocks, in order. An
    empty text part is dropped (the API rejects empty text blocks)."""
    out: list[dict] = []
    for part in parts:
        if isinstance(part, TextPart) and not part.text:
            continue
        out.append(to_anthropic_block(part))
    return out


def _b64_len(n_bytes: int) -> int:
    return 4 * math.ceil(n_bytes / 3)


def payload_bytes(parts: Sequence[Part], provider: str = "openai") -> int:
    """Approximate request bytes the parts occupy once base64-encoded (text
    as UTF-8). Same for both providers today; `provider` is kept for the
    planner's signature."""
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider '{provider}'.")
    total = 0
    for part in parts:
        if isinstance(part, TextPart):
            total += len(part.text.encode("utf-8"))
        elif isinstance(part, ImagePart):
            total += _b64_len(len(part.png))
        elif isinstance(part, FilePart):
            total += _b64_len(len(part.data))
    return total


def iter_parts(messages: Sequence[Turn]) -> list[Part]:
    """Every content part of a transcript in send order (user parts, tool
    result images, extra parts). Assistant turns carry no parts."""
    out: list[Part] = []
    for turn in messages:
        if isinstance(turn, UserTurn):
            out.extend(turn.parts)
        elif isinstance(turn, ToolResultsTurn):
            for result in turn.results:
                out.append(TextPart(text=result.text))
                out.extend(result.images)
            out.extend(turn.extra_parts)
    return out


def count_images(parts: Sequence[Part]) -> int:
    return sum(1 for p in parts if isinstance(p, ImagePart))


@dataclass(frozen=True)
class RequestLimits:
    images: int
    payload_bytes: int
    max_images: int
    max_bytes: int
    oversized_images: tuple[int, ...] = ()  # indexes (in iter_parts order) of images past the spec

    @property
    def ok(self) -> bool:
        return (
            self.images <= self.max_images
            and self.payload_bytes <= self.max_bytes
            and not self.oversized_images
        )

    def problem(self) -> str | None:
        if self.images > self.max_images:
            return f"{self.images} images exceed the provider limit of {self.max_images} per request."
        if self.payload_bytes > self.max_bytes:
            return (
                f"{self.payload_bytes} bytes of content exceed the provider limit of "
                f"{self.max_bytes} bytes per request."
            )
        if self.oversized_images:
            return (
                f"{len(self.oversized_images)} image(s) exceed the provider's pixel limits; "
                "the planner must tile or downscale them."
            )
        return None


def check_limits(messages: Sequence[Turn], provider: str) -> RequestLimits:
    """Images, payload bytes and oversized images of a transcript against
    the provider's spec. A transport refuses to send when `ok` is false
    (the planner must make that unreachable)."""
    spec = image_spec_for(provider)
    parts = iter_parts(messages)
    oversized: list[int] = []
    n_images = 0
    for i, part in enumerate(parts):
        if isinstance(part, ImagePart):
            n_images += 1
            if part.w_px and part.h_px and not spec.fits(part.w_px, part.h_px):
                oversized.append(i)
    return RequestLimits(
        images=n_images,
        payload_bytes=payload_bytes(parts, provider),
        max_images=spec.max_images_per_request,
        max_bytes=spec.max_request_bytes,
        oversized_images=tuple(oversized),
    )


def estimate_text_tokens(parts: Sequence[Part], chars_per_token: int = 3) -> int:
    """Planning estimate of the text tokens in `parts` (3 chars per token is
    the conservative side the plans reader uses)."""
    chars = sum(len(p.text) for p in parts if isinstance(p, TextPart))
    return math.ceil(chars / max(1, chars_per_token))


def estimate_part_tokens(parts: Sequence[Part], provider: str, chars_per_token: int = 3) -> int:
    """Text plus image tokens of `parts` at the provider's image spec.
    Files count by their byte size at 3 chars per token (a PDF's text
    layer is the only thing that bills; its size is a rough upper bound)."""
    spec = image_spec_for(provider)
    total = estimate_text_tokens(parts, chars_per_token)
    for part in parts:
        if isinstance(part, ImagePart):
            if part.w_px and part.h_px:
                total += spec.image_tokens(part.w_px, part.h_px)
            else:
                total += ANTHROPIC_MAX_IMAGE_TOKENS if provider == "anthropic" else math.ceil(
                    MAX_PATCHES_ORIGINAL * TOKENS_PER_PATCH
                )
        elif isinstance(part, FilePart):
            total += math.ceil(len(part.data) / max(1, chars_per_token))
    return total


def tool_call_dicts(calls: Sequence[ToolCall] | Sequence[dict]) -> list[dict]:
    """Normalize tool calls to [{call_id, name, args}] dicts (accepts the
    legacy LlmResult shape with `arguments`)."""
    out: list[dict] = []
    for c in calls:
        if isinstance(c, ToolCall):
            out.append(c.as_dict())
            continue
        args = c.get("args")
        if args is None:
            args = c.get("arguments")
        out.append(
            {
                "call_id": str(c.get("call_id") or ""),
                "name": str(c.get("name") or ""),
                "args": dict(args or {}),
            }
        )
    return out


__all__ = [
    "ANTHROPIC_IMAGES",
    "AssistantTurn",
    "DEFAULT_MAX_TOKENS",
    "EFFORTS",
    "FilePart",
    "ImagePart",
    "MAX_CACHE_BREAKPOINTS",
    "ModelRequest",
    "ModelTurn",
    "OPENAI_IMAGES",
    "Part",
    "ProviderImageSpec",
    "RequestLimits",
    "STOP_REASONS",
    "TextPart",
    "ToolCall",
    "ToolDef",
    "ToolResultMsg",
    "ToolResultsTurn",
    "Turn",
    "UserTurn",
    "check_limits",
    "count_images",
    "data_url",
    "estimate_part_tokens",
    "estimate_text_tokens",
    "from_responses_part",
    "image_spec_for",
    "iter_parts",
    "payload_bytes",
    "to_anthropic_block",
    "to_anthropic_blocks",
    "to_responses_part",
    "to_responses_parts",
    "tool_call_dicts",
]


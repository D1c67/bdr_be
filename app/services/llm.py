"""Unified LLM routing — every AI feature calls its model through here.

Two provider pools, selected by FULL_SELF_HOSTED_LLMS_ENABLED:

- false → 3rd-party SDKs. Which vendor a feature uses is fixed in _FEATURES
  (Anthropic for the document-heavy extractions, OpenAI for the rest); the
  model within that vendor is picked per feature via env (see .env.example).
- true → the self-hosted OpenAI-compatible endpoint (vLLM / Ollama /
  llama.cpp / TGI / LM Studio, behind an EC2 load balancer or on a local
  server). STRICT: while the flag is on, no prompt is ever sent to a
  3rd-party provider. A self-hosted outage or an unset per-feature model
  degrades exactly like a missing API key does today (each caller keeps its
  own graceful fallback) — it never re-routes. The one exception is the
  openai_only features (AI Estimating), which are pinned to OpenAI in BOTH
  modes: a bid set is never sent to a small self-hosted model, so the
  switch leaves them where they are instead of turning them off. The
  mirror image is the self_hosted_only features (the PM email filer's
  email_match), which always run on the box chosen in Settings > AI model
  server, whatever the flag says, with that box's own model.

Call sites keep their own gating, retries and domain validation; this module
only resolves the route, speaks the three wire protocols, and normalizes the
result to text or a parsed JSON dict:

- JSON: OpenAI uses strict structured outputs; the self-hosted path requests
  response_format json_schema and retries once without it when the server
  rejects the parameter (older llama.cpp/Ollama builds), with the schema also
  embedded in the system prompt so schema-less servers still comply;
  Anthropic returns JSON-in-text which is tolerantly parsed (fences stripped).
- PDFs: Anthropic document block / OpenAI input_file. The self-hosted path
  extracts text locally with pypdf so the raw file never leaves the server;
  image-only scans yield no text and raise (the caller's manual-entry
  fallback covers it).

Security posture: endpoint URLs and keys come only from env (validated at
boot in core/config — https enforced in production, TLS verification on by
default with a private-CA override); nothing here logs prompts, keys, or the
endpoint URL; every self-hosted call carries a bounded timeout.

Usage and cost (0152): every transport reports the provider's token usage
into a per-call box that _guarded reads, so each llm_call_log row carries
input / cached / output / reasoning tokens and the cost from
services/llm_pricing. The complete_* functions keep their return types; a
caller that needs the numbers reads last_usage() / last_cost_usd() right
after the call, in the same thread (they ride a contextvar, so a call made
inside asyncio.to_thread is visible inside that thread only).

AI Estimating (docs/PLANS_READER.md): complete_parts() is the OpenAI
Responses transport with mixed content parts (text, images with a detail
level, PDF files), reasoning effort, tools and tool-call continuation. Its
features (plans_*) are openai_only: plan sets are never sent to a small
self-hosted model, so FULL_SELF_HOSTED_LLMS_ENABLED does not move them; they
keep running on the 3rd-party key (owner decision 2026-10-18, reversing the
earlier "off in self-hosted mode" rule). The only thing that switches them
off by design is their own PLANS_READER_ENABLED flag (unavailable_reason).

Routes (docs/AI_ESTIMATING_BUILD_SPEC.md 4.2, WS2): each plans feature maps
to a pipeline step (services/plans_reader_model_routes) whose route names
the provider (openai or anthropic), model and effort. With no route set for
the step the feature runs exactly as before: OpenAI on OPENAI_PLANS_MODEL.
complete_turn(feature, ModelRequest) is the provider-neutral entry of the
tool loop: it dispatches to the OpenAI Responses transport here or to the
Anthropic Messages transport in services/llm_anthropic by the resolved
route, inside the same _guarded gate (logging, caps, ledger guards) and
with the queue lease renewed before every send. complete_parts stays a thin
OpenAI-only wrapper whose request kwargs are byte-identical to before
(tests/test_llm_complete_turn.py snapshots them).

Eval harness hooks (docs/AI_ESTIMATING_BUILD_SPEC.md 0.4, WS0): every
public operation hands _guarded the request it is about to send
(`request=`). _guarded makes exactly two extra calls: (1)
llm_cassette.begin opens the call's tape (LLM_CASSETTE_MODE; off by
default, a no-op); a recorded answer returns at once with cost 0 and a
CassetteUsage, no gate slot, no ledger reservation and no llm_call_log row,
and a live answer is recorded on the way out; (2) eval_ledger.admit
reserves the call's maximum cost under the open eval sweep when
AI_EVAL_LEDGER_ENFORCE is on (EvalBudgetRefused otherwise; a no-op when
off), and the ticket is released after the llm_call_log row is written
(llm_gate.log_call stamps eval_sweep_id). Both transports of complete_turn
(OpenAI Responses and Anthropic Messages) and every legacy operation pass
through the same two hooks.
"""

from __future__ import annotations

import contextvars
import io
import json
import logging
import ssl
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, ClassVar

import httpx
import openai

from app.core.config import Settings, get_settings
from app.services import eval_ledger
from app.services import llm_cassette
from app.services import llm_target
from app.services import llm_gate
from app.services import llm_parts
from app.services import llm_pricing
from app.services import plans_reader_model_routes as model_routes

logger = logging.getLogger(__name__)

# Self-hosted PDF ingestion caps (text is extracted locally with pypdf).
# The char budget and deadline are enforced DURING extraction (visitor_text),
# not after it — a crafted page packed with millions of tiny text operators
# must not be able to peg a worker for minutes before an after-the-fact cap.
_PDF_MAX_PAGES = 50
_PDF_TEXT_MAX_CHARS = 200_000
_PDF_EXTRACT_MAX_SECONDS = 20.0

# Anthropic requires max_tokens; used when a caller doesn't pass one.
_DEFAULT_MAX_TOKENS = 8192


class LlmNotConfigured(RuntimeError):
    """The resolved provider for a feature is missing its key/URL/model."""


class SelfHostedUnreachable(RuntimeError):
    """The self-hosted endpoint did not respond (connection failed / timed out)."""


class LlmBadOutput(ValueError):
    """The model answered, but unusably (cut off, invalid JSON, wrong shape).

    Distinct from plain ValueError so failure handling can tell "this input
    can never work" (permanent) from "regenerate and it will probably work"
    (transient, retried by the job queue)."""


class LlmBadOutputPermanent(LlmBadOutput):
    """An unusable answer that regenerating the same request will not fix:
    the output token limit cut it off, the content filter stopped it, or the
    model refused. Declared bad_input (llm_errors honors llm_error_kind), so
    the queue fails fast instead of resending a 240k-token AI Estimating
    group up to four times at full input cost. The message is app-authored
    and says what to change."""

    llm_error_kind = "bad_input"


class LlmLeaseLost(RuntimeError):
    """complete_parts refused to send because the queue job running it lost
    its lease (the attempt is stale; another worker may own the job). Declared
    infrastructure: its message is app-authored, and the queue's own fenced
    writes already ignore a stale attempt."""

    llm_error_kind = "infrastructure"


@dataclass(frozen=True)
class LlmUsage:
    """Provider-reported token usage for one call.

    input_tokens INCLUDES cached_tokens and output_tokens INCLUDES
    reasoning_tokens (OpenAI's convention; the Anthropic and self-hosted
    readers normalize to it). input_tokens also includes the Anthropic
    prompt-cache WRITE tokens (cache_write_5m_tokens, cache_write_1h_tokens),
    which llm_pricing bills at 1.25x and 2x the input price; OpenAI reports
    none, so they stay 0 there."""

    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    # Where the numbers came from: "provider" for a live call, "cassette"
    # for a replayed one (CassetteUsage). A class attribute, not a field.
    source: ClassVar[str] = "provider"

    # Read-only aliases with the field names docs/PLANS_READER.md section 6
    # uses (LlmUsage(input, cached, output, reasoning)), so modules written to
    # the contract and modules written to this class read the same numbers.
    @property
    def input(self) -> int:
        """Alias of input_tokens (includes cached)."""
        return self.input_tokens

    @property
    def cached(self) -> int:
        """Alias of cached_tokens."""
        return self.cached_tokens

    @property
    def output(self) -> int:
        """Alias of output_tokens (includes reasoning)."""
        return self.output_tokens

    @property
    def reasoning(self) -> int:
        """Alias of reasoning_tokens."""
        return self.reasoning_tokens

    @property
    def cache_write_tokens(self) -> int:
        """Both cache-write counts together (part of input_tokens)."""
        return self.cache_write_5m_tokens + self.cache_write_1h_tokens

    @property
    def cache_ratio(self) -> float:
        """cached / input, 0 when nothing was reported."""
        return (self.cached_tokens / self.input_tokens) if self.input_tokens > 0 else 0.0


@dataclass(frozen=True)
class CassetteUsage(LlmUsage):
    """The recorded usage of a call served from a cassette
    (services/llm_cassette): same numbers, billed $0."""

    source: ClassVar[str] = "cassette"


@dataclass(frozen=True)
class LlmResult:
    """What complete_parts returns.

    parsed: the JSON object when a schema was given and the model answered
    with text (None when it answered with tool calls instead, or no schema).
    tool_calls: [{"call_id", "name", "arguments": dict}] from the response's
    function_call items, in order. usage is zeros when the provider reported
    none. model and cost_usd (llm_pricing.cost_for; None without a price
    row) are extras so callers can record the call without recomputing."""

    parsed: Any
    text: str
    usage: LlmUsage
    response_id: str
    tool_calls: list[dict] = field(default_factory=list)
    model: str = ""
    cost_usd: Decimal | None = None


# Sentinel for SELF_HOSTED_<FEATURE>_MODEL: switch that feature off in
# self-hosted mode even though the live target names a model.
SELF_HOSTED_MODEL_OFF = "off"

PLANS_READER_OFF_MESSAGE = (
    "AI Estimating is switched off on this server (PLANS_READER_ENABLED is false)."
)


@dataclass(frozen=True)
class _Feature:
    # "anthropic" | "openai", or a Settings-reader returning one of those for
    # the features whose vendor is env-selectable (bid_split).
    third_party: str | Callable[[Settings], str]
    tp_model: Callable[[Settings], str]
    sh_model: Callable[[Settings], str]
    # True = pinned to OpenAI: never served by the self-hosted pool, and
    # FULL_SELF_HOSTED_LLMS_ENABLED=true leaves the feature on OpenAI instead
    # of moving it to the box or switching it off.
    openai_only: bool = False
    # True = pinned to the self-hosted pool: always the live box (the one
    # picked in Settings > AI model server, services/llm_target) and that
    # box's own model, even while FULL_SELF_HOSTED_LLMS_ENABLED=false. The
    # per-feature env model is not used, except "off" to disable it.
    self_hosted_only: bool = False
    # self_hosted_only features only: True = a SELF_HOSTED_<FEATURE>_MODEL
    # value other than "off" pins the model on the env default box (the live
    # box override drops pins, services/llm_target). Empty = the box's model.
    self_hosted_pin: bool = False
    # The feature's own on/off switch, when it has one. Off = the feature is
    # unavailable by design (unavailable_reason) whatever the providers say,
    # so the health snapshot leaves it out instead of grading it forever.
    switch: Callable[[Settings], bool] | None = None
    # AI Estimating only: the pipeline step whose route (provider, model,
    # effort; services/plans_reader_model_routes) this feature follows. None
    # = no route (plans_modify): OpenAI on OPENAI_PLANS_MODEL as before.
    route_step: str | None = None
    # True = pinned to OpenAI like openai_only, but NOT an AI Estimating
    # feature: no step route, no PLANS_READER_ENABLED switch and not counted
    # in PLANS_FEATURES (BDR Assistant, docs/ASSISTANT.md, Model and
    # transport). Under FULL_SELF_HOSTED_LLMS_ENABLED the pin holds only with
    # ASSISTANT_ALLOW_OPENAI_IN_SELF_HOSTED (see resolve).
    openai_pinned: bool = False


def _plans_feature(step: str | None = None) -> _Feature:
    """AI Estimating features: never self-hosted (the pin keeps them on the
    3rd-party key in both modes), routed per step when a route names the
    step, else OpenAI on OPENAI_PLANS_MODEL; all behind the page's own
    PLANS_READER_ENABLED switch."""
    return _Feature(
        "openai",
        lambda s: s.openai_plans_model,
        lambda s: SELF_HOSTED_MODEL_OFF,
        openai_only=True,
        switch=lambda s: bool(getattr(s, "plans_reader_enabled", False)),
        route_step=step,
    )


_FEATURES: dict[str, _Feature] = {
    "boq": _Feature(
        "anthropic", lambda s: s.claude_boq_model, lambda s: s.self_hosted_boq_model
    ),
    "estimate": _Feature(
        "anthropic", lambda s: s.claude_estimate_model, lambda s: s.self_hosted_estimate_model
    ),
    "translate": _Feature(
        "anthropic", lambda s: s.claude_translate_model, lambda s: s.self_hosted_translate_model
    ),
    "proposal": _Feature(
        "openai", lambda s: s.openai_proposal_model, lambda s: s.self_hosted_proposal_model
    ),
    "quote_pdf": _Feature(
        "openai", lambda s: s.openai_quote_model, lambda s: s.self_hosted_quote_pdf_model
    ),
    "email_vary": _Feature(
        "openai", lambda s: s.openai_email_model, lambda s: s.self_hosted_email_vary_model
    ),
    "aliases": _Feature(
        "openai", lambda s: s.openai_alias_model, lambda s: s.self_hosted_aliases_model
    ),
    # PM email filer, identification round 3 (services/email_ingest): owner
    # decision 2026-10-18, it runs on the Settings model server only.
    "email_match": _Feature(
        "openai",
        lambda s: s.openai_email_match_model,
        lambda s: s.self_hosted_email_match_model,
        self_hosted_only=True,
    ),
    # PM email classification (services/email_classification): labels every
    # email filed to a PM project. Owner decision: self-hosted box ONLY, never
    # a 3rd-party model, whatever FULL_SELF_HOSTED_LLMS_ENABLED says. A box
    # outage is a transient queue failure, never a fallback. Behind its own
    # EMAIL_CLASSIFY_ENABLED switch.
    "email_classify": _Feature(
        "openai",
        lambda s: "",
        lambda s: s.self_hosted_email_classify_model,
        self_hosted_only=True,
        self_hosted_pin=True,
        switch=lambda s: bool(getattr(s, "email_classify_enabled", True)),
    ),
    # Bid File Splitter page classification. The vendor is env-selectable
    # (BID_SPLIT_LLM_PROVIDER: anthropic | openai | self_hosted) rather than
    # fixed: the tool exists to compare models against each other. Vision-only
    # — see complete_images_json.
    "bid_split": _Feature(
        lambda s: s.bid_split_llm_provider,
        lambda s: (
            s.claude_bid_split_model
            if s.bid_split_llm_provider == "anthropic"
            else s.openai_bid_split_model
        ),
        lambda s: s.self_hosted_bid_split_model,
    ),
    # RFP email intake: is this message inviting us to bid? Small JSON answer
    # (yes/no/undetermined + confidence). Waits, never falls back, while the
    # self-hosted box is off (see services/rfp_email_ingest).
    "rfp_classify": _Feature(
        "openai",
        lambda s: s.openai_rfp_classify_model,
        lambda s: s.self_hosted_rfp_classify_model,
    ),
    # RFP field extraction and project matching (services/rfp_match): the two
    # steps after classify, same vendor and the same wait-never-fall-back
    # behavior while the box is off. An empty model setting falls back to the
    # classify model so no new env is required to run them.
    "rfp_extract": _Feature(
        "openai",
        lambda s: s.openai_rfp_extract_model or s.openai_rfp_classify_model,
        lambda s: s.self_hosted_rfp_extract_model or s.self_hosted_rfp_classify_model,
    ),
    "rfp_match": _Feature(
        "openai",
        lambda s: s.openai_rfp_match_model or s.openai_rfp_classify_model,
        lambda s: s.self_hosted_rfp_match_model or s.self_hosted_rfp_classify_model,
    ),
    # AI Estimating (docs/PLANS_READER.md): sub agent group calls, the main
    # consolidation, deliverable modifications and the chat. GPT-6 Astra by
    # default (OPENAI_PLANS_MODEL) in both modes (openai_only); each follows
    # its step's route (docs/AI_ESTIMATING_BUILD_SPEC.md 4.2) when one is set.
    "plans_sub": _plans_feature("sub"),
    "plans_main": _plans_feature("main"),
    "plans_modify": _plans_feature(None),
    "plans_chat": _plans_feature("chat"),
    # AI Estimating autonomous build (spec 4.2): the recount tool loop, the
    # independent review and its revise turn, the derive-stage measure
    # assist, the scope attribution fallback and the quote-parsing fallback.
    "plans_recount": _plans_feature("recount"),
    "plans_review": _plans_feature("review"),
    "plans_revise": _plans_feature("revise"),
    "plans_derive": _plans_feature("derive"),
    "plans_scope_attrib": _plans_feature("scope_attrib"),
    "pricing_quote": _plans_feature("quote"),
    # BDR Assistant (docs/ASSISTANT.md, Model and transport). The planner is
    # pinned to OpenAI in both LLM modes through openai_pinned (not
    # openai_only: it is not an AI Estimating feature and must not join
    # PLANS_FEATURES) and carries NO switch: ASSISTANT_ENABLED gates the
    # routes and the worker pass, not the route table, so the feature stays
    # on while PLANS_READER_ENABLED is off (a test asserts it). The pin needs
    # ASSISTANT_ALLOW_OPENAI_IN_SELF_HOSTED under the self-hosted master
    # switch (the boot validator demands it while the assistant is on);
    # without it the feature follows the box like every other feature, which
    # only the health snapshot can observe (the routes 404 and the claim
    # pass is skipped while ASSISTANT_ENABLED is off).
    "assistant": _Feature(
        "openai",
        lambda s: s.assistant_model,
        lambda s: "",
        openai_pinned=True,
    ),
    # Tool-less summarizing and classifying of untrusted text (mail bodies,
    # phase 5); may run on the Qwen boxes, so it follows the master switch.
    "assistant_extract": _Feature(
        "openai",
        lambda s: s.assistant_model,
        lambda s: "",
    ),
}

PLANS_FEATURES: tuple[str, ...] = tuple(
    key for key, spec in _FEATURES.items() if spec.openai_only
)


@dataclass(frozen=True)
class Route:
    provider: str  # "anthropic" | "openai" | "self_hosted"
    model: str
    api_key: str
    base_url: str = ""
    # AI Estimating routes only: the step the route came from and its
    # effort (None for every other feature).
    step: str | None = None
    effort: str | None = None


def _plans_route(spec: _Feature, s: Settings, *, step: str | None = None, snapshot: dict | None = None) -> Route:
    """The pinned 3rd-party route of a plans feature: the step's route
    (provider, model, effort) when the step has one, else OpenAI on
    OPENAI_PLANS_MODEL. Never self-hosted."""
    chosen = step or spec.route_step
    if chosen is None:
        return Route(provider="openai", model=spec.tp_model(s), api_key=s.openai_api_key)
    sr = model_routes.resolve_step(chosen, s, snapshot=snapshot)
    if sr.provider == "anthropic":
        return Route(
            provider="anthropic", model=sr.model, api_key=s.anthropic_api_key,
            step=sr.step, effort=sr.effort,
        )
    return Route(
        provider="openai", model=sr.model, api_key=s.openai_api_key,
        step=sr.step, effort=sr.effort,
    )


def _self_hosted_model(spec: _Feature, s: Settings) -> str:
    """The model a feature uses on the live self-hosted target.

    Per-feature SELF_HOSTED_<FEATURE>_MODEL wins when set (an override, or the
    literal "off" to disable the feature); otherwise the target's own
    SELF_HOSTED_LLM_<TARGET>_MODEL, so flipping SELF_HOSTED_LLM_TARGET moves
    every feature to the new box's model in one line. Empty on both = off.
    """
    explicit = (spec.sh_model(s) or "").strip()
    if explicit.lower() == SELF_HOSTED_MODEL_OFF:
        return ""
    return explicit or s.self_hosted_llm_target_model


def resolve(feature: str, settings: Settings | None = None) -> Route:
    """Resolve a feature to (provider, model, credentials) from the env, with
    the live box override from Settings (services/llm_target) applied."""
    s = llm_target.apply(settings or get_settings())
    spec = _FEATURES[feature]
    if spec.openai_only:
        # Pinned: the self-hosted switch does not move AI Estimating; the
        # step's route (when set) decides between OpenAI and Anthropic.
        return _plans_route(spec, s)
    if spec.openai_pinned and getattr(s, "assistant_allow_openai_in_self_hosted", False):
        # Pinned: the self-hosted switch does not move the assistant either.
        # The pin needs the owner's explicit allow (core/config
        # _validate_assistant refuses to boot without it while the assistant
        # is on); without it the feature is off on the box like any other
        # feature whose self-hosted model is "off".
        return Route(provider="openai", model=spec.tp_model(s), api_key=s.openai_api_key)
    if spec.self_hosted_only:
        explicit = (spec.sh_model(s) or "").strip()
        off = explicit.lower() == SELF_HOSTED_MODEL_OFF
        model = "" if off else (explicit if spec.self_hosted_pin and explicit else s.self_hosted_llm_target_model)
        base_url = s.self_hosted_llm_base_url
        if (
            base_url.startswith("http://") and s.is_production and not s.self_hosted_llm_allow_http
        ) or not base_url.startswith(("https://", "http://")):
            # The boot validator only checks the box while the master switch
            # is on; with it off this route must refuse plain http itself.
            base_url = ""
        return Route(
            provider="self_hosted",
            model=model,
            api_key=s.self_hosted_llm_api_key,
            base_url=base_url,
        )
    if s.full_self_hosted_llms_enabled:
        return Route(
            provider="self_hosted",
            model=_self_hosted_model(spec, s),
            api_key=s.self_hosted_llm_api_key,
            base_url=s.self_hosted_llm_base_url,
        )
    provider = spec.third_party(s) if callable(spec.third_party) else spec.third_party
    if provider == "self_hosted":
        # Env-selectable features (bid_split) may opt INTO the self-hosted pool
        # for just themselves while the master switch is off. The switch stays
        # one-way: true forces EVERY feature self-hosted, never the reverse.
        return Route(
            provider="self_hosted",
            model=_self_hosted_model(spec, s),
            api_key=s.self_hosted_llm_api_key,
            base_url=s.self_hosted_llm_base_url,
        )
    if provider == "anthropic":
        return Route(provider="anthropic", model=spec.tp_model(s), api_key=s.anthropic_api_key)
    return Route(provider="openai", model=spec.tp_model(s), api_key=s.openai_api_key)


def unavailable_reason(feature: str, settings: Settings | None = None) -> str | None:
    """Why the feature is switched off by design (its own switch is off,
    e.g. PLANS_READER_ENABLED=false for the plans features), or None. A
    missing key or model is NOT reported here; is_configured covers that."""
    s = llm_target.apply(settings or get_settings())
    spec = _FEATURES[feature]
    if spec.switch is not None and not spec.switch(s):
        return PLANS_READER_OFF_MESSAGE if spec.openai_only else f"The '{feature}' feature is switched off."
    return None


def is_configured(feature: str, settings: Settings | None = None) -> bool:
    """True when the ACTIVE pool can serve this feature. Callers gate on this
    exactly as they used to gate on the vendor API key being present. A
    feature whose own switch is off is False, never an exception."""
    if unavailable_reason(feature, settings) is not None:
        return False
    route = resolve(feature, settings)
    if route.provider == "self_hosted":
        return bool(route.base_url and route.model)
    return bool(route.api_key and route.model)


def active_model(feature: str, settings: Settings | None = None) -> str:
    """Model label for DB records / user-facing errors, provider-tagged when
    self-hosted so stored rows show where an answer actually came from.

    Never raises for a feature whose own switch is off: it returns the
    3rd-party model name, because the queue labels a failed job with this
    (llm_queue._JobSpec.model_label) and a raise there would bury the real
    reason under the generic handler-failed text. Use is_configured /
    unavailable_reason to ask whether the feature can run."""
    s = llm_target.apply(settings or get_settings())
    if unavailable_reason(feature, s) is not None:
        return _FEATURES[feature].tp_model(s)
    route = resolve(feature, s)
    if route.provider == "self_hosted":
        return f"self-hosted:{route.model or '(no model set)'}"
    return route.model


def _require_route(feature: str, settings: Settings | None) -> tuple[Route, Settings]:
    s = llm_target.apply(settings or get_settings())
    route = resolve(feature, s)
    if route.provider == "self_hosted":
        if not route.base_url:
            raise LlmNotConfigured(
                "Self-hosted LLM mode is enabled but no endpoint URL is set "
                f"(SELF_HOSTED_LLM_TARGET={s.self_hosted_llm_target!r} — set the "
                "matching SELF_HOSTED_LLM_*_BASE_URL)."
            )
        if not route.model:
            raise LlmNotConfigured(
                "Self-hosted LLM mode is enabled but no model is configured for "
                f"this feature (set SELF_HOSTED_LLM_{s.self_hosted_llm_target.upper()}"
                f"_MODEL for the whole target, or SELF_HOSTED_{feature.upper()}_MODEL)."
            )
    elif not route.api_key:
        var = "ANTHROPIC_API_KEY" if route.provider == "anthropic" else "OPENAI_API_KEY"
        raise LlmNotConfigured(f"{var} is not configured on the server.")
    elif not route.model:
        raise LlmNotConfigured(f"No model is configured for the '{feature}' feature.")
    return route, s


# ── Clients ──────────────────────────────────────────────────────────────────

_clients: dict[tuple, Any] = {}


def _client_for(route: Route, settings: Settings) -> Any:
    """Cached SDK client for a route. Key includes everything that shapes the
    client so env changes (tests, restarts of the model server) re-create it."""
    if route.provider in ("anthropic", "openai"):
        key: tuple = (
            route.provider,
            route.api_key,
            settings.third_party_llm_timeout_seconds,
            settings.third_party_llm_max_retries,
        )
    else:
        key = (
            "self_hosted",
            route.base_url,
            route.api_key,
            settings.self_hosted_llm_timeout_seconds,
            settings.self_hosted_llm_verify_tls,
            settings.self_hosted_llm_ca_bundle,
        )
    client = _clients.get(key)
    if client is None:
        client = _clients.setdefault(key, _build_client(route, settings))
    return client


def _build_client(route: Route, settings: Settings) -> Any:
    if route.provider == "anthropic":
        from anthropic import Anthropic

        # Explicit bounds: the SDK defaults (600s read, 2 retries) could
        # outlive the RFP sweep lease; config.py sizes the lease from these.
        return Anthropic(
            api_key=route.api_key,
            timeout=float(settings.third_party_llm_timeout_seconds),
            max_retries=settings.third_party_llm_max_retries,
        )
    if route.provider == "openai":
        return openai.OpenAI(
            api_key=route.api_key,
            timeout=float(settings.third_party_llm_timeout_seconds),
            max_retries=settings.third_party_llm_max_retries,
        )

    # Self-hosted: OpenAI-compatible endpoint. TLS verification stays on unless
    # explicitly disabled; a private CA (internal ALB certs) is the right way
    # to keep verification while using your own PKI.
    verify: ssl.SSLContext | bool
    if settings.self_hosted_llm_ca_bundle:
        verify = ssl.create_default_context(cafile=settings.self_hosted_llm_ca_bundle)
    else:
        verify = settings.self_hosted_llm_verify_tls
    return openai.OpenAI(
        # Many self-hosted servers ignore auth entirely; the SDK still requires
        # a non-empty bearer value.
        api_key=route.api_key or "self-hosted-no-key",
        base_url=route.base_url,
        timeout=float(settings.self_hosted_llm_timeout_seconds),
        http_client=httpx.Client(verify=verify),
    )


def _with_timeout(client: Any, timeout: float | None) -> Any:
    return client.with_options(timeout=timeout) if timeout is not None else client


# ── JSON helpers ─────────────────────────────────────────────────────────────


def parse_json_loose(text: str) -> Any:
    """Tolerantly parse model JSON: strip ``` fences (multi-line or the whole
    reply on one line), accept raw control chars inside strings (strict=False).
    Raises ValueError on non-JSON."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t[3:]
        stripped = t.lstrip()
        if stripped[:4].lower() == "json":
            t = stripped[4:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    try:
        return json.loads(t, strict=False)
    except json.JSONDecodeError as exc:
        raise LlmBadOutput("Model response was not valid JSON. Retry the generation.") from exc


def _schema_hint(system: str, schema: dict | None) -> str:
    """Self-hosted servers may ignore/reject response_format — embed the output
    contract in the system prompt so the model complies regardless."""
    if schema is None:
        return (
            system
            + "\n\nReturn ONLY a single JSON object — no markdown fences, no commentary."
        )
    return (
        system
        + "\n\nReturn ONLY a single JSON object matching this JSON Schema — "
        "no markdown fences, no commentary:\n"
        + json.dumps(schema)
    )


# ── Usage capture ────────────────────────────────────────────────────────────


class _UsageBox:
    """Per-call slot a transport fills with the provider's usage, read back by
    _guarded. A box (not a return value) so the transports keep returning
    plain text and every existing call path stays unchanged."""

    __slots__ = ("usage",)

    def __init__(self) -> None:
        self.usage: LlmUsage | None = None


_usage_box: contextvars.ContextVar[_UsageBox | None] = contextvars.ContextVar(
    "llm_usage_box", default=None
)
_last_usage: contextvars.ContextVar[LlmUsage | None] = contextvars.ContextVar(
    "llm_last_usage", default=None
)
_last_cost: contextvars.ContextVar[Decimal | None] = contextvars.ContextVar(
    "llm_last_cost", default=None
)


def last_usage() -> LlmUsage | None:
    """Token usage of the most recent LLM call made in this context (thread or
    task), success or failure; None when the provider reported none or the
    call failed before reaching the provider (every complete_* clears it
    first, so it never shows an earlier call's numbers). Read it
    right after a complete_* call to record tokens without changing that
    function's return type."""
    return _last_usage.get()


def last_cost_usd() -> Decimal | None:
    """Cost of the most recent call in this context (llm_pricing.cost_for),
    None when there was no usage or the model has no price row."""
    return _last_cost.get()


def last_call_replayed() -> bool:
    """True when the most recent call in this context was served from a
    cassette (no provider call, cost 0)."""
    return getattr(_last_usage.get(), "source", None) == "cassette"


def _reset_last() -> None:
    """Clear last_usage() / last_cost_usd() at the start of every public
    complete_* call, before anything can raise. Otherwise a call that fails
    before reaching _guarded (not configured, bad arguments) would leave the
    PREVIOUS call's numbers readable, and a caller recording spend in its
    except path would bill that call twice. Queue threads are pooled, so a
    stale value could also leak from one job into the next."""
    _last_usage.set(None)
    _last_cost.set(None)


def _record_usage(usage: LlmUsage | None) -> None:
    """Hand a transport's usage to the _UsageBox of the call in flight (no-op
    outside _guarded, e.g. a transport called directly from a test)."""
    box = _usage_box.get()
    if box is not None:
        box.usage = usage


def _get(obj: Any, name: str) -> Any:
    """Attribute or key `name` of an SDK object or a plain dict (tests and
    some SDK versions hand back dicts), None when absent."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _int(value: Any) -> int:
    """A non-negative token count; anything that is not a number reads as 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _openai_usage(resp: Any) -> LlmUsage | None:
    """Responses API usage: input_tokens (incl. cached), output_tokens (incl.
    reasoning) and the two detail counts. Never raises."""
    try:
        u = _get(resp, "usage")
        if u is None:
            return None
        return LlmUsage(
            input_tokens=_int(_get(u, "input_tokens")),
            cached_tokens=_int(_get(_get(u, "input_tokens_details"), "cached_tokens")),
            output_tokens=_int(_get(u, "output_tokens")),
            reasoning_tokens=_int(_get(_get(u, "output_tokens_details"), "reasoning_tokens")),
        )
    except Exception:  # noqa: BLE001 - accounting must never break the call
        return None


def _anthropic_usage(resp: Any) -> LlmUsage | None:
    """Messages API usage. Anthropic's input_tokens EXCLUDES cache reads and
    cache writes, so they are added back to match the OpenAI convention
    (input includes cached). Cache writes are split by TTL from
    usage.cache_creation (5m and 1h); without that detail the whole
    cache_creation_input_tokens count reads as a 5-minute write. Thinking
    bills inside output_tokens, so reasoning_tokens stays 0. Never raises."""
    try:
        u = _get(resp, "usage")
        if u is None:
            return None
        cache_read = _int(_get(u, "cache_read_input_tokens"))
        cache_write = _int(_get(u, "cache_creation_input_tokens"))
        creation = _get(u, "cache_creation")
        write_5m = _int(_get(creation, "ephemeral_5m_input_tokens"))
        write_1h = _int(_get(creation, "ephemeral_1h_input_tokens"))
        if write_5m + write_1h == 0:
            write_5m = cache_write
        elif write_5m + write_1h > cache_write:
            cache_write = write_5m + write_1h
        return LlmUsage(
            input_tokens=_int(_get(u, "input_tokens")) + cache_read + cache_write,
            cached_tokens=cache_read,
            output_tokens=_int(_get(u, "output_tokens")),
            reasoning_tokens=0,
            cache_write_5m_tokens=write_5m,
            cache_write_1h_tokens=write_1h,
        )
    except Exception:  # noqa: BLE001
        return None


def _chat_usage(resp: Any) -> LlmUsage | None:
    """Chat Completions usage (the self-hosted wire): prompt_tokens and
    completion_tokens, plus the detail counts when the server sends them.
    Never raises."""
    try:
        u = _get(resp, "usage")
        if u is None:
            return None
        return LlmUsage(
            input_tokens=_int(_get(u, "prompt_tokens")),
            cached_tokens=_int(_get(_get(u, "prompt_tokens_details"), "cached_tokens")),
            output_tokens=_int(_get(u, "completion_tokens")),
            reasoning_tokens=_int(
                _get(_get(u, "completion_tokens_details"), "reasoning_tokens")
            ),
        )
    except Exception:  # noqa: BLE001
        return None


# ── Transports ───────────────────────────────────────────────────────────────


def _anthropic_call(
    client: Any, model: str, system: str, messages: list[dict], max_tokens: int | None
) -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens or _DEFAULT_MAX_TOKENS,
        # Cache the (often large, reused) system prompt across re-runs.
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=messages,
    )
    _record_usage(_anthropic_usage(resp))
    return "".join(b.text for b in resp.content if b.type == "text")


def _openai_call(
    client: Any,
    model: str,
    system: str,
    input_: Any,
    max_tokens: int | None,
    schema: dict | None,
    schema_name: str,
    json_output: bool,
) -> str:
    kwargs: dict[str, Any] = {"model": model, "instructions": system, "input": input_}
    if max_tokens is not None:
        kwargs["max_output_tokens"] = max_tokens
    if json_output and schema is not None:
        kwargs["text"] = {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema,
                "strict": True,
            }
        }
    resp = client.responses.create(**kwargs)
    _raise_if_incomplete(resp)
    return resp.output_text or ""


def _raise_if_incomplete(resp: Any) -> None:
    """Record the usage (an incomplete response still bills) and raise
    LlmBadOutput when the Responses API cut the answer off."""
    _record_usage(_openai_usage(resp))
    if getattr(resp, "status", None) == "incomplete":
        reason = getattr(getattr(resp, "incomplete_details", None), "reason", "unknown")
        raise LlmBadOutput(f"Model response was cut off ({reason}). Retry the generation.")


def _merge_same_role(messages: list[dict]) -> list[dict]:
    """Fold consecutive same-role turns into one. Anthropic accepts them
    (merged server-side), but self-hosted chat templates with strict role
    alternation (Mistral-family under vLLM/TGI) reject them with a 400."""
    merged: list[dict] = []
    for m in messages:
        if (
            merged
            and merged[-1]["role"] == m["role"]
            and isinstance(merged[-1].get("content"), str)
            and isinstance(m.get("content"), str)
        ):
            merged[-1] = {
                "role": m["role"],
                "content": merged[-1]["content"] + "\n\n" + m["content"],
            }
        else:
            merged.append(dict(m))
    return merged


def _self_hosted_call(
    client: Any,
    model: str,
    system: str,
    messages: list[dict],
    max_tokens: int | None,
    schema: dict | None,
    schema_name: str,
    json_output: bool,
) -> str:
    if json_output:
        system = _schema_hint(system, schema)
    chat = [{"role": "system", "content": system}, *_merge_same_role(messages)]
    kwargs: dict[str, Any] = {"model": model, "messages": chat}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if json_output:
        if schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            }
        else:
            kwargs["response_format"] = {"type": "json_object"}
    try:
        try:
            resp = client.chat.completions.create(**kwargs)
        except openai.BadRequestError:
            # Older Ollama/llama.cpp builds reject response_format — the schema
            # is already embedded in the system prompt, so retry without it.
            if "response_format" not in kwargs:
                raise
            kwargs.pop("response_format")
            resp = client.chat.completions.create(**kwargs)
    except openai.APIConnectionError as exc:  # incl. timeouts
        raise SelfHostedUnreachable(
            "The self-hosted AI service did not respond (connection failed or "
            "timed out). Check that the model server is running, or contact "
            "your IT Director."
        ) from exc
    _record_usage(_chat_usage(resp))
    choice = resp.choices[0]
    if json_output and getattr(choice, "finish_reason", None) == "length":
        raise LlmBadOutput("Model response was cut off (token limit). Retry the generation.")
    return choice.message.content or ""


# ── Public operations ────────────────────────────────────────────────────────


def _guarded(
    feature: str, route: Route, s: Settings, fn: Callable[[], Any], *, request: Any = None
) -> Any:
    """Run one provider call inside the concurrency gate and account for it.

    The gate caps in-flight calls per provider (LlmBusy on admission
    timeout); every outcome lands in llm_call_log for the AI monitor, with
    the token usage the transport reported and its cost. The same numbers
    are exposed through last_usage() / last_cost_usd().

    `request` is what the transport is about to send (the cassette
    fingerprint and the ledger's reserve estimate). Two hooks, see the
    module docstring: the cassette tape (a hit returns here, $0) and the
    eval ledger ticket (released after the log row is written)."""
    tape = llm_cassette.begin(feature, route, s, request)
    if tape.hit:
        return _cassette_replay(tape)
    tier = llm_gate.current_tier()
    started = time.monotonic()
    box = _UsageBox()
    box_token = _usage_box.set(box)
    ticket = eval_ledger.NO_TICKET
    try:
        ticket = eval_ledger.admit(feature, route, s, request)
        with llm_gate.slot(route.provider, tier, s):
            result = fn()
    except Exception as exc:
        ticket.release(_log(feature, route, tier, started, box.usage, exc), exc)
        raise
    finally:
        _usage_box.reset(box_token)
    ticket.release(_log(feature, route, tier, started, box.usage, None))
    tape.record(result, box.usage)
    return result


def _cassette_replay(tape: llm_cassette.Tape) -> Any:
    """A cassette hit: publish the recorded usage (CassetteUsage) with cost
    0 and return the recorded result. No gate, no ledger, no log row."""
    _last_usage.set(tape.usage)
    _last_cost.set(Decimal("0"))
    return tape.result


def _log(
    feature: str,
    route: Route,
    tier: str,
    started: float,
    usage: LlmUsage | None,
    error: Exception | None,
) -> Decimal | None:
    """Publish the call's usage and cost to last_usage() / last_cost_usd()
    and write its llm_call_log row (log_call never raises). Returns the
    cost (the eval ledger commits it)."""
    cost = llm_pricing.cost_for(route.model, usage) if usage is not None else None
    _last_usage.set(usage)
    _last_cost.set(cost)
    llm_gate.log_call(
        feature=feature,
        provider=route.provider,
        model=route.model,
        tier_name=tier,
        ok=error is None,
        error=error,
        duration_ms=int((time.monotonic() - started) * 1000),
        input_tokens=usage.input_tokens if usage else None,
        cached_tokens=usage.cached_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        reasoning_tokens=usage.reasoning_tokens if usage else None,
        cost_usd=cost,
    )
    return cost


def complete_text(
    feature: str,
    *,
    system: str,
    messages: list[dict],
    max_tokens: int | None = None,
    timeout: float | None = None,
    settings: Settings | None = None,
) -> str:
    """Plain-text completion. `messages` are neutral {role, content} turns."""
    _reset_last()
    route, s = _require_route(feature, settings)
    client = _with_timeout(_client_for(route, s), timeout)

    def call() -> str:
        if route.provider == "anthropic":
            return _anthropic_call(client, route.model, system, messages, max_tokens)
        if route.provider == "openai":
            input_ = messages if len(messages) > 1 else messages[0]["content"]
            return _openai_call(
                client, route.model, system, input_, max_tokens, None, "response", False
            )
        return _self_hosted_call(
            client, route.model, system, messages, max_tokens, None, "response", False
        )

    request = {"op": "text", "system": system, "messages": messages, "max_tokens": max_tokens}
    return _guarded(feature, route, s, call, request=request)


def complete_json(
    feature: str,
    *,
    system: str,
    messages: list[dict],
    schema: dict | None = None,
    schema_name: str = "response",
    max_tokens: int | None = None,
    timeout: float | None = None,
    settings: Settings | None = None,
) -> Any:
    """JSON completion → parsed object. With `schema`, OpenAI/self-hosted use
    structured outputs; without one (the Claude extractions, whose prompts
    define the shape), the reply is tolerantly parsed. Domain validation stays
    with the caller."""
    _reset_last()
    route, s = _require_route(feature, settings)
    client = _with_timeout(_client_for(route, s), timeout)

    def call() -> Any:
        if route.provider == "anthropic":
            # Claude has no response_format: with a schema, the output
            # contract goes into the system prompt (as the image and PDF
            # variants already do), or the model answers in prose and the
            # parse below fails every time.
            text = _anthropic_call(
                client,
                route.model,
                _schema_hint(system, schema) if schema is not None else system,
                messages,
                max_tokens,
            )
        elif route.provider == "openai":
            input_ = messages if len(messages) > 1 else messages[0]["content"]
            text = _openai_call(
                client, route.model, system, input_, max_tokens, schema, schema_name, True
            )
        else:
            text = _self_hosted_call(
                client, route.model, system, messages, max_tokens, schema, schema_name, True
            )
        return parse_json_loose(text)

    request = {
        "op": "json", "system": system, "messages": messages, "schema": schema,
        "schema_name": schema_name, "max_tokens": max_tokens,
    }
    return _guarded(feature, route, s, call, request=request)


def complete_pdf_json(
    feature: str,
    *,
    prompt: str,
    pdf_bytes: bytes,
    filename: str,
    schema: dict,
    schema_name: str,
    max_tokens: int | None = None,
    timeout: float | None = None,
    settings: Settings | None = None,
) -> Any:
    """Ask a question about a PDF, returning parsed JSON. 3rd-party models read
    the PDF natively; the self-hosted path extracts text locally (pypdf) so the
    file itself never leaves the server."""
    _reset_last()
    route, s = _require_route(feature, settings)
    client = _with_timeout(_client_for(route, s), timeout)

    def call() -> Any:
        if route.provider == "anthropic":
            import base64

            content = [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.b64encode(pdf_bytes).decode(),
                    },
                },
                {"type": "text", "text": prompt},
            ]
            text = _anthropic_call(
                client, route.model, _schema_hint("", schema), [{"role": "user", "content": content}], max_tokens
            )
        elif route.provider == "openai":
            import base64

            input_ = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_file",
                            "filename": filename,
                            "file_data": "data:application/pdf;base64,"
                            + base64.b64encode(pdf_bytes).decode(),
                        },
                        {"type": "input_text", "text": prompt},
                    ],
                }
            ]
            text = _openai_call(
                client, route.model, "", input_, max_tokens, schema, schema_name, True
            )
        else:
            doc_text = _pdf_to_text(pdf_bytes)
            if not doc_text.strip():
                raise ValueError(
                    "The PDF contains no extractable text (likely a scanned image) — "
                    "a self-hosted text model cannot read it."
                )
            user = (
                f"{prompt}\n\nExtracted text of {filename}:\n<document>\n{doc_text}\n</document>"
            )
            text = _self_hosted_call(
                client,
                route.model,
                "",
                [{"role": "user", "content": user}],
                max_tokens,
                schema,
                schema_name,
                True,
            )
        return parse_json_loose(text)

    request = {
        "op": "pdf_json", "prompt": prompt, "pdf": pdf_bytes, "filename": filename,
        "schema": schema, "schema_name": schema_name, "max_tokens": max_tokens,
    }
    return _guarded(feature, route, s, call, request=request)


def complete_images_json(
    feature: str,
    *,
    system: str = "",
    prompt: str,
    images: list[tuple[str, bytes]],
    media_type: str = "image/jpeg",
    schema: dict,
    schema_name: str,
    max_tokens: int | None = None,
    timeout: float | None = None,
    settings: Settings | None = None,
) -> Any:
    """Ask a question about a batch of images, returning parsed JSON.

    `images` is an ordered list of (label, bytes) pairs; each label is placed
    as a text block immediately before its image so the model can reference
    pages by name ("Page 3 of 40"). The first vision path in the codebase
    (bid_split) — unlike complete_pdf_json there is NO local-text fallback for
    the self-hosted pool: page images go to the configured endpoint as-is, so
    in self-hosted mode the served model must be vision-capable (a text-only
    server rejects the request and the error propagates normally).
    """
    _reset_last()
    route, s = _require_route(feature, settings)
    client = _with_timeout(_client_for(route, s), timeout)

    import base64

    def call() -> Any:
        if route.provider == "anthropic":
            content: list[dict] = []
            for label, data in images:
                content.append({"type": "text", "text": label})
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.b64encode(data).decode(),
                        },
                    }
                )
            content.append({"type": "text", "text": prompt})
            text = _anthropic_call(
                client,
                route.model,
                _schema_hint(system, schema),
                [{"role": "user", "content": content}],
                max_tokens,
            )
        elif route.provider == "openai":
            parts: list[dict] = []
            for label, data in images:
                parts.append({"type": "input_text", "text": label})
                parts.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{media_type};base64,"
                        + base64.b64encode(data).decode(),
                    }
                )
            parts.append({"type": "input_text", "text": prompt})
            text = _openai_call(
                client,
                route.model,
                system,
                [{"role": "user", "content": parts}],
                max_tokens,
                schema,
                schema_name,
                True,
            )
        else:
            chat_parts: list[dict] = []
            for label, data in images:
                chat_parts.append({"type": "text", "text": label})
                chat_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,"
                            + base64.b64encode(data).decode()
                        },
                    }
                )
            chat_parts.append({"type": "text", "text": prompt})
            text = _self_hosted_call(
                client,
                route.model,
                system,
                [{"role": "user", "content": chat_parts}],
                max_tokens,
                schema,
                schema_name,
                True,
            )
        return parse_json_loose(text)

    request = {
        "op": "images_json", "system": system, "prompt": prompt, "images": images,
        "media_type": media_type, "schema": schema, "schema_name": schema_name,
        "max_tokens": max_tokens,
    }
    return _guarded(feature, route, s, call, request=request)


def _tool_output_item(out: dict) -> dict:
    """One function_call_output input item. The API takes a string output;
    dict/list outputs are JSON-encoded so callers can pass tool results as
    data."""
    call_id = str(out.get("call_id") or "").strip()
    if not call_id:
        raise ValueError("Every tool output needs the call_id of the tool call it answers.")
    output = out.get("output", "")
    if not isinstance(output, str):
        output = json.dumps(output, default=str)
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def _parse_tool_calls(resp: Any) -> list[dict]:
    """The function_call items of a Responses result as
    [{call_id, name, arguments: dict}], in output order."""
    calls: list[dict] = []
    for item in _get(resp, "output") or []:
        if _get(item, "type") != "function_call":
            continue
        raw = _get(item, "arguments")
        if isinstance(raw, dict):
            args: Any = raw
        else:
            try:
                args = json.loads(raw or "{}", strict=False)
            except (TypeError, json.JSONDecodeError) as exc:
                raise LlmBadOutput(
                    "Model returned a tool call with invalid arguments. Retry the generation."
                ) from exc
        if not isinstance(args, dict):
            raise LlmBadOutput(
                "Model returned a tool call with invalid arguments. Retry the generation."
            )
        call_id = str(_get(item, "call_id") or "").strip()
        if not call_id:
            # Unanswerable: a tool output must name the call it answers.
            raise LlmBadOutput(
                "Model returned a tool call without a call id. Retry the generation."
            )
        calls.append(
            {
                "call_id": call_id,
                "name": str(_get(item, "name") or ""),
                "arguments": args,
            }
        )
    return calls


# Incomplete reasons a retry of the same request will not fix.
_PERMANENT_INCOMPLETE_REASONS = frozenset({"max_output_tokens", "content_filter"})


def _refusal_text(resp: Any) -> str | None:
    """The model's refusal, when a Responses message carries a refusal
    content item (structured outputs answer a refusal that way, with an
    empty output_text), else None."""
    for item in _get(resp, "output") or []:
        if _get(item, "type") != "message":
            continue
        for content in _get(item, "content") or []:
            if _get(content, "type") == "refusal":
                return str(_get(content, "refusal") or "").strip() or "refused"
    return None


def _raise_if_unusable_parts(resp: Any) -> None:
    """complete_parts' version of _raise_if_incomplete: records usage (an
    incomplete or refused response still bills), then raises
    LlmBadOutputPermanent for a cut-off at the output limit, a content
    filter stop or a refusal, and plain LlmBadOutput (retried) for any
    other incomplete reason."""
    _record_usage(_openai_usage(resp))
    if getattr(resp, "status", None) == "incomplete":
        reason = getattr(getattr(resp, "incomplete_details", None), "reason", None) or "unknown"
        if reason == "max_output_tokens":
            raise LlmBadOutputPermanent(
                "Model response was cut off (max_output_tokens). Retrying the same "
                "request will not help; raise the output token limit and run it again."
            )
        if reason in _PERMANENT_INCOMPLETE_REASONS:
            raise LlmBadOutputPermanent(
                f"Model response was stopped ({reason}). Retrying the same request "
                "will not help; change the request and run it again."
            )
        raise LlmBadOutput(f"Model response was cut off ({reason}). Retry the generation.")
    if _refusal_text(resp) is not None:
        raise LlmBadOutputPermanent(
            "The AI model declined to answer this request. Retrying the same "
            "request will not help; change the request and run it again."
        )


def complete_parts(
    feature: str,
    *,
    system: str,
    parts: list[dict],
    schema: dict | None = None,
    schema_name: str = "response",
    reasoning_effort: str | None = None,
    tools: list[dict] | None = None,
    previous_response_id: str | None = None,
    tool_outputs: list[dict] | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
    settings: Settings | None = None,
) -> LlmResult:
    """One OpenAI Responses call with mixed content parts (AI Estimating).

    `parts` are already in Responses shape and go out in order as one user
    message: {"type": "input_text", "text"}, {"type": "input_image",
    "image_url": data URL, "detail": "original" | "high" | "low"},
    {"type": "input_file", "filename", "file_data": data URL}.

    Tool loop: pass `tools` (Responses function tool definitions); the
    result's tool_calls lists what the model asked for. Answer with
    `tool_outputs=[{"call_id", "output"}]` plus `previous_response_id=` the
    earlier result's response_id: the input is then the function_call_output
    items (followed by a user message with `parts` when parts is not empty,
    which is how rendered page images reach the model after a read_pages
    tool call). `previous_response_id` without tool outputs continues a
    conversation with a new user message.

    With `schema` the answer uses strict structured outputs and `parsed`
    holds the JSON object (None when the model answered with tool calls).
    A cut-off answer raises LlmBadOutput like every other transport; a
    cut-off at the output token limit, a content filter stop and a refusal
    raise LlmBadOutputPermanent instead (bad_input, not retried), because
    resending the same large request would fail the same way at full cost.

    reasoning_effort defaults to the step route's effort for the plans_*
    features (PLANS_READER_ROUTES, a null route effort inheriting
    PLANS_READER_REASONING_EFFORT; a feature without a step, plans_modify,
    uses PLANS_READER_REASONING_EFFORT), so every AI Estimating call
    carries the configured effort without each caller passing it.

    Inside a queue job the lease is renewed right before the request (after
    gate admission), so render time spent before the call and the gate wait
    do not eat into the window the call itself needs. A lost lease (the
    attempt is stale and another worker may own the job) refuses to send.

    OpenAI provider only: an anthropic or self-hosted route raises
    LlmNotConfigured (and the settings validator refuses to boot with an
    anthropic route on sub, escalation, chat, or main while
    PLANS_READER_CONSOLIDATOR_MODE is legacy, the steps this transport
    serves). The client timeout defaults to
    PLANS_READER_CALL_TIMEOUT_SECONDS (a 240k-token group can take minutes)
    and the SDK's own retries are off: the job queue retries transient
    failures, and one attempt has to fit inside the queue lease.
    """
    _reset_last()
    base = llm_target.apply(settings or get_settings())
    route = resolve(feature, base)
    if route.provider != "openai":
        raise LlmNotConfigured(
            f"The '{feature}' feature needs the OpenAI provider for mixed content "
            f"calls; it is routed to {route.provider}."
        )
    route, s = _require_route(feature, base)
    if tool_outputs is not None and not previous_response_id:
        raise ValueError("Tool outputs need the previous_response_id of the call that asked for them.")
    if not tool_outputs and not parts:
        raise ValueError("An AI request needs at least one content part.")

    input_: list[dict] = []
    if tool_outputs:
        input_.extend(_tool_output_item(o) for o in tool_outputs)
    if parts:
        input_.append({"role": "user", "content": list(parts)})

    kwargs: dict[str, Any] = {"model": route.model, "instructions": system, "input": input_}
    if previous_response_id:
        kwargs["previous_response_id"] = previous_response_id
    if reasoning_effort is None and _FEATURES[feature].openai_only:
        # The step's route effort (spec 4.2), else the run-wide default
        # (plans_modify has no route step).
        reasoning_effort = route.effort or s.plans_reader_reasoning_effort
    if reasoning_effort:
        kwargs["reasoning"] = {"effort": reasoning_effort}
    if tools:
        kwargs["tools"] = list(tools)
    if max_tokens is not None:
        kwargs["max_output_tokens"] = max_tokens
    if schema is not None:
        kwargs["text"] = {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema,
                "strict": True,
            }
        }

    effective_timeout = float(
        timeout if timeout is not None else s.plans_reader_call_timeout_seconds
    )
    client = _client_for(route, s).with_options(timeout=effective_timeout, max_retries=0)

    def call() -> LlmResult:
        resp = _send_responses(client, kwargs)
        _raise_if_unusable_parts(resp)
        usage = _openai_usage(resp)
        text = getattr(resp, "output_text", None) or ""
        tool_calls = _parse_tool_calls(resp)
        parsed: Any = None
        if schema is not None and not tool_calls:
            parsed = parse_json_loose(text)
        return LlmResult(
            parsed=parsed,
            text=text,
            usage=usage or LlmUsage(),
            response_id=str(getattr(resp, "id", None) or ""),
            tool_calls=tool_calls,
            model=route.model,
            cost_usd=llm_pricing.cost_for(route.model, usage) if usage is not None else None,
        )

    return _guarded(feature, route, s, call, request={"op": "parts", **kwargs})


# ── complete_turn: the provider-neutral tool-loop transport (WS2) ───────────


def renew_lease_or_raise() -> None:
    """Renew the queue lease of the job running on this thread right before
    a request goes out (a no-op outside a queue job). A lost lease refuses
    to send: the attempt is stale and another worker may own the job."""
    # Late import keeps the queue (and its Supabase client) out of this
    # module's import graph.
    from app.services import llm_queue

    if not llm_queue.renew_lease():
        raise LlmLeaseLost(
            "This AI Estimating attempt lost its queue lease (another worker "
            "may own the job now), so the request was not sent."
        )


def _send_responses(client: Any, kwargs: dict) -> Any:
    """One Responses API request with the lease renewed first (shared by
    complete_parts and the OpenAI side of complete_turn)."""
    renew_lease_or_raise()
    return client.responses.create(**kwargs)


def _responses_tools(req: llm_parts.ModelRequest) -> list[dict]:
    return [
        {
            "type": "function",
            "name": t.name,
            "description": t.description,
            "parameters": t.schema,
            "strict": True,
        }
        for t in req.tools_offered
    ]


def _responses_input(req: llm_parts.ModelRequest) -> list[dict]:
    """The Responses `input` for a neutral transcript. With a
    previous_response_id only the turns after the last AssistantTurn go
    out (tool outputs, then a user message with any images the tools
    delivered and the extra parts). Without one the whole transcript is
    replayed: user messages, function_call items for every tool call and
    their outputs (reasoning items are not replayed), which costs a full
    uncached resend and is the resume path when the server no longer holds
    the previous response."""
    turns: list[llm_parts.Turn] = list(req.messages)
    if req.previous_response_id:
        last_assistant = max(
            (i for i, t in enumerate(turns) if isinstance(t, llm_parts.AssistantTurn)), default=-1
        )
        turns = turns[last_assistant + 1 :]
    items: list[dict] = []
    for turn in turns:
        if isinstance(turn, llm_parts.UserTurn):
            items.append({"role": "user", "content": llm_parts.to_responses_parts(turn.parts)})
        elif isinstance(turn, llm_parts.AssistantTurn):
            for call in turn.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.call_id,
                        "name": call.name,
                        "arguments": json.dumps(call.args, default=str),
                    }
                )
            if turn.text and not turn.tool_calls:
                items.append(
                    {"role": "assistant", "content": [{"type": "output_text", "text": turn.text}]}
                )
        elif isinstance(turn, llm_parts.ToolResultsTurn):
            follow: list[llm_parts.Part] = []
            for result in turn.results:
                items.append(_tool_output_item({"call_id": result.call_id, "output": result.text}))
                if result.images:
                    follow.append(llm_parts.TextPart(text=f"Images for call {result.call_id}:"))
                    follow.extend(result.images)
            follow.extend(turn.extra_parts)
            if follow:
                items.append({"role": "user", "content": llm_parts.to_responses_parts(follow)})
    if not items:
        raise ValueError("An AI request needs at least one content part.")
    return items


def _responses_kwargs(req: llm_parts.ModelRequest, *, model: str, effort: str | None) -> dict:
    """The Responses request of a ModelRequest (pure; snapshot-tested)."""
    kwargs: dict[str, Any] = {
        "model": model,
        "instructions": req.system,
        "input": _responses_input(req),
    }
    if req.previous_response_id:
        kwargs["previous_response_id"] = req.previous_response_id
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    tools = _responses_tools(req)
    if tools:
        kwargs["tools"] = tools
    kwargs["max_output_tokens"] = req.max_tokens
    if req.response_schema is not None:
        kwargs["text"] = {
            "format": {
                "type": "json_schema",
                "name": req.schema_name,
                "schema": req.response_schema,
                "strict": True,
            }
        }
    if req.cache_key:
        kwargs["prompt_cache_key"] = req.cache_key
    return kwargs


def _openai_turn(client: Any, kwargs: dict, req: llm_parts.ModelRequest, route: Route) -> llm_parts.ModelTurn:
    """Send one Responses request and read it as a ModelTurn. A cut-off at
    the output limit is reported as stop_reason max_tokens (the loop turns
    it into a forced final round); a content filter stop or a refusal is
    LlmBadOutputPermanent; any other incomplete reason is retryable."""
    resp = _send_responses(client, kwargs)
    usage = _openai_usage(resp)
    _record_usage(usage)
    stop = "final"
    if getattr(resp, "status", None) == "incomplete":
        reason = getattr(getattr(resp, "incomplete_details", None), "reason", None) or "unknown"
        if reason == "max_output_tokens":
            stop = "max_tokens"
        elif reason in _PERMANENT_INCOMPLETE_REASONS:
            raise LlmBadOutputPermanent(
                f"Model response was stopped ({reason}). Retrying the same request "
                "will not help; change the request and run it again."
            )
        else:
            raise LlmBadOutput(f"Model response was cut off ({reason}). Retry the generation.")
    if _refusal_text(resp) is not None:
        raise LlmBadOutputPermanent(
            "The AI model declined to answer this request. Retrying the same "
            "request will not help; change the request and run it again."
        )
    text = getattr(resp, "output_text", None) or ""
    tool_calls = llm_parts.tool_call_dicts(_parse_tool_calls(resp))
    if tool_calls and stop == "final":
        stop = "tool_use"
    parsed: Any = None
    if req.response_schema is not None and stop == "final":
        parsed = parse_json_loose(text)
    return llm_parts.ModelTurn(
        text=text,
        parsed=parsed,
        tool_calls=tool_calls,
        usage=usage or LlmUsage(),
        cost_usd=llm_pricing.cost_for(route.model, usage) if usage is not None else None,
        response_id=str(getattr(resp, "id", None) or ""),
        stop_reason=stop,
        provider="openai",
        model=route.model,
        raw_assistant=(),
    )


def complete_turn(
    feature: str,
    req: llm_parts.ModelRequest,
    *,
    settings: Settings | None = None,
) -> llm_parts.ModelTurn:
    """One model turn of the AI Estimating tool loop on the provider the
    feature's step routes to (docs/AI_ESTIMATING_BUILD_SPEC.md 4.3).

    The route comes from `req.route` (a run's snapshotted step route) when
    given, else from the feature's step (`req.step` overrides the feature's
    default step, e.g. escalation on plans_sub), else today's OpenAI route.
    Effort: `req.effort`, else the route's, else PLANS_READER_REASONING_EFFORT.
    The request is checked against the provider's image and payload limits
    before anything is sent (LlmBadOutputPermanent: the planner must make
    that unreachable). Both transports run inside _guarded (gate, logging,
    caps, ledger guards) and renew the queue lease right before sending.
    A self-hosted route refuses (plan sets never go to the box)."""
    _reset_last()
    base = llm_target.apply(settings or get_settings())
    spec = _FEATURES[feature]
    if spec.openai_only:
        snapshot = None
        if req.route is not None:
            if not req.step and spec.route_step is None:
                raise ValueError("A snapshotted route needs the step it belongs to (ModelRequest.step).")
            snapshot = {req.step or spec.route_step: req.route}
        route = _plans_route(spec, base, step=req.step, snapshot=snapshot)
    else:
        route = resolve(feature, base)
    if route.provider == "self_hosted":
        raise LlmNotConfigured(
            f"The '{feature}' feature needs a 3rd-party provider for tool-loop "
            "calls; it is routed to the self-hosted pool."
        )
    s = base
    if not route.api_key:
        var = "ANTHROPIC_API_KEY" if route.provider == "anthropic" else "OPENAI_API_KEY"
        raise LlmNotConfigured(f"{var} is not configured on the server.")
    if not route.model:
        raise LlmNotConfigured(f"No model is configured for the '{feature}' feature.")
    limits = llm_parts.check_limits(req.messages, route.provider)
    if not limits.ok:
        raise LlmBadOutputPermanent(
            f"This request is too large for the {route.provider} route: {limits.problem()}"
        )
    effort = req.effort or route.effort or s.plans_reader_reasoning_effort
    effective_timeout = float(
        req.timeout if req.timeout is not None else s.plans_reader_call_timeout_seconds
    )
    client = _client_for(route, s).with_options(timeout=effective_timeout, max_retries=0)

    if route.provider == "openai":
        kwargs = _responses_kwargs(req, model=route.model, effort=effort)

        def call() -> llm_parts.ModelTurn:
            return _openai_turn(client, kwargs, req, route)

    else:
        # Late import: llm_anthropic imports this module for the usage and
        # error types, so the transport is loaded on first use.
        from app.services import llm_anthropic

        def call() -> llm_parts.ModelTurn:
            return llm_anthropic.anthropic_turn(
                client,
                req,
                model=route.model,
                effort=effort,
                refusal_fallback=bool(s.anthropic_plans_refusal_fallback),
            )

    request = {"op": "turn", "req": req, "effort": effort}
    return _guarded(feature, route, s, call, request=request)


class _ExtractBudgetReached(Exception):
    """Internal signal: abort pypdf extraction mid-page once a budget is hit."""


def _pdf_to_text(pdf_bytes: bytes) -> str:
    import time

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    parts: list[str] = []
    total = 0
    deadline = time.monotonic() + _PDF_EXTRACT_MAX_SECONDS
    for page in reader.pages[:_PDF_MAX_PAGES]:
        buf: list[str] = []

        def visit(text: str, cm, tm, font_dict, font_size) -> None:  # noqa: ANN001
            nonlocal total
            if total >= _PDF_TEXT_MAX_CHARS or time.monotonic() > deadline:
                raise _ExtractBudgetReached
            buf.append(text)
            total += len(text)

        try:
            page.extract_text(visitor_text=visit)
        except _ExtractBudgetReached:
            parts.append("".join(buf))
            break
        parts.append("".join(buf))
        if total >= _PDF_TEXT_MAX_CHARS or time.monotonic() > deadline:
            break
    return "\n".join(parts)[:_PDF_TEXT_MAX_CHARS]

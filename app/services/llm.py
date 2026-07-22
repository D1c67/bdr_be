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
  own graceful fallback) — it never re-routes.

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
"""

from __future__ import annotations

import io
import json
import logging
import ssl
from dataclasses import dataclass
from typing import Any, Callable

import httpx
import openai

from app.core.config import Settings, get_settings

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


@dataclass(frozen=True)
class _Feature:
    third_party: str  # "anthropic" | "openai"
    tp_model: Callable[[Settings], str]
    sh_model: Callable[[Settings], str]


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
    "email_match": _Feature(
        "openai",
        lambda s: s.openai_email_match_model,
        lambda s: s.self_hosted_email_match_model,
    ),
}


@dataclass(frozen=True)
class Route:
    provider: str  # "anthropic" | "openai" | "self_hosted"
    model: str
    api_key: str
    base_url: str = ""


def resolve(feature: str, settings: Settings | None = None) -> Route:
    """Resolve a feature to (provider, model, credentials) from the env."""
    s = settings or get_settings()
    spec = _FEATURES[feature]
    if s.full_self_hosted_llms_enabled:
        return Route(
            provider="self_hosted",
            model=spec.sh_model(s),
            api_key=s.self_hosted_llm_api_key,
            base_url=s.self_hosted_llm_base_url,
        )
    if spec.third_party == "anthropic":
        return Route(provider="anthropic", model=spec.tp_model(s), api_key=s.anthropic_api_key)
    return Route(provider="openai", model=spec.tp_model(s), api_key=s.openai_api_key)


def is_configured(feature: str, settings: Settings | None = None) -> bool:
    """True when the ACTIVE pool can serve this feature. Callers gate on this
    exactly as they used to gate on the vendor API key being present."""
    route = resolve(feature, settings)
    if route.provider == "self_hosted":
        return bool(route.base_url and route.model)
    return bool(route.api_key and route.model)


def active_model(feature: str, settings: Settings | None = None) -> str:
    """Model label for DB records / user-facing errors, provider-tagged when
    self-hosted so stored rows show where an answer actually came from."""
    route = resolve(feature, settings)
    if route.provider == "self_hosted":
        return f"self-hosted:{route.model or '(no model set)'}"
    return route.model


def _require_route(feature: str, settings: Settings | None) -> tuple[Route, Settings]:
    s = settings or get_settings()
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
                f"this feature (set SELF_HOSTED_{feature.upper()}_MODEL)."
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
    if route.provider == "anthropic":
        key: tuple = ("anthropic", route.api_key)
    elif route.provider == "openai":
        key = ("openai", route.api_key)
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

        return Anthropic(api_key=route.api_key)
    if route.provider == "openai":
        return openai.OpenAI(api_key=route.api_key)

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
        raise ValueError("Model response was not valid JSON — retry generation.") from exc


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


# ── Transports ───────────────────────────────────────────────────────────────


def _anthropic_call(
    client: Any, model: str, system: str, messages: list[dict], max_tokens: int | None
) -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens or _DEFAULT_MAX_TOKENS,
        # Cache the (often large, reused) system prompt across re-runs/refines.
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=messages,
    )
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
    if getattr(resp, "status", None) == "incomplete":
        reason = getattr(getattr(resp, "incomplete_details", None), "reason", "unknown")
        raise ValueError(f"Model response was cut off ({reason}) — retry generation.")
    return resp.output_text or ""


def _merge_same_role(messages: list[dict]) -> list[dict]:
    """Fold consecutive same-role turns into one. Anthropic does this server-
    side (so e.g. the BOQ refine of a failed run — [user, user] when there is
    no prior result — works there), but self-hosted chat templates with strict
    role alternation (Mistral-family under vLLM/TGI) reject it with a 400."""
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
    choice = resp.choices[0]
    if json_output and getattr(choice, "finish_reason", None) == "length":
        raise ValueError("Model response was cut off (token limit) — retry generation.")
    return choice.message.content or ""


# ── Public operations ────────────────────────────────────────────────────────


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
    route, s = _require_route(feature, settings)
    client = _with_timeout(_client_for(route, s), timeout)
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
    route, s = _require_route(feature, settings)
    client = _with_timeout(_client_for(route, s), timeout)
    if route.provider == "anthropic":
        text = _anthropic_call(client, route.model, system, messages, max_tokens)
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
    route, s = _require_route(feature, settings)
    client = _with_timeout(_client_for(route, s), timeout)
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

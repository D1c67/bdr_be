"""Classify LLM failures and turn them into user-facing messages.

Every failed LLM call lands here twice:
- classify(exc) buckets the exception into a stable error kind. The queue
  uses the kind to decide retry vs fail-fast (is_transient_kind), and the
  dev AI monitor aggregates failures by kind.
- user_message(exc, model) renders the kind as text a non-engineer can act
  on. This is what gets stored on domain rows, job rows, and the call log;
  raw exception text is never stored for provider/transport errors because
  SDK messages can carry the endpoint URL or response bodies.

The model label comes from llm.active_model(), which prefixes
"self-hosted:" when the self-hosted pool is active; messages use that to
talk about "the local AI server" vs "the AI provider". Non-LLM queue jobs
(the RFP Ingestion sandbox) pass a plain label such as "sandbox"; every
branch tolerates it, no branch dereferences the label beyond a prefix test.

Non-LLM jobs on the same queue can declare their own kind instead of relying
on the LLM-shaped heuristics below: an exception class (or instance) carrying
`llm_error_kind = "<one of the kinds here>"` is bucketed as that kind before
any other test runs. rfp_ingest uses it for RfpIngestTransient
(infrastructure: a storage or spawn hiccup worth retrying) and
RfpIngestPermanent (bad_input). The infrastructure kind is the one kind whose
user_message is the exception's own text, because that text is app-authored
by rfp_ingest (protocol.VERDICT_MESSAGES), never a child's output.
"""

from typing import Any

import httpx
import openai

# ── Error kinds ──────────────────────────────────────────────────────────

KIND_UNREACHABLE = "unreachable"        # connection refused / DNS / TLS
KIND_TIMEOUT = "timeout"                # request deadline hit
KIND_OVERLOADED = "overloaded"          # gate admission timed out, or 503/529
KIND_RATE_LIMITED = "rate_limited"      # provider 429 that is not a quota problem
KIND_SERVER_ERROR = "server_error"      # provider 5xx
KIND_OUT_OF_TOKENS = "out_of_tokens"    # account has no API credits left
KIND_NOT_CONFIGURED = "not_configured"  # missing key / URL / model env
KIND_UNAUTHORIZED = "unauthorized"      # provider rejected the credentials
KIND_BAD_INPUT = "bad_input"            # this input can never work (scanned PDF, ...)
KIND_INVALID_OUTPUT = "invalid_output"  # model answered, but unusably
KIND_INFRASTRUCTURE = "infrastructure"  # non-LLM job: storage / spawn / disk hiccup
KIND_UNKNOWN = "unknown"

_ALL_KINDS = frozenset(
    {
        KIND_UNREACHABLE,
        KIND_TIMEOUT,
        KIND_OVERLOADED,
        KIND_RATE_LIMITED,
        KIND_SERVER_ERROR,
        KIND_OUT_OF_TOKENS,
        KIND_NOT_CONFIGURED,
        KIND_UNAUTHORIZED,
        KIND_BAD_INPUT,
        KIND_INVALID_OUTPUT,
        KIND_INFRASTRUCTURE,
        KIND_UNKNOWN,
    }
)

# Kinds worth retrying automatically. unknown is treated as transient so a
# never-seen-before failure gets the retry schedule rather than an instant
# terminal failure; if it keeps happening it fails after the last attempt
# anyway. invalid_output is transient because regeneration usually fixes it.
# infrastructure is transient by definition (the raiser asserts the input is
# fine and only the environment failed).
_TRANSIENT_KINDS = frozenset(
    {
        KIND_UNREACHABLE,
        KIND_TIMEOUT,
        KIND_OVERLOADED,
        KIND_RATE_LIMITED,
        KIND_SERVER_ERROR,
        KIND_INVALID_OUTPUT,
        KIND_INFRASTRUCTURE,
        KIND_UNKNOWN,
    }
)

# The attribute a non-LLM exception class sets to declare its own kind, and
# the class-name fallback for the one raiser that predates the attribute.
_DECLARED_KIND_ATTR = "llm_error_kind"
_TRANSIENT_CLASS_NAMES = frozenset({"RfpIngestTransient"})
# str(exc) is stored for the infrastructure kind only; cap it like every
# other stored message (llm_queue._ERROR_MAX_CHARS).
_INFRASTRUCTURE_MESSAGE_MAX_CHARS = 500

_QUOTA_MARKERS = (
    "credit balance is too low",
    "insufficient_quota",
    "exceeded your current quota",
    "spend limit",
    "billing hard limit",
    "insufficient credits",
)


def is_out_of_tokens(exc: Exception) -> bool:
    """True if the provider rejected the call because the account has no
    API tokens/credits left (not a transient rate limit)."""
    if getattr(exc, "code", None) == "insufficient_quota":
        return True
    text = str(exc).lower()
    body: Any = getattr(exc, "body", None)  # both SDKs attach the parsed error body
    if body is not None:
        text += " " + str(body).lower()
    return any(marker in text for marker in _QUOTA_MARKERS)


def _is_timeout(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, (openai.APITimeoutError, httpx.TimeoutException, TimeoutError)):
        return True
    try:
        import anthropic

        if isinstance(exc, anthropic.APITimeoutError):
            return True
    except ImportError:  # pragma: no cover - anthropic is a normal dependency
        pass
    return False


def _is_connection_error(exc: BaseException) -> bool:
    if isinstance(exc, (openai.APIConnectionError, httpx.TransportError, ConnectionError)):
        return True
    try:
        import anthropic

        if isinstance(exc, anthropic.APIConnectionError):
            return True
    except ImportError:  # pragma: no cover
        pass
    return False


def declared_kind(exc: BaseException) -> str | None:
    """The kind an exception declares for itself via `llm_error_kind`, or by
    class name for RfpIngestTransient; None when it declares nothing valid.
    Only a known kind is honored, so a typo in a raiser degrades to the
    heuristics below instead of inventing a kind the queue cannot act on."""
    kind = getattr(exc, _DECLARED_KIND_ATTR, None)
    if isinstance(kind, str) and kind in _ALL_KINDS:
        return kind
    if type(exc).__name__ in _TRANSIENT_CLASS_NAMES:
        return KIND_INFRASTRUCTURE
    return None


def classify(exc: Exception) -> str:
    """Bucket an exception from an LLM call into a stable error kind."""
    # Late imports: llm/llm_gate import this module at load time.
    from app.services.llm import LlmBadOutput, LlmNotConfigured, SelfHostedUnreachable
    from app.services.llm_gate import LlmBusy

    declared = declared_kind(exc)
    if declared is not None:
        return declared
    if is_out_of_tokens(exc):
        return KIND_OUT_OF_TOKENS
    if isinstance(exc, LlmNotConfigured):
        return KIND_NOT_CONFIGURED
    if isinstance(exc, LlmBusy):
        return KIND_OVERLOADED
    if isinstance(exc, LlmBadOutput):
        return KIND_INVALID_OUTPUT
    if isinstance(exc, SelfHostedUnreachable):
        return KIND_TIMEOUT if _is_timeout(exc.__cause__) else KIND_UNREACHABLE
    if _is_timeout(exc):
        return KIND_TIMEOUT
    if _is_connection_error(exc):
        return KIND_UNREACHABLE
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in (503, 529):
            return KIND_OVERLOADED
        if status == 429:
            return KIND_RATE_LIMITED
        if status >= 500:
            return KIND_SERVER_ERROR
        if status in (401, 403):
            return KIND_UNAUTHORIZED
        if 400 <= status < 500:
            return KIND_BAD_INPUT
    if isinstance(exc, ValueError):
        return KIND_BAD_INPUT
    return KIND_UNKNOWN


def is_transient_kind(kind: str) -> bool:
    """True when an automatic retry is worth attempting for this kind."""
    return kind in _TRANSIENT_KINDS


def is_transient(exc: Exception) -> bool:
    return is_transient_kind(classify(exc))


# ── User-facing messages ─────────────────────────────────────────────────


def _service_name(model: str) -> str:
    return "the local AI server" if model.startswith("self-hosted:") else "the AI provider"


def user_message(exc: Exception, model: str) -> str:
    """User-facing error text for a failed LLM call against `model`.

    `model` is only ever prefix-tested and interpolated, so a non-LLM label
    such as "sandbox" (the RFP Ingestion queue jobs) is tolerated by every
    branch.
    """
    kind = classify(exc)
    service = _service_name(model)
    if kind == KIND_INFRASTRUCTURE:
        # The one kind whose raw text is stored: the raiser (rfp_ingest) writes
        # it for users from protocol.VERDICT_MESSAGES, and nothing from a
        # sandbox child or a filename ever reaches the message.
        return str(exc)[:_INFRASTRUCTURE_MESSAGE_MAX_CHARS]
    if kind == KIND_OUT_OF_TOKENS:
        return (
            f"The AI service is out of API tokens for the {model} model. "
            "Please contact your IT Director about getting more API tokens "
            "for this model."
        )
    if kind == KIND_UNREACHABLE:
        return (
            f"Could not connect to {service}. Check that it is running and "
            "reachable, or contact your IT Director."
        )
    if kind == KIND_TIMEOUT:
        return (
            f"The request to {service} timed out before the model finished. "
            "It may be overloaded or the document may be very large."
        )
    if kind == KIND_OVERLOADED:
        return (
            "The AI service is handling too many requests right now. "
            "Please try again in a moment."
        )
    if kind == KIND_RATE_LIMITED:
        return (
            f"Requests to {service} are being rate limited. "
            "Please try again shortly."
        )
    if kind == KIND_SERVER_ERROR:
        return (
            f"{_service_name(model).capitalize()} returned an internal error "
            f"for the {model} model. Please try again; if it keeps failing, "
            "contact your IT Director."
        )
    if kind == KIND_UNAUTHORIZED:
        return (
            f"{_service_name(model).capitalize()} rejected the server's "
            "credentials. Contact your IT Director to check the API key."
        )
    if kind == KIND_INVALID_OUTPUT:
        # The specific reason (cut off, invalid JSON, missing field) is
        # already written for users by the raiser (LlmBadOutput, app-authored).
        return str(exc)
    if kind == KIND_BAD_INPUT:
        if isinstance(exc, ValueError):
            # App-authored loader/validator messages (file too large, scanned
            # PDF, file deleted) are written for users; pass them through.
            return str(exc)
        # SDK 4xx: never quote the provider's body (it can carry the endpoint
        # URL, request echoes, or other internals).
        status = getattr(exc, "status_code", None)
        suffix = f" (HTTP {status})" if isinstance(status, int) else ""
        return (
            f"{_service_name(model).capitalize()} rejected the request{suffix}. "
            f"The input may be too large or unsupported for the {model} model. "
            "Contact your IT Director if this persists."
        )
    if kind == KIND_UNKNOWN:
        # Unrecognized exception type: the raw text is not trusted to be
        # user-safe. The full exception is logged server-side by the caller.
        return (
            "Something unexpected went wrong while running this AI task. "
            "Please try again; if it keeps failing, contact your IT Director."
        )
    # not_configured: LlmNotConfigured messages are app-authored.
    return str(exc)

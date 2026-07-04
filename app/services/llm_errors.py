"""Map LLM provider errors to user-facing messages.

The one failure users can act on is the account running out of API tokens:
Anthropic reports it as "credit balance is too low" (or a spend-limit 429),
OpenAI as an "insufficient_quota" 429. Detect those and tell the user to
contact their IT Director; every other error surfaces unchanged.
"""

from typing import Any

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


def user_message(exc: Exception, model: str) -> str:
    """User-facing error text for a failed LLM call against `model`."""
    if is_out_of_tokens(exc):
        return (
            f"The AI service is out of API tokens for the {model} model. "
            "Please contact your IT Director about getting more API tokens "
            "for this model."
        )
    return str(exc)

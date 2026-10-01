"""llm_errors: out-of-tokens detection, the IT Director message, and the
self-classifying kinds the non-LLM queue jobs use.

The RFP Ingestion sandbox runs on the LLM queue but never calls a model, so
its failures cannot be bucketed by the SDK-shaped heuristics. Pinned here:

  * an exception carrying `llm_error_kind` is bucketed as that kind before
    any heuristic runs (class attribute or instance attribute), a typo'd kind
    is ignored, and the class name RfpIngestTransient is a fallback;
  * `infrastructure` is transient, and it is the ONE kind whose user_message
    is the raiser's own text (app-authored), capped at 500 characters;
  * every user_message branch tolerates a non-LLM model label ("sandbox").
"""

import pytest

from app.services import llm_errors


class _FakeApiError(Exception):
    def __init__(self, message: str, code: str | None = None, body=None):
        super().__init__(message)
        self.code = code
        self.body = body


class _StatusError(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class _Transient(RuntimeError):
    llm_error_kind = "infrastructure"


class _Permanent(ValueError):
    llm_error_kind = "bad_input"


def test_anthropic_credit_exhaustion_detected():
    exc = _FakeApiError(
        "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
        "'message': 'Your credit balance is too low to access the Anthropic API.'}}"
    )
    assert llm_errors.is_out_of_tokens(exc)


def test_openai_insufficient_quota_code_detected():
    exc = _FakeApiError("Error code: 429", code="insufficient_quota")
    assert llm_errors.is_out_of_tokens(exc)


def test_openai_quota_message_detected():
    exc = _FakeApiError(
        "You exceeded your current quota, please check your plan and billing details."
    )
    assert llm_errors.is_out_of_tokens(exc)


def test_quota_marker_in_body_detected():
    exc = _FakeApiError(
        "Error code: 429",
        body={"error": {"message": "Your credit balance is too low", "type": "error"}},
    )
    assert llm_errors.is_out_of_tokens(exc)


def test_plain_rate_limit_not_treated_as_out_of_tokens():
    exc = _FakeApiError(
        "Error code: 429 - {'error': {'type': 'rate_limit_error', "
        "'message': 'Number of requests has exceeded your per-minute rate limit.'}}"
    )
    assert not llm_errors.is_out_of_tokens(exc)


def test_user_message_names_model_and_it_director():
    exc = _FakeApiError("Your credit balance is too low")
    msg = llm_errors.user_message(exc, "claude-opus-4-8")
    assert "claude-opus-4-8" in msg
    assert "IT Director" in msg
    assert "API tokens" in msg


def test_user_message_generic_for_unrecognized_errors():
    # Unrecognized exception types classify as "unknown"; their raw text is
    # not trusted to be user-safe (SDK errors can carry the endpoint URL), so
    # the message is a fixed generic one and the raw exception is only logged.
    exc = _FakeApiError("Model response did not match the expected schema.")
    msg = llm_errors.user_message(exc, "claude-opus-4-8")
    assert "Model response" not in msg
    assert "Something unexpected went wrong" in msg
    assert "IT Director" in msg


# ── Self-classifying kinds (non-LLM queue jobs) ──────────────────────────


def test_infrastructure_is_a_transient_kind():
    assert llm_errors.KIND_INFRASTRUCTURE == "infrastructure"
    assert llm_errors.is_transient_kind(llm_errors.KIND_INFRASTRUCTURE)
    # The existing policy is untouched.
    assert not llm_errors.is_transient_kind(llm_errors.KIND_BAD_INPUT)


def test_declared_kind_wins_over_the_heuristics():
    assert llm_errors.classify(_Transient("storage hiccup")) == "infrastructure"
    # A ValueError would be bad_input anyway; the declaration makes it explicit.
    assert llm_errors.classify(_Permanent("The PDF has no pages.")) == "bad_input"
    # A RuntimeError is normally "unknown"; a declaration on the INSTANCE
    # is honored too.
    exc = RuntimeError("x")
    exc.llm_error_kind = "infrastructure"
    assert llm_errors.classify(exc) == "infrastructure"
    # Even text that would otherwise trip the quota markers defers to it.
    assert llm_errors.classify(_Permanent("credit balance is too low")) == "bad_input"


def test_a_typoed_declared_kind_falls_back_to_the_heuristics():
    class _Typo(RuntimeError):
        llm_error_kind = "banana"

    class _NotAString(ValueError):
        llm_error_kind = 7

    assert llm_errors.declared_kind(_Typo("x")) is None
    assert llm_errors.classify(_Typo("x")) == "unknown"
    assert llm_errors.classify(_NotAString("x")) == "bad_input"


def test_rfp_ingest_transient_is_recognized_by_class_name_alone():
    class RfpIngestTransient(RuntimeError):
        pass

    assert llm_errors.classify(RfpIngestTransient("spawn failed")) == "infrastructure"
    assert llm_errors.is_transient(RfpIngestTransient("spawn failed"))


def test_infrastructure_message_is_the_raisers_text_capped_at_500():
    text = "A storage operation failed; retry the run."
    assert llm_errors.user_message(_Transient(text), "sandbox") == text
    long = "x" * 600
    assert llm_errors.user_message(_Transient(long), "sandbox") == "x" * 500
    # Only this kind gets the pass-through: an undeclared RuntimeError with
    # the same text is still the generic message.
    assert "Something unexpected" in llm_errors.user_message(RuntimeError(text), "sandbox")


@pytest.mark.parametrize(
    "exc",
    [
        _StatusError("slow down", 429),
        _StatusError("unavailable", 503),
        _StatusError("boom", 500),
        _StatusError("bad key", 401),
        _StatusError("bad request", 400),
        _FakeApiError("Error code: 429", code="insufficient_quota"),
        RuntimeError("???"),
        ValueError("The PDF has no pages."),
        _Transient("A storage operation failed; retry the run."),
        _Permanent("The file is not a PDF."),
    ],
)
def test_user_message_tolerates_a_non_llm_model_label(exc):
    msg = llm_errors.user_message(exc, "sandbox")
    assert isinstance(msg, str) and msg
    assert "self-hosted" not in msg and "local AI server" not in msg

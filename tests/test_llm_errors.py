"""llm_errors: out-of-tokens detection and the IT Director message."""

from app.services import llm_errors


class _FakeApiError(Exception):
    def __init__(self, message: str, code: str | None = None, body=None):
        super().__init__(message)
        self.code = code
        self.body = body


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

"""Tool-less mail extraction reserves before calling and never falls back raw."""
from decimal import Decimal

import pytest

from app.assistant import llm_messages, spend
from app.services import assistant_mail as mail
from app.services import assistant_mail_review as review

OWNER = "11111111-1111-4111-8111-111111111111"
RUN = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def calls(monkeypatch):
    calls = []
    def reserve(user_id, **kwargs):
        calls.append(("reserve", user_id, kwargs))
        return "hold"
    monkeypatch.setattr(spend, "reserve", reserve)
    monkeypatch.setattr(spend, "settle", lambda *args: calls.append(("settle", args)))
    return calls


def test_extraction_is_bounded_toolless_and_cost_attributed(calls, monkeypatch):
    def complete(feature, **kwargs):
        calls.append(("model", feature, kwargs))
        attribution = llm_messages.current_attribution()
        assert attribution.user_id == OWNER and attribution.run_id == RUN
        assert attribution.reservation_id == "hold"
        assert "tools" not in kwargs
        assert len(kwargs["messages"][0]["content"]) <= 16000
        return {"summary": "Summary" + chr(0x2014) + "of the email"}
    monkeypatch.setattr(review.llm, "complete_json", complete)
    result = review.extract(OWNER, RUN, [{"body": "untrusted data" * 10000}])
    assert result == "Summary,of the email"
    assert calls[0][0] == "reserve" and calls[0][2]["amount"] == Decimal("0.25")
    assert calls[1][0] == "model" and calls[2] == ("settle", (OWNER, "hold"))
    assert llm_messages.current_attribution() is None


@pytest.mark.parametrize("value", [None, [], {"summary": None}, {"summary": 4}, RuntimeError("private provider data")])
def test_extraction_failure_settles_without_raw_fallback(calls, monkeypatch, value):
    def complete(*args, **kwargs):
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(review.llm, "complete_json", complete)
    with pytest.raises(mail.MailError) as error:
        review.extract(OWNER, RUN, [{"body": "secret email body"}])
    assert "secret" not in str(error.value) and "private" not in str(error.value)
    assert calls[-1] == ("settle", (OWNER, "hold"))
    assert llm_messages.current_attribution() is None


def test_reservation_refusal_makes_no_model_call(calls, monkeypatch):
    def refuse(*args, **kwargs):
        raise mail.MailError("cap_reached")
    monkeypatch.setattr(spend, "reserve", refuse)
    monkeypatch.setattr(review.llm, "complete_json", lambda *a, **k: pytest.fail("No call without a reservation"))
    with pytest.raises(mail.MailError):
        review.extract(OWNER, RUN, [{"body": "secret"}])
    assert calls == []

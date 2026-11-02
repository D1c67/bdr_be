"""whats_due keeps its lists when they are empty (docs/ASSISTANT.md, Skills
and tools). Found in the 2026-10-22 browser pass: with nothing due, the
trimmer's compact() dropped due_today and the model saw only
{"scope": "today"}, which it reported as "I could not read today's due list"
instead of "nothing is due today"."""

from __future__ import annotations

import json

from app.assistant import registry
from app.assistant.registry import TrimContext

PID = "549a5065-f894-4df1-9abd-5f83e846467f"


def _ctx(fmt="concise") -> TrimContext:
    return TrimContext(response_format=fmt, limit=20, short=lambda v: str(v)[:8])


def _trim(payloads, args, fmt="concise"):
    spec = registry.get("whats_due")
    assert spec is not None
    return spec.response_trimmer(payloads, args, _ctx(fmt))


def test_nothing_due_today_is_an_explicit_empty_list():
    for fmt in ("concise", "detailed"):
        out = _trim([[], []], {"scope": "today"}, fmt)
        assert out == {"scope": "today", "due_today": []}
        assert json.loads(json.dumps(out))["due_today"] == []


def test_nothing_due_or_overdue_keeps_both_lists():
    out = _trim([[], []], {"scope": "overdue_and_today"})
    assert out == {"scope": "overdue_and_today", "due_today": [], "overdue_or_sent_today": []}


def test_missing_payloads_still_answer_with_empty_lists():
    assert _trim([], {})["due_today"] == []
    assert _trim([None, None], {"scope": "bogus"}) == {"scope": "today", "due_today": []}


def test_rows_and_hints_are_unchanged_when_something_is_due():
    row = {"project": {"id": PID, "number": "26.6.7118", "name": "Boutique Motel",
                       "internal_bid_at": "2026-10-22T16:40:00+00:00", "current_stage": "markup"},
           "checks": {"has_quotes": True, "has_labor": False, "has_markups": True},
           "severity": "warn"}
    out = _trim([[row] * 3, []], {"scope": "today", "limit": 2}, "concise")
    assert out["scope"] == "today"
    assert len(out["due_today"]) == 3  # ctx.limit is 20 here; args limit is applied by the executor
    first = out["due_today"][0]
    assert first["number"] == "26.6.7118" and first["missing"] == ["labor"] and "ready" not in first
    assert "due_today_hint" not in out
    many = _trim([[row] * 25, []], {"scope": "today"})
    assert len(many["due_today"]) == 20 and many["due_today_hint"].startswith("20 of 25 shown.")

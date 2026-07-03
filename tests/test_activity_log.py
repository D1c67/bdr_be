"""The Analytics → Activity feed (analytics_metrics.activity_log): merging
audit_log with stage_events, resolving actor/project labels, sorting newest-first
and paginating. A fake Supabase returns canned rows per table (the date filters
are exercised live; here we pin the merge/label/sort/paginate logic)."""

from types import SimpleNamespace

import pytest

from app.services import analytics_metrics as m


class _Q:
    def __init__(self, data):
        self._data = data

    def select(self, *a, **k):
        return self

    def gte(self, *a, **k):
        return self

    def lte(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def in_(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return SimpleNamespace(data=self._data)


class _SB:
    def __init__(self, tables):
        self._tables = tables

    def table(self, name):
        return _Q(self._tables.get(name, []))


_TABLES = {
    "audit_log": [
        {
            "id": "a1",
            "actor_id": "u1",
            "action": "labor.review",
            "entity": "project",
            "entity_id": "p1",
            "payload": {"verified": True},
            "created_at": "2026-06-20T10:00:00+00:00",
        }
    ],
    "stage_events": [
        {
            "id": "e1",
            "project_id": "p1",
            "from_stage": "markup",
            "to_stage": "gc_pricing",
            "note": None,
            "actor_id": "u2",
            "entered_at": "2026-06-21T10:00:00+00:00",
        }
    ],
    "profiles": [
        {"id": "u1", "full_name": "Alice", "role": "estimating_engineer"},
        {"id": "u2", "full_name": "Bob", "role": "executive"},
    ],
    "projects": [{"id": "p1", "number": "42", "name": "Acme"}],
}


@pytest.fixture(autouse=True)
def _patch_sb(monkeypatch):
    monkeypatch.setattr(m, "get_supabase", lambda: _SB(_TABLES))


def test_merges_audit_and_stage_events_newest_first():
    out = m.activity_log("year", None, None, None, None, None, 50, 0)
    assert out["total"] == 2
    assert not out["truncated"]

    # Newest first: the 6/21 stage advance precedes the 6/20 labor review.
    first, second = out["rows"]
    assert first["action"] == "stage.advance"
    assert first["action_label"] == "Advanced stage"
    assert first["actor_name"] == "Bob" and first["actor_role"] == "executive"
    assert first["project_label"] == "42 — Acme"
    assert first["detail"] == {"from": "markup", "to": "gc_pricing", "note": None}

    assert second["action"] == "labor.review"
    assert second["action_label"] == "Set labor numbers"  # friendly label resolved
    assert second["actor_name"] == "Alice" and second["actor_role"] == "estimating_engineer"
    assert second["project_label"] == "42 — Acme"


def test_available_actions_lists_present_actions():
    out = m.activity_log("year", None, None, None, None, None, 50, 0)
    values = {a["value"] for a in out["available_actions"]}
    assert values == {"labor.review", "stage.advance"}


def test_action_filter_for_stage_advance_skips_audit():
    out = m.activity_log("year", None, None, None, "stage.advance", None, 50, 0)
    assert out["total"] == 1
    assert out["rows"][0]["action"] == "stage.advance"


def test_pagination_offsets_into_the_merged_list():
    page2 = m.activity_log("year", None, None, None, None, None, 1, 1)
    assert page2["total"] == 2
    assert len(page2["rows"]) == 1
    assert page2["rows"][0]["action"] == "labor.review"  # the second (older) row

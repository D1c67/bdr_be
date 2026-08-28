"""Estimating Engineer (Labor) dashboard - row assembly over a filtering fake
Supabase: quotes-complete detection (last quoted category, missing categories,
the General Material estimate fallback), labor first-save from the audit log,
markup-lane completion, sent-out status classification, actual-date delta
redaction, and open-event filtering to labor-focused engineers."""

from types import SimpleNamespace

from app.services import labor_engineer_report as ler


class _Q:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def in_(self, col, vals):
        allowed = set(vals)
        self._rows = [r for r in self._rows if r.get(col) in allowed]
        return self

    def execute(self):
        return SimpleNamespace(data=[dict(r) for r in self._rows])


class _SB:
    def __init__(self, tables):
        self._tables = tables

    def table(self, name):
        return _Q(self._tables.get(name, []))


_PROJECTS = [
    {
        # Fully quoted, sent one minute before the internal date: on_time.
        "id": "p1", "number": "0101", "name": "Fire Station 88",
        "internal_bid_at": "2026-08-10T21:00:00+00:00",
        "actual_bid_at": "2026-08-11T21:00:00+00:00",
        "current_stage": "submitted",
    },
    {
        # Missing a quote in one category, never sent.
        "id": "p2", "number": "0102", "name": "Rec Center",
        "internal_bid_at": "2026-08-12T19:00:00+00:00",
        "actual_bid_at": None,
        "current_stage": "markup",
    },
    {
        # Sent after the actual bid date (and no proposal_sends row: the
        # 'submitted' stage event is the fallback timestamp).
        "id": "p3", "number": "0103", "name": "Parking Garage",
        "internal_bid_at": "2026-08-05T19:00:00+00:00",
        "actual_bid_at": "2026-08-06T19:00:00+00:00",
        "current_stage": "bid_outcome",
    },
]

_TABLES = {
    "material_categories": [
        {"id": "gm", "name": "General Material", "is_general": True},
        {"id": "sw", "name": "Switchgear", "is_general": False},
        {"id": "lt", "name": "Lighting", "is_general": False},
    ],
    "rfqs": [
        {"id": "r1", "project_id": "p1", "material_category_id": "gm"},
        {"id": "r2", "project_id": "p1", "material_category_id": "sw"},
        {"id": "r3", "project_id": "p2", "material_category_id": "sw"},
        {"id": "r4", "project_id": "p2", "material_category_id": "lt"},
        {"id": "r5", "project_id": "p3", "material_category_id": "sw"},
    ],
    "quotes": [
        # p1: GM's materialized quote row is LATER than the estimate row below;
        # the estimate's created_at must win. Switchgear's first of two quotes
        # arrives last of the categories, so it is the "last quoted" one.
        {"rfq_id": "r1", "received_at": "2026-08-09T00:00:00+00:00"},
        {"rfq_id": "r2", "received_at": "2026-08-08T12:00:00+00:00"},
        {"rfq_id": "r2", "received_at": "2026-08-09T09:00:00+00:00"},
        # p2: Switchgear quoted, Lighting missing.
        {"rfq_id": "r3", "received_at": "2026-08-11T00:00:00+00:00"},
        # p3: quoted.
        {"rfq_id": "r5", "received_at": "2026-08-04T00:00:00+00:00"},
    ],
    "general_material_estimates": [
        {"project_id": "p1", "created_at": "2026-08-07T15:00:00+00:00"},
    ],
    "audit_log": [
        {"action": "labor.review", "entity": "project", "entity_id": "p1",
         "created_at": "2026-08-09T18:00:00+00:00"},
        {"action": "labor.review", "entity": "project", "entity_id": "p1",
         "created_at": "2026-08-09T10:00:00+00:00"},
        # A different action against p1 must not count.
        {"action": "markup.set", "entity": "project", "entity_id": "p1",
         "created_at": "2026-08-09T08:00:00+00:00"},
    ],
    "project_category_state": [
        {"project_id": "p1", "category": "markup", "status": "complete",
         "completed_at": "2026-08-10T02:00:00+00:00"},
        {"project_id": "p2", "category": "markup", "status": "active",
         "completed_at": None},
    ],
    "proposal_sends": [
        {"project_id": "p1", "status": "sent", "sent_at": "2026-08-10T20:59:00+00:00"},
        {"project_id": "p1", "status": "sent", "sent_at": "2026-08-10T21:30:00+00:00"},
        {"project_id": "p1", "status": "failed", "sent_at": None},
    ],
    "stage_events": [
        {"project_id": "p1", "to_stage": "submitted",
         "entered_at": "2026-08-10T21:40:00+00:00"},
        {"project_id": "p3", "to_stage": "submitted",
         "entered_at": "2026-08-06T21:00:00+00:00"},
        {"project_id": "p3", "to_stage": "markup",
         "entered_at": "2026-08-01T00:00:00+00:00"},
    ],
    "project_open_events": [
        {"project_id": "p1", "user_id": "lab", "kind": "project",
         "opened_at": "2026-08-08T16:00:00+00:00"},
        {"project_id": "p1", "user_id": "lab", "kind": "project",
         "opened_at": "2026-08-09T16:00:00+00:00"},
        {"project_id": "p1", "user_id": "lab", "kind": "details",
         "opened_at": "2026-08-09T16:05:00+00:00"},
        # An executive's opens must not count toward the engineer columns.
        {"project_id": "p1", "user_id": "exec", "kind": "project",
         "opened_at": "2026-08-07T16:00:00+00:00"},
    ],
    "profiles": [
        {"id": "lab", "role": "estimating_engineer_labor"},
        {"id": "exec", "role": "executive"},
    ],
}


def _rows(role="estimating_engineer_labor", projects=_PROJECTS):
    rows = ler.build_rows(_SB(_TABLES), projects, role)
    return {r["project_id"]: r for r in rows}


def test_quotes_complete_uses_last_first_quote_and_gm_estimate():
    r = _rows()["p1"]
    # GM's first figure is the estimate row (08-07), Switchgear's first quote
    # is 08-08 12:00 - the LATER of the two firsts completes the set.
    assert r["quotes_complete_at"] == "2026-08-08T12:00:00+00:00"
    assert r["last_quoted_category"] == "Switchgear"
    assert r["missing_quote_categories"] == []
    assert r["category_count"] == 2


def test_missing_category_leaves_quotes_incomplete():
    r = _rows()["p2"]
    assert r["quotes_complete_at"] is None
    assert r["missing_quote_categories"] == ["Lighting"]
    assert r["last_quoted_category"] == "Switchgear"  # latest quoted so far


def test_labor_first_save_is_earliest_labor_review_audit():
    rows = _rows()
    assert rows["p1"]["labor_first_saved_at"] == "2026-08-09T10:00:00+00:00"
    assert rows["p2"]["labor_first_saved_at"] is None


def test_markup_completion_only_from_complete_lane():
    rows = _rows()
    assert rows["p1"]["markup_completed_at"] == "2026-08-10T02:00:00+00:00"
    assert rows["p2"]["markup_completed_at"] is None


def test_sent_status_on_time_and_first_transmission_wins():
    r = _rows()["p1"]
    assert r["sent_out_at"] == "2026-08-10T20:59:00+00:00"
    assert r["sent_status"] == "on_time"
    assert r["sent_delta_seconds"] == -60  # one minute early


def test_not_sent_and_stage_event_fallback_after_actual():
    rows = _rows()
    assert rows["p2"]["sent_status"] == "not_sent"
    assert rows["p2"]["sent_out_at"] is None
    # p3 has no proposal_sends rows; the submitted stage event dates the send,
    # which lands after the actual bid date.
    assert rows["p3"]["sent_out_at"] == "2026-08-06T21:00:00+00:00"
    assert rows["p3"]["sent_status"] == "after_actual"


def test_actual_delta_redacted_for_non_viewer_roles():
    engineer = _rows()["p3"]
    assert engineer["sent_after_actual_seconds"] is None  # confidential date
    assert engineer["sent_delta_seconds"] is not None  # internal delta stays
    executive = _rows(role="executive")["p3"]
    assert executive["sent_after_actual_seconds"] == 2 * 3600


def test_opens_count_only_labor_focused_engineers():
    r = _rows()["p1"]
    assert r["first_opened_at"] == "2026-08-08T16:00:00+00:00"
    assert r["project_open_count"] == 2  # the executive's open is excluded
    assert r["details_opened"] is True
    assert r["details_open_count"] == 1
    r2 = _rows()["p2"]
    assert r2["first_opened_at"] is None
    assert r2["details_opened"] is False


def test_search_sanitizer_strips_filter_syntax():
    assert ler._sanitize_search("fire, station (88)") == "fire  station  88"
    assert ler._sanitize_search("%_*\\") == ""


def test_open_kind_validated():
    import pytest

    from app.models.schemas import ProjectOpenIn

    assert ProjectOpenIn(kind="project").kind == "project"
    assert ProjectOpenIn(kind="details").kind == "details"
    with pytest.raises(ValueError):
        ProjectOpenIn(kind="dashboard")

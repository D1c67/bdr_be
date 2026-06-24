"""Pure-logic tests for analytics metrics — ranges, on-time classification, the
send-out benchmark redaction, and trend bucketing. No DB: send_out runs entirely
off a hand-built WindowData, and the helpers are pure."""

from datetime import datetime, timezone

import pytest

from app.core.roles import Role
from app.services import analytics_metrics as m

UTC = timezone.utc


def _dt(y, mo, d, h=0):
    return datetime(y, mo, d, h, tzinfo=UTC)


# ── range resolution ──────────────────────────────────────────────────────


def test_resolve_named_range_is_rolling_and_aware():
    a, b = m.resolve_range("week", None, None)
    assert a.tzinfo is not None and b.tzinfo is not None
    assert round((b - a).total_seconds() / 86400) == 7


def test_resolve_custom_makes_to_date_inclusive():
    a, b = m.resolve_range("custom", "2026-06-01", "2026-06-30")
    assert a == _dt(2026, 6, 1)
    # End-of-day: the whole 30th counts, so the bound is the 1st of July midnight.
    assert b == _dt(2026, 7, 1)


def test_resolve_custom_requires_both_bounds():
    with pytest.raises(ValueError):
        m.resolve_range("custom", "2026-06-01", None)


def test_resolve_unknown_range_raises():
    with pytest.raises(ValueError):
        m.resolve_range("decade", None, None)


# ── pure helpers ──────────────────────────────────────────────────────────


def test_parse_coerces_naive_dates_to_utc():
    assert m._parse("2026-06-22").tzinfo is not None
    assert m._parse("2026-06-22T10:00:00+00:00") == _dt(2026, 6, 22, 10)
    assert m._parse(None) is None


def test_eod_pushes_date_only_to_next_midnight_but_keeps_times():
    assert m._eod("2026-06-22") == _dt(2026, 6, 23)
    assert m._eod("2026-06-22T15:00:00+00:00") == _dt(2026, 6, 22, 15)


def test_on_time_classification():
    assert m._on_time(_dt(2026, 6, 10), _dt(2026, 6, 11)) == "on_time"
    assert m._on_time(_dt(2026, 6, 12), _dt(2026, 6, 11)) == "late"
    assert m._on_time(None, _dt(2026, 6, 11)) is None
    assert m._on_time(_dt(2026, 6, 12), None) is None


def test_build_trend_fills_empty_buckets():
    df, dt = _dt(2026, 6, 1), _dt(2026, 6, 8)
    pairs = [(_dt(2026, 6, 2), 1.0), (_dt(2026, 6, 2), 1.0), (_dt(2026, 6, 5), 3.0)]
    trend = m._build_trend(pairs, df, dt)
    assert len(trend) == 7  # one bucket per day, gaps filled with 0
    by_day = {t["bucket_start"][:10]: t["value"] for t in trend}
    assert by_day["2026-06-02"] == 2.0
    assert by_day["2026-06-05"] == 3.0
    assert by_day["2026-06-03"] == 0.0


# ── send_out: on-time + benchmark redaction (no DB) ────────────────────────


def _send_out_window():
    projects = {
        # submitted before its internal date (on time), but after its actual date (late).
        "p1": {
            "id": "p1", "number": "100", "name": "A", "current_stage": "submitted",
            "internal_bid_at": "2026-06-10T17:00:00+00:00",
            "actual_bid_at": "2026-06-09T17:00:00+00:00",
        },
        # submitted after both dates (late either way).
        "p2": {
            "id": "p2", "number": "101", "name": "B", "current_stage": "submitted",
            "internal_bid_at": "2026-06-05T17:00:00+00:00",
            "actual_bid_at": "2026-06-05T17:00:00+00:00",
        },
        # no internal date at all → no_benchmark.
        "p3": {
            "id": "p3", "number": "102", "name": "C", "current_stage": "submitted",
            "internal_bid_at": None, "actual_bid_at": None,
        },
    }
    w = m.WindowData(_dt(2026, 6, 1), _dt(2026, 6, 22), None, projects)
    w.submitted_at = {
        "p1": _dt(2026, 6, 10, 12),
        "p2": _dt(2026, 6, 6, 12),
        "p3": _dt(2026, 6, 15, 12),
    }
    return w


def test_send_out_internal_benchmark_classifies():
    w = _send_out_window()
    out = m.send_out(w, "month", "internal_bid_at", Role.PM)
    assert out["total"] == 3
    assert out["on_time"] == 1  # p1
    assert out["late"] == 1  # p2
    assert out["no_benchmark"] == 1  # p3
    assert out["benchmark"] == "internal_bid_at"
    assert out["benchmark_redacted"] is False


def test_send_out_redacts_actual_for_non_viewer():
    w = _send_out_window()
    out = m.send_out(w, "month", "actual_bid_at", Role.PM)  # PM cannot see actual
    assert out["benchmark"] == "internal_bid_at"
    assert out["benchmark_redacted"] is True
    # Falls back to internal classification (p1 on time), and never leaks an actual date.
    assert out["on_time"] == 1
    p1 = next(r for r in out["projects"] if r["project_id"] == "p1")
    assert p1["benchmark_at"] == "2026-06-10T17:00:00+00:00"


def test_send_out_actual_benchmark_for_viewer():
    w = _send_out_window()
    out = m.send_out(w, "month", "actual_bid_at", Role.EXECUTIVE)  # may see actual
    assert out["benchmark"] == "actual_bid_at"
    assert out["benchmark_redacted"] is False
    # p1 submitted 6/10 > actual 6/9 → late; p2 late too. None on time.
    assert out["on_time"] == 0
    assert out["late"] == 2


def test_send_out_excludes_projects_submitted_outside_window():
    w = _send_out_window()
    w.submitted_at["p2"] = _dt(2026, 5, 1, 12)  # before window start
    out = m.send_out(w, "month", "internal_bid_at", Role.PM)
    assert {r["project_id"] for r in out["projects"]} == {"p1", "p3"}

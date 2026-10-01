"""GET /analytics/calling-in (docs/CALLING_IN.md section 6).

Pinned here: the same RangeParam (`?range=`) as the other windowed analytics
routes, the custom range through `start`/`end` (the contract) or
`date_from`/`date_to` (what the analytics tabs already send), 400 on a bad
custom range, the internal-roles gate, and the end-to-end math over the
loader: call rate, median / average hours to call, missed vs open, attempts,
no actual bid dates in the payload. Each slot is anchored on when its window
opened (pre_bid: the GC's sent_at, post_bid: T), so a bid tomorrow shows its
pre_bid slots; the go-live marker (call_in_meta) drops missed slots whose
window closed before it, and a missing marker reads as now.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.services import analytics_metrics as metrics
from app.services import calling_in as ci
from tests.calling_in_fake import FakeSB

UTC = timezone.utc
PT = ci.PT


def pt(y, mo, d, h=0, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=PT).astimezone(UTC)


NOW = pt(2026, 9, 30, 12)
T1 = pt(2026, 9, 15, 14)  # List 2 over (closed 9/25 14:00)
T2 = pt(2026, 9, 28, 10)  # List 2 open (day 2)
T_OLD = pt(2026, 6, 1, 10)  # outside the month
GO_LIVE = pt(2025, 1, 1)  # call_in_meta.started_at for the fixture


def _send(pid, gc, sent_at):
    return {"id": f"ps-{pid}-{gc}", "project_id": pid, "gc_id": gc, "gc_name": gc.upper(),
            "status": "sent", "sent_at": sent_at.isoformat(), "sent_via": "email",
            "material_amount": "1", "labor_amount": "1"}


def _call(cid, pid, gc, round_, outcome, at, by="u1", names=("Pat",)):
    return {"id": cid, "project_id": pid, "gc_id": gc, "round": round_, "outcome": outcome,
            "note": "n", "contacts": [{"gc_contact_id": None, "name": n} for n in names],
            "called_at": at.isoformat(), "created_by": by, "updated_at": at.isoformat()}


def _project(pid, name, actual, **kw):
    return {"id": pid, "name": name, "number": pid[-3:], "actual_bid_at":
            actual.isoformat() if actual else None, "abandoned_at": None,
            "current_stage": "submitted", "test_session_id": None, **kw}


@pytest.fixture
def db(monkeypatch):
    sent1 = T1 - timedelta(days=5)
    sent2 = T2 - timedelta(days=3)
    database = FakeSB({
        "projects": [
            _project("p-001", "One", T1),
            _project("p-002", "Two", T2),
            _project("p-003", "Old", T_OLD),
            _project("p-004", "Abandoned", T1, abandoned_at=T1.isoformat()),
            _project("p-005", "No date", None),
        ],
        "proposal_sends": [
            _send("p-001", "a", sent1), _send("p-001", "b", sent1),
            _send("p-002", "a", sent2),
            _send("p-003", "a", T_OLD - timedelta(days=2)),
            _send("p-004", "a", sent1),
            _send("p-005", "a", pt(2026, 9, 20, 9)),
        ],
        "call_in_calls": [
            # p-001 a: pre_bid voicemail then spoke 10 h after send; post_bid spoke 48 h after T.
            _call("c1", "p-001", "a", "pre_bid", "voicemail", sent1 + timedelta(hours=4)),
            _call("c2", "p-001", "a", "pre_bid", "spoke", sent1 + timedelta(hours=10),
                  names=("Pat", "Lee")),
            _call("c3", "p-001", "a", "post_bid", "spoke", T1 + timedelta(hours=48), by="u2"),
            # p-001 b: two post_bid attempts, never spoke (missed both rounds).
            _call("c4", "p-001", "b", "post_bid", "no_answer", T1 + timedelta(hours=30)),
            # p-002 a: pre_bid spoke 20 h after send.
            _call("c5", "p-002", "a", "pre_bid", "spoke", sent2 + timedelta(hours=20)),
            # p-003 (outside the range) and p-004 (abandoned) never count.
            _call("c6", "p-003", "a", "pre_bid", "spoke", T_OLD - timedelta(hours=1)),
            _call("c7", "p-004", "a", "pre_bid", "spoke", T1 - timedelta(hours=1)),
        ],
        "bid_outcomes": [],
        "general_contractors": [{"id": "a", "name": "Acme"}, {"id": "b", "name": "Brite"}],
        "profiles": [{"id": "u1", "full_name": "Una"}, {"id": "u2", "full_name": "Ugo"}],
        # Go-live before every window in the fixture, so history counts here.
        "call_in_meta": [{"id": True, "started_at": GO_LIVE.isoformat()}],
    })
    monkeypatch.setattr(ci, "get_supabase", lambda: database)
    monkeypatch.setattr(ci, "_now", lambda: NOW)
    return database


@pytest.fixture
def client(db, monkeypatch):
    import app.main

    who = {"role": Role.ESTIMATING_ENGINEER_LABOR}
    app.main.app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id="u9", email="x@g3.test", role=who["role"], is_active=True
    )
    monkeypatch.setattr(metrics, "utcnow", lambda: NOW)
    yield TestClient(app.main.app), who
    app.main.app.dependency_overrides.pop(get_current_user, None)


def test_month_window_math(client):
    c, _ = client
    body = c.get("/analytics/calling-in?range=month").json()
    pre, post = body["rounds"]["pre_bid"], body["rounds"]["post_bid"]
    # pre_bid slots: p-001 a (called 10 h), p-001 b (missed), p-002 a (called 20 h),
    # p-005 a (open, no date, anchored on its first send 9/20).
    assert (pre["slots"], pre["called"], pre["missed"], pre["open"]) == (4, 2, 1, 1)
    assert pre["call_rate"] == round(2 / 3, 4)
    assert pre["median_hours_to_call"] == 15.0 and pre["avg_hours_to_call"] == 15.0
    # post_bid slots: p-001 a (called 48 h), p-001 b (missed), p-002 a (open).
    assert (post["slots"], post["called"], post["missed"], post["open"]) == (3, 1, 1, 1)
    assert post["call_rate"] == 0.5
    assert post["median_hours_to_call"] == 48.0

    by_gc = {g["gc_id"]: g for g in body["by_gc"]}
    assert by_gc["a"] == {"gc_id": "a", "gc_name": "Acme", "called": 3, "missed": 0,
                          "median_hours_to_call": 20.0}
    assert by_gc["b"]["missed"] == 2 and by_gc["b"]["median_hours_to_call"] is None

    first = next(r for r in body["calls"] if r["project_id"] == "p-001" and r["round"] == "pre_bid")
    assert first["attempts_before"] == 1 and first["hours_to_call"] == 10.0
    assert first["contact_names"] == ["Pat", "Lee"] and first["called_by_name"] == "Una"
    ugo = next(r for r in body["calls"] if r["round"] == "post_bid")
    assert ugo["called_by_name"] == "Ugo" and ugo["gc_name"] == "Acme"

    assert {(m["project_id"], m["gc_id"], m["round"]) for m in body["missed"]} == {
        ("p-001", "b", "pre_bid"), ("p-001", "b", "post_bid"),
    }
    assert {r["project_id"] for r in body["calls"]} == {"p-001", "p-002"}
    assert "bid_at" not in str(body)


@pytest.mark.parametrize("params", [
    "range=custom&start=2026-09-01&end=2026-09-20",
    "range=custom&date_from=2026-09-01&date_to=2026-09-20",
])
def test_custom_range_accepts_start_end_and_date_from_to(client, params):
    c, _ = client
    body = c.get(f"/analytics/calling-in?{params}").json()
    # Only p-001 (T 9/15) and p-005 (first send 9/20) anchor inside 9/1..9/20.
    projects = {r["project_id"] for r in body["calls"]} | {m["project_id"] for m in body["missed"]}
    assert projects == {"p-001"}
    assert body["rounds"]["pre_bid"]["slots"] == 3  # p-001 a, p-001 b, p-005 a


def test_custom_range_errors(client):
    c, _ = client
    assert c.get("/analytics/calling-in?range=custom&start=2026-09-01").status_code == 400
    assert c.get(
        "/analytics/calling-in?range=custom&start=2026-09-20&end=2026-09-01"
    ).status_code == 400
    assert c.get("/analytics/calling-in?range=fortnight").status_code == 422


def test_year_window_includes_the_old_project(client):
    c, _ = client
    body = c.get("/analytics/calling-in?range=year").json()
    assert "p-003" in {r["project_id"] for r in body["calls"]}
    assert "p-004" not in {r["project_id"] for r in body["calls"]}  # abandoned never counts


def test_every_internal_role_reads_it(client):
    c, who = client
    for role in (Role.ACCOUNTANT, Role.EXECUTIVE, Role.ESTIMATING_ENGINEER_MATERIALS):
        who["role"] = role
        assert c.get("/analytics/calling-in").status_code == 200
    who["role"] = Role.ESTIMATOR
    assert c.get("/analytics/calling-in").status_code == 403


def test_empty_window(client, db):
    db.tables["proposal_sends"] = []
    c, _ = client
    body = c.get("/analytics/calling-in").json()
    assert body["rounds"]["pre_bid"] == {
        "slots": 0, "called": 0, "missed": 0, "open": 0, "call_rate": None,
        "median_hours_to_call": None, "avg_hours_to_call": None,
    }
    assert body["by_gc"] == [] and body["calls"] == [] and body["missed"] == []


def test_month_range_includes_pre_bid_slots_for_upcoming_bids(client, db):
    # Bid tomorrow (T inside the loader's one-day slack) and bid in 5 days (T
    # outside it: found through the sent_at query). Both were sent this month.
    db.tables["projects"] += [
        _project("p-006", "Tomorrow", NOW + timedelta(days=1)),
        _project("p-007", "Next week", NOW + timedelta(days=5)),
    ]
    db.tables["proposal_sends"] += [
        _send("p-006", "a", NOW - timedelta(days=2)),
        _send("p-006", "b", NOW - timedelta(days=2)),
        _send("p-007", "a", NOW - timedelta(days=3)),
    ]
    db.tables["call_in_calls"].append(
        _call("c8", "p-006", "a", "pre_bid", "spoke", NOW - timedelta(hours=6))
    )
    c, _ = client
    body = c.get("/analytics/calling-in?range=month").json()
    pre, post = body["rounds"]["pre_bid"], body["rounds"]["post_bid"]
    # The 4 fixture pre_bid slots plus p-006 a (called 42 h), p-006 b and p-007 a (open).
    assert (pre["slots"], pre["called"], pre["missed"], pre["open"]) == (7, 3, 1, 3)
    # No post_bid slot for either: those windows have not opened yet.
    assert (post["slots"], post["called"], post["missed"], post["open"]) == (3, 1, 1, 1)
    upcoming = next(r for r in body["calls"] if r["project_id"] == "p-006")
    assert upcoming["round"] == "pre_bid" and upcoming["hours_to_call"] == 42.0
    assert "bid_at" not in str(body)


def test_missed_slots_whose_window_closed_before_go_live_do_not_count(client, db):
    # Go-live 9/20: p-001 b's pre_bid window closed 9/15 (dropped), its
    # post_bid window closed 9/25 (still missed). p-001 a's pre_bid window
    # also closed 9/15 but it has a logged call, so it counts.
    db.tables["call_in_meta"] = [{"id": True, "started_at": pt(2026, 9, 20).isoformat()}]
    c, _ = client
    body = c.get("/analytics/calling-in?range=month").json()
    pre, post = body["rounds"]["pre_bid"], body["rounds"]["post_bid"]
    assert (pre["slots"], pre["called"], pre["missed"], pre["open"]) == (3, 2, 0, 1)
    assert pre["call_rate"] == 1.0
    assert (post["slots"], post["called"], post["missed"], post["open"]) == (3, 1, 1, 1)
    assert {(m["project_id"], m["gc_id"], m["round"]) for m in body["missed"]} == {
        ("p-001", "b", "post_bid"),
    }


def test_missing_meta_row_reads_as_now_so_nothing_historic_is_missed(client, db):
    db.tables["call_in_meta"] = []
    c, _ = client
    body = c.get("/analytics/calling-in?range=month").json()
    pre, post = body["rounds"]["pre_bid"], body["rounds"]["post_bid"]
    # Logged calls and open windows still count; no slot is missed.
    assert (pre["slots"], pre["called"], pre["missed"], pre["open"]) == (3, 2, 0, 1)
    assert (post["slots"], post["called"], post["missed"], post["open"]) == (2, 1, 0, 1)
    assert body["missed"] == []
    # GC b only had historic missed slots, so it drops out of the by-GC table.
    assert [g["gc_id"] for g in body["by_gc"]] == ["a"]


def test_unreadable_meta_table_reads_as_now(monkeypatch):
    from postgrest.exceptions import APIError

    class Boom:
        def table(self, name):
            raise APIError({"code": "PGRST205", "message": "no table", "details": None,
                            "hint": None})

    assert ci.load_started_at(Boom(), NOW) == NOW
    assert ci.load_started_at(FakeSB({"call_in_meta": []}), NOW) == NOW
    marker = pt(2026, 9, 30, 8)
    assert ci.load_started_at(
        FakeSB({"call_in_meta": [{"id": True, "started_at": marker.isoformat()}]}), NOW
    ) == marker

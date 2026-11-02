"""The two phase 4 recipes, estimator_vs_bid and quote_readiness
(docs/ASSISTANT_PHASE4_PLAN.md 3.10, WI-4): params, windows across DST,
membership, prices, coverage, the server-built message and the email. No
network: a filtering fake client and a fake estimate loader."""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.assistant import recipes
from app.assistant.recipes import RecipeError
from app.core.roles import ACTUAL_BID_VIEWER_ROLES, INTERNAL_ROLES, Role
from app.services import assistant_recipes as ar
from app.services import auto_send_estimate

UTC = timezone.utc
PT = ar.PT
EM = "\u2014"


def pt(*a):
    """A Pacific wall time as aware UTC."""
    return datetime(*a, tzinfo=PT).astimezone(UTC)


def _ts(value) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


# ── a filtering fake of the PostgREST builder ────────────────────────────────


class _Query:
    def __init__(self, sb, table):
        self._sb, self._table, self._filters = sb, table, []
        self._range = None

    def select(self, *a, **k):
        return self

    def _add(self, fn):
        self._filters.append(fn)
        return self

    def eq(self, col, value):
        return self._add(lambda r: str(r.get(col)) == str(value))

    def neq(self, col, value):
        return self._add(lambda r: str(r.get(col)) != str(value))

    def in_(self, col, values):
        values = {str(v) for v in values}
        return self._add(lambda r: str(r.get(col)) in values)

    def is_(self, col, value):
        assert value == "null"
        return self._add(lambda r: r.get(col) is None)

    def gte(self, col, value):
        return self._add(lambda r: r.get(col) is not None and _ts(r[col]) >= _ts(value))

    def lt(self, col, value):
        return self._add(lambda r: r.get(col) is not None and _ts(r[col]) < _ts(value))

    def order(self, *a, **k):
        return self

    def range(self, lo, hi):
        self._range = (lo, hi)
        return self

    def execute(self):
        self._sb.calls.append(self._table)
        rows = [dict(r) for r in self._sb.tables.get(self._table, []) if all(f(r) for f in self._filters)]
        if self._range:
            rows = rows[self._range[0]: self._range[1] + 1]
        return SimpleNamespace(data=rows)


class FakeSb:
    def __init__(self, **tables):
        self.tables = tables
        self.calls = []

    def table(self, name):
        return _Query(self, name)


# ── params ───────────────────────────────────────────────────────────────────


def test_catalog_entries():
    evb, qr = recipes.RECIPES["estimator_vs_bid"], recipes.RECIPES["quote_readiness"]
    assert evb.label == "Estimator vs our price" and evb.roles == ACTUAL_BID_VIEWER_ROLES and evb.accepts_period
    assert qr.label == "Key quotes for the bid week" and qr.roles == INTERNAL_ROLES and qr.accepts_week
    for key in ("due_today", "due_this_week", "missing_quotes", "labor_not_entered"):
        r = recipes.RECIPES[key]
        assert r.roles == INTERNAL_ROLES and not r.accepts_period and not r.accepts_week
    assert not evb.accepts_horizon and not evb.accepts_watch and not qr.accepts_horizon and not qr.accepts_watch


def test_validate_params_keyword_compat_and_new_params():
    # The three positional callers are unchanged.
    assert recipes.validate_params("missing_quotes", 14, None) == {"horizon_days": 14}
    assert recipes.validate_params("due_today", None, None) == {}
    # No period or week: nothing in the dict, the data side defaults.
    assert recipes.validate_params("estimator_vs_bid", None, None) == {}
    assert recipes.validate_params("quote_readiness", None, None) == {}
    assert recipes.validate_params("estimator_vs_bid", None, None, period="2026-09") == {"period": "2026-09"}
    assert recipes.validate_params("estimator_vs_bid", None, None, period=" This_Month ") == {"period": "this_month"}
    assert recipes.validate_params("estimator_vs_bid", None, None, period="last_month") == {"period": "last_month"}
    assert recipes.validate_params("quote_readiness", None, None, week="next_week") == {"week": "next_week"}
    assert recipes.validate_params("quote_readiness", None, None, week="2026-10-26") == {"week": "2026-10-26"}
    assert recipes.validate_params("quote_readiness", None, None, week="") == {}


@pytest.mark.parametrize("period", ["2026-13", "2026-00", "26-09", "1999-12", "2101-01", "september", "2026-9",
                                    "../x", 202609, True])
def test_bad_periods(period):
    with pytest.raises(RecipeError) as e:
        recipes.validate_params("estimator_vs_bid", None, None, period=period)
    assert e.value.code == "period_invalid" and EM not in str(e.value)


@pytest.mark.parametrize("week", ["2026-10-27", "2026-10-25", "monday", "10/26/2026", "2026-02-30", "last_week", 5])
def test_bad_weeks(week):
    with pytest.raises(RecipeError) as e:
        recipes.validate_params("quote_readiness", None, None, week=week)
    assert e.value.code == "week_invalid" and EM not in str(e.value)


@pytest.mark.parametrize("recipe,kwargs", [
    ("quote_readiness", {"period": "2026-09"}),
    ("estimator_vs_bid", {"week": "next_week"}),
    ("due_today", {"period": "last_month"}),
    ("missing_quotes", {"week": "this_week"}),
    ("estimator_vs_bid", {"period": "2026-09", "horizon_days": 7}),
    ("quote_readiness", {"week": "next_week", "horizon_days": 7}),
])
def test_param_the_recipe_does_not_take_is_refused(recipe, kwargs):
    horizon = kwargs.pop("horizon_days", None)
    with pytest.raises(RecipeError) as e:
        recipes.validate_params(recipe, horizon, None, **kwargs)
    assert e.value.code == "bad_params"


def test_watch_is_refused_for_the_new_recipes():
    for recipe in ("estimator_vs_bid", "quote_readiness"):
        with pytest.raises(RecipeError):
            recipes.validate_params(recipe, None, "6f1c2f8e-6a3b-4f53-9d0a-1f2e3d4c5b6a")


def test_resolve_period_and_week():
    assert recipes.resolve_period(None, date(2026, 10, 24)) == (date(2026, 9, 1), date(2026, 10, 1))
    assert recipes.resolve_period("this_month", date(2026, 10, 24)) == (date(2026, 10, 1), date(2026, 11, 1))
    assert recipes.resolve_period("last_month", date(2027, 1, 5)) == (date(2026, 12, 1), date(2027, 1, 1))
    assert recipes.resolve_period("2026-12", date(2026, 10, 24)) == (date(2026, 12, 1), date(2027, 1, 1))
    assert recipes.period_label(date(2026, 9, 1)) == "September 2026"
    assert recipes.resolve_week(None, date(2026, 10, 24)) == date(2026, 10, 26)       # Saturday: next Monday
    assert recipes.resolve_week("next_week", date(2026, 10, 26)) == date(2026, 11, 2)
    assert recipes.resolve_week("this_week", date(2026, 10, 25)) == date(2026, 10, 19)  # Sunday: the week just over
    assert recipes.resolve_week("2026-11-09", date(2026, 10, 24)) == date(2026, 11, 9)


# ── windows in Pacific time across DST ───────────────────────────────────────


def test_month_window_is_pacific_midnights_across_dst():
    at = pt(2026, 12, 3, 9)
    start, end, label = ar.window_for("estimator_vs_bid", at, None, period="2026-11")
    assert (start, end) == (pt(2026, 11, 1, 0), pt(2026, 12, 1, 0)) and label == "November 2026"
    assert start.hour == 7 and end.hour == 8          # PDT then PST: the month spans the change
    start, end, _ = ar.window_for("estimator_vs_bid", at, None, period="2027-03")
    assert (start, end) == (pt(2027, 3, 1, 0), pt(2027, 4, 1, 0)) and (start.hour, end.hour) == (8, 7)
    start, end, label = ar.window_for("estimator_vs_bid", pt(2026, 10, 24, 9), None)   # default last month
    assert (start, end, label) == (pt(2026, 9, 1, 0), pt(2026, 10, 1, 0), "September 2026")


def test_week_window_is_monday_to_saturday_midnight_pacific():
    start, end, label = ar.window_for("quote_readiness", pt(2026, 10, 24, 9), None)
    assert (start, end) == (pt(2026, 10, 26, 0), pt(2026, 10, 31, 0))
    assert label == "Monday, October 26 to Friday, October 30"
    start, end, label = ar.window_for("quote_readiness", pt(2027, 3, 10, 9), None, week="2027-03-08")
    assert (start, end) == (pt(2027, 3, 8, 0), pt(2027, 3, 13, 0)) and label.endswith("Friday, March 12")
    # The week of the November change: Monday is PDT, Saturday midnight is PST.
    start, end, _ = ar.window_for("quote_readiness", pt(2026, 10, 24, 9), None, week="2026-10-26")
    assert end - start == (pt(2026, 10, 31, 0) - pt(2026, 10, 26, 0))
    # 11:30 PM Pacific on Friday the 23rd is already Saturday UTC; "today" is still Friday.
    late = ar.window_for("quote_readiness", pt(2026, 10, 23, 23, 30), None, week="this_week")
    assert late[0] == pt(2026, 10, 19, 0)


# ── estimator_vs_bid ─────────────────────────────────────────────────────────

P = {n: f"00000000-0000-4000-8000-{n:012d}" for n in range(1, 40)}


def _project(n, actual, internal=None, stage="rfqs", **extra):
    return {"id": P[n], "name": f"Tower <{n}>", "number": f"26.9.{7000 + n}",
            "actual_bid_at": actual.isoformat() if actual else None,
            "internal_bid_at": (internal or actual).isoformat() if (internal or actual) else None,
            "current_stage": stage, "abandoned_at": None, "failed_to_bid_at": None, "test_session_id": None, **extra}


def _send(n, gc, amount, *, sent_at=None, status="sent", labor="0"):
    return {"project_id": P[n], "gc_id": gc, "status": status,
            "sent_at": (sent_at or pt(2026, 9, 10, 15)).isoformat(),
            "material_amount": str(amount), "gear_amount": None, "underground_amount": None,
            "low_voltage_amount": None, "labor_amount": labor}


def _estimate(base, *, round_=2, uploaded=None, name="Ed Estimator", warnings=()):
    return {"ok": True, "code": None, "message": None, "base": base, "round": round_, "estimator_name": name,
            "uploaded_at": (uploaded or pt(2026, 9, 5, 9)).isoformat(), "warnings": list(warnings)}


def _evb_world():
    projects = [
        _project(1, pt(2026, 9, 10, 14)),                                   # we bid higher (lowest of two)
        _project(2, pt(2026, 9, 15, 14)),                                   # we bid lower
        _project(3, pt(2026, 9, 20, 14)),                                   # same price
        _project(4, pt(2026, 9, 12, 14), stage="pm_only"),                  # excluded
        _project(5, pt(2026, 9, 13, 14), test_session_id="sess-1"),         # excluded
        _project(6, pt(2026, 9, 28, 14), internal=pt(2026, 10, 2, 14)),     # sent early, due October
        _project(7, pt(2026, 8, 31, 23, 30)),                               # August, edge
        _project(8, pt(2026, 10, 1, 0, 10)),                                # October, edge
        _project(9, pt(2026, 9, 21, 14)),                                   # no price from us
        _project(10, pt(2026, 9, 22, 14)),                                  # workbook unreadable
        _project(11, pt(2026, 9, 23, 14)),                                  # estimate revised after we sent
        _project(12, pt(2026, 9, 24, 14), stage="cp_only"),                 # excluded
    ]
    sends = [
        _send(1, "g1", "1300000"), _send(1, "g2", "1250000"), _send(1, "g3", "9999999", status="failed"),
        _send(2, "g1", "450000"), _send(3, "g1", "700000.00"), _send(6, "g1", "300000"),
        _send(10, "g1", "100000"), _send(11, "g1", "800000", sent_at=pt(2026, 9, 25, 9)),
        _send(7, "g1", "1"), _send(8, "g1", "1"),
    ]
    return FakeSb(projects=projects, proposal_sends=sends)


ESTIMATES = {
    P[1]: _estimate("1000000.00"),
    P[2]: _estimate("500000.00"),
    P[3]: _estimate("700000.00"),
    P[6]: _estimate("250000.00"),
    P[9]: _estimate("333.00"),
    P[10]: {"ok": False, "code": "no_estimate", "message": "No estimate workbook was found.", "base": None},
    P[11]: _estimate("790000.00", uploaded=pt(2026, 9, 26, 9), warnings=["Alternate 1 is not in the base bid."]),
}


@pytest.fixture
def loader(monkeypatch):
    calls = []

    def load(project_id, *, sb=None):
        calls.append(project_id)
        return dict(ESTIMATES[project_id])

    monkeypatch.setattr(auto_send_estimate, "load_estimate", load)
    return calls


AT = pt(2026, 10, 24, 9)
VIEWER = Role.EXECUTIVE


def _by_number(rows):
    return {r["project"]["number"]: r["extra"] for r in rows}


def test_evb_membership_and_prices(loader):
    payload = ar.rows_for(_evb_world(), "estimator_vs_bid", at_utc=AT, role=VIEWER)
    assert payload["recipe"] == "estimator_vs_bid" and payload["watch"] is None
    assert payload["window"]["label"] == "September 2026"
    numbers = [r["project"]["number"] for r in payload["rows"]]
    assert numbers == ["26.9.7001", "26.9.7002", "26.9.7003", "26.9.7009", "26.9.7010", "26.9.7011"]
    # pm_only, cp_only, test sessions and the August and October edges never load an estimate.
    assert set(loader) == {P[n] for n in (1, 2, 3, 6, 9, 10, 11)}
    by = _by_number(payload["rows"])
    one = by["26.9.7001"]
    assert one["our_min"] == "1250000.00" and one["estimator_base"] == "1000000.00"      # min over GCs, failed send ignored
    assert one["difference"] == "250000.00" and one["difference_pct"] == "25.0"
    assert one["verdict"] == "We bid higher" and one["gc_count"] == 2 and one["code"] is None
    assert one["estimator_name"] == "Ed Estimator" and one["round"] == 2 and one["revised_after_send"] is False
    assert by["26.9.7002"]["verdict"] == "We bid lower" and by["26.9.7002"]["difference"] == "-50000.00"
    assert by["26.9.7002"]["difference_pct"] == "-10.0"
    assert by["26.9.7003"]["verdict"] == "Same price" and by["26.9.7003"]["difference"] == "0.00"
    for row in payload["rows"]:
        assert row["project"]["actual_bid_at"]          # viewers see the date


def test_evb_sent_early_is_outside_rows_and_totals(loader):
    payload = ar.rows_for(_evb_world(), "estimator_vs_bid", at_utc=AT, role=VIEWER)
    early = payload["extra"]["sent_early"]
    assert [r["project"]["number"] for r in early] == ["26.9.7006"]
    assert early[0]["extra"]["verdict"] == "We bid higher"
    assert "26.9.7006" not in [r["project"]["number"] for r in payload["rows"]]
    lfl = payload["totals"]["like_for_like"]
    assert lfl["count"] == 4 and lfl["estimator_total"] == "2990000.00" and lfl["our_total"] == "3200000.00"
    assert lfl["difference"] == "210000.00" and lfl["difference_pct"] == "7.0"


def test_evb_missing_prices_and_both_totals(loader):
    payload = ar.rows_for(_evb_world(), "estimator_vs_bid", at_utc=AT, role=VIEWER)
    by = _by_number(payload["rows"])
    none_from_us = by["26.9.7009"]
    assert none_from_us["code"] == "no_our_price" and none_from_us["verdict"] is None
    assert none_from_us["estimator_base"] == "333.00" and none_from_us["our_min"] is None
    unreadable = by["26.9.7010"]
    assert unreadable["code"] == "no_estimate" and unreadable["estimator_base"] is None
    assert unreadable["our_min"] == "100000.00" and unreadable["verdict"] is None
    totals = payload["totals"]
    # Every row counted on whichever side it has (not like-for-like).
    every = totals["all_rows"]
    assert every["count"] == 6 and "not like-for-like" in every["note"]
    assert every["estimator_total"] == str(Decimal("1000000") + 500000 + 700000 + 333 + 790000) + ".00"
    assert every["our_total"] == str(Decimal("1250000") + 450000 + 700000 + 100000 + 800000) + ".00"
    assert totals["like_for_like"]["count"] == 4        # 7001, 7002, 7003 and 7011 have both prices
    assert totals["like_for_like"]["count"] != every["count"]


def test_evb_revised_after_send_and_alternates(loader):
    by = _by_number(ar.rows_for(_evb_world(), "estimator_vs_bid", at_utc=AT, role=VIEWER)["rows"])
    revised = by["26.9.7011"]
    assert revised["revised_after_send"] is True
    assert revised["alternates_note"] and "alternates" in revised["alternates_note"]
    assert by["26.9.7001"]["alternates_note"] is None


def test_evb_not_loaded_yet_when_the_budget_runs_out(monkeypatch):
    release = threading.Event()

    def slow(project_id, *, sb=None):
        if project_id == P[2]:
            release.wait(5)
        return dict(ESTIMATES[project_id])

    monkeypatch.setattr(auto_send_estimate, "load_estimate", slow)
    monkeypatch.setattr(ar, "ESTIMATE_BUDGET_SECONDS", 0.3)
    try:
        by = _by_number(ar.rows_for(_evb_world(), "estimator_vs_bid", at_utc=AT, role=VIEWER)["rows"])
    finally:
        release.set()
    slow_row = by["26.9.7002"]
    assert slow_row["code"] == "not_loaded_yet" and slow_row["estimator_base"] is None and slow_row["verdict"] is None
    assert slow_row["message"] == "Ask again in a minute; the workbook is still being read."
    assert by["26.9.7001"]["verdict"] == "We bid higher"        # the others were read in time


# Shaped like dev's SND-01 (seeded by scratch/seed_sent_stage_dev.py): the
# assignment came back 2026-08-29 but there is no estimator_submissions row
# and no project_files row at all, and like dev's 7901 / 26.6.7118: a round 1
# submission whose estimate workbook was never sealed (estimator_deliverable
# false, submission_round null). load_estimate answers estimate_not_received
# for all of them; the estimator's price stays empty (BASE BID PRICE of a
# sealed workbook only), but the row must not say the estimate never came.

def _assignment(n, *, returned_at, revoked_at=None, aid="989d9c40-bf22-4ec9-b3c9-c645686d3279"):
    return {"id": aid, "project_id": P[n], "estimator_id": "5cfaaee6-0f1b-4f19-b9f9-7912dfc82bf3",
            "assigned_by": "5cfaaee6-0f1b-4f19-b9f9-7912dfc82bf3",
            "due_at": "2026-08-30T05:15:50.511464+00:00", "expires_at": "2026-09-28T11:15:50.511464+00:00",
            "revoked_at": revoked_at, "created_at": "2026-08-26T00:16:19.311464+00:00",
            "sent_to_estimator_at": "2026-08-26T00:16:19.311464+00:00", "returned_at": returned_at}


def _returned_world():
    projects = [
        _project(21, pt(2026, 9, 12, 8, 35)),     # SND-01: returned by assignment only, nothing on file
        _project(22, pt(2026, 9, 14, 10)),        # 7901-like: round 1 submitted, workbook never sealed
        _project(23, pt(2026, 9, 16, 10)),        # assigned, not returned yet
        _project(24, pt(2026, 9, 18, 10)),        # priced normally
    ]
    return FakeSb(
        projects=projects,
        proposal_sends=[_send(21, "g1", "410000"), _send(24, "g1", "500000")],
        estimator_assignments=[
            _assignment(21, returned_at="2026-08-29T11:15:50.511464+00:00"),
            _assignment(22, returned_at="2026-09-03T17:26:38.716554+00:00", aid="a2"),
            _assignment(23, returned_at=None, aid="a3"),
        ],
        estimator_submissions=[
            {"id": "s2", "project_id": P[22], "round": 1, "submitted_at": "2026-09-02T17:26:38.716554+00:00",
             "summary": None},
        ],
    )


@pytest.fixture
def returned_loader(monkeypatch):
    real_not_received = auto_send_estimate._failed("estimate_not_received")
    results = {P[21]: real_not_received, P[22]: real_not_received, P[23]: real_not_received,
               P[24]: _estimate("450000.00")}

    def load(project_id, *, sb=None):
        return dict(results[project_id])

    monkeypatch.setattr(auto_send_estimate, "load_estimate", load)


def test_evb_returned_without_an_estimate_file_says_when(returned_loader):
    sb = _returned_world()
    payload = ar.rows_for(sb, "estimator_vs_bid", at_utc=AT, role=VIEWER)
    by = _by_number(payload["rows"])

    snd = by["26.9.7021"]
    assert snd["code"] == "returned_without_estimate"
    assert snd["message"] == ("The estimator returned the work on 8/29/2026 but sent no estimate file, "
                              "so there is no estimator price.")
    assert snd["estimator_returned_at"] == "2026-08-29T11:15:50.511464+00:00"
    # Still no estimator price, so never compared and outside like-for-like.
    assert snd["estimator_base"] is None and snd["verdict"] is None and snd["our_min"] == "410000.00"

    legacy = by["26.9.7022"]           # the first submission wins over the assignment stamp
    assert legacy["code"] == "returned_without_estimate"
    assert legacy["estimator_returned_at"] == "2026-09-02T17:26:38.716554+00:00"
    assert "9/2/2026" in legacy["message"]

    waiting = by["26.9.7023"]          # genuinely not back yet: auto_send_estimate's own wording
    assert waiting["code"] == "estimate_not_received" and waiting["estimator_returned_at"] is None
    assert waiting["message"] == auto_send_estimate.REASON_MESSAGES["estimate_not_received"]

    priced = by["26.9.7024"]
    assert priced["verdict"] == "We bid higher" and priced["estimator_returned_at"] is None
    assert payload["totals"]["like_for_like"]["count"] == 1

    content, blocks = recipes.message_for("estimator_vs_bid", payload)
    [table] = [b for b in blocks if b["type"] == "table"]
    assert table["rows"][0][2] == "No price" and table["rows"][0][5] == snd["message"]   # fits the 120-char cell
    assert EM not in json.dumps([payload, content, blocks])


def test_evb_reads_the_estimator_tables_only_for_unreceived_estimates(loader):
    sb = _evb_world()
    ar.rows_for(sb, "estimator_vs_bid", at_utc=AT, role=VIEWER)
    assert "estimator_submissions" not in sb.calls and "estimator_assignments" not in sb.calls


def test_evb_refuses_roles_outside_the_viewers(loader):
    for role in (Role.ESTIMATING_ENGINEER_LABOR, Role.ESTIMATING_ENGINEER_MATERIALS, None, "estimator"):
        with pytest.raises(RecipeError) as e:
            ar.rows_for(_evb_world(), "estimator_vs_bid", at_utc=AT, role=role)
        assert e.value.code == "recipe_unavailable"
    assert loader == []
    for role in ACTUAL_BID_VIEWER_ROLES:
        assert ar.rows_for(_evb_world(), "estimator_vs_bid", at_utc=AT, role=role)["rows"]


def test_evb_period_param_and_empty_month(loader):
    payload = ar.rows_for(_evb_world(), "estimator_vs_bid", at_utc=AT, period="2026-08", role=VIEWER)
    assert payload["window"]["label"] == "August 2026"
    assert [r["project"]["number"] for r in payload["rows"]] == ["26.9.7007"]
    empty = ar.rows_for(_evb_world(), "estimator_vs_bid", at_utc=AT, period="2025-01", role=VIEWER)
    assert empty["rows"] == [] and empty["totals"]["like_for_like"]["count"] == 0
    with pytest.raises(RecipeError):
        ar.rows_for(_evb_world(), "estimator_vs_bid", at_utc=AT, period="2026-13", role=VIEWER)


def test_evb_message_is_counts_only_and_the_table_has_the_plan_columns(loader):
    payload = ar.rows_for(_evb_world(), "estimator_vs_bid", at_utc=AT, role=VIEWER)
    content, blocks = recipes.message_for("estimator_vs_bid", payload)
    # 7001 and 7011 higher, 7002 lower, 7003 same; 7009 and 7010 have no verdict.
    assert content == "4 bids compared; we bid higher on 2. We bid lower on 1. Same price on 1. 2 could not be compared."
    for needle in ("$", "Tower", "26.9."):
        assert needle not in content
    [table] = [b for b in blocks if b["type"] == "table"]
    assert table["columns"] == ["Project", "Sent (Pacific)", "Estimator", "Our price", "Difference", "Verdict"]
    first = table["rows"][0]
    assert first[0] == "Tower <1> (#26.9.7001)" and first[2] == "$1,000,000" and first[3] == "$1,250,000"
    assert first[4] == "+$250,000 (+25.0%)" and first[5] == "We bid higher"
    assert first[1] == "Thu, Sep 10, 3:00 PM"
    assert table["rows"][1][4] == "-$50,000 (-10.0%)"
    assert table["rows"][3][2:] == ["$333", "No price", "", "We have no recorded price for this bid."]
    assert EM not in json.dumps([content, blocks])


def test_evb_message_wording_variants():
    def rows(*verdicts):
        return [{"project": {"id": f"p{i}"}, "extra": {"verdict": v}} for i, v in enumerate(verdicts)]

    label = {"label": "September 2026"}
    assert recipes.message_for("estimator_vs_bid", {"window": label, "rows": []})[0] == (
        "No bids were sent in September 2026.")
    one = recipes.message_for("estimator_vs_bid", {"window": label, "rows": rows("We bid higher")})[0]
    assert one == "1 bid compared; we bid higher on 1."
    assert recipes.message_for("estimator_vs_bid", {"window": label, "rows": rows(None, None)})[0] == (
        "2 bids sent; none could be compared yet.")
    fourteen = recipes.message_for("estimator_vs_bid", {"window": label, "rows": rows(*["We bid higher"] * 14)})[0]
    assert fourteen == "14 bids compared; we bid higher on 14."


def test_evb_result_hash_keys_on_prices_and_verdict(loader):
    rows = ar.rows_for(_evb_world(), "estimator_vs_bid", at_utc=AT, role=VIEWER)["rows"]
    h = recipes.result_hash("estimator_vs_bid", rows)
    assert len(h) == 64 and h == recipes.result_hash("estimator_vs_bid", list(reversed(rows)))
    moved = json.loads(json.dumps(rows))
    moved[0]["extra"]["message"] = "reworded"
    moved[0]["extra"]["first_sent_at"] = "2026-09-11T00:00:00+00:00"
    assert recipes.result_hash("estimator_vs_bid", moved) == h       # dates and words never matter
    moved[0]["extra"]["our_min"] = "1240000.00"
    assert recipes.result_hash("estimator_vs_bid", moved) != h
    other = json.loads(json.dumps(rows))
    other[1]["extra"]["estimator_base"] = "510000.00"
    assert recipes.result_hash("estimator_vs_bid", other) != h
    assert recipes.result_hash("estimator_vs_bid", rows) != recipes.result_hash("quote_readiness", rows)


def test_evb_email_has_counts_and_a_link_but_no_amounts_or_names(loader):
    payload = ar.rows_for(_evb_world(), "estimator_vs_bid", at_utc=AT, role=VIEWER)
    html = ar.render_email("Estimator vs our price, monthly", payload, recipient_name="Pat Lee",
                           base_url="https://bdr.example")
    assert "4 bids compared; we bid higher on 2." in html
    assert "https://bdr.example/assistant?tab=routines" in html
    for needle in ("$", "Tower", "26.9.", ">Project<", ">Verdict<", "And "):
        assert needle not in html
    assert EM not in html


# ── quote_readiness ──────────────────────────────────────────────────────────


def _qproject(n, internal, actual=None, stage="rfqs", **extra):
    return {"id": P[n], "name": f"Plaza <{n}>", "number": f"26.10.{7100 + n}",
            "internal_bid_at": internal.isoformat() if internal else None,
            "actual_bid_at": actual.isoformat() if actual else None,
            "current_stage": stage, "abandoned_at": None, "failed_to_bid_at": None, "test_session_id": None, **extra}


def _qr_world():
    projects = [
        _qproject(1, pt(2026, 10, 27, 10), pt(2026, 10, 27, 14)),
        _qproject(2, pt(2026, 10, 28, 10)),
        _qproject(3, pt(2026, 11, 3, 10), pt(2026, 10, 29, 14)),         # only the actual date is in the week
        _qproject(4, pt(2026, 10, 29, 10), abandoned_at="2026-10-20T00:00:00Z"),
        _qproject(5, pt(2026, 10, 29, 11), stage="declined"),
        _qproject(6, pt(2026, 10, 29, 12), stage="pm_only"),
        _qproject(7, pt(2026, 10, 30, 9), failed_to_bid_at="2026-10-20T00:00:00Z"),
        _qproject(8, pt(2026, 10, 30, 10), test_session_id="sess-1"),
        _qproject(9, pt(2026, 10, 30, 11)),
        _qproject(10, pt(2026, 10, 31, 9)),                              # Saturday: outside Monday to Friday
        _qproject(11, pt(2026, 10, 26, 0, 0)),                           # Monday midnight: inside
    ]
    cats = [{"id": f"c{i}", "name": n} for i, n in enumerate(
        ["Gear", "Wiring", "Lighting", "General Material", "Panels", "Demolition", "Low Voltage"])]
    cid = {c["name"]: c["id"] for c in cats}
    rfqs, sends, quotes = [], [], []

    def rfq(n, cat, *, sent=1, quote=None, late=False, send_quoted=False, linked=False):
        rid = f"r{n}-{cid[cat]}"
        rfqs.append({"id": rid, "project_id": P[n], "material_category_id": cid[cat]})
        for k in range(sent):
            sid = f"{rid}-s{k}"
            sends.append({"id": sid, "rfq_id": rid, "status": "sent", "sent_at": pt(2026, 10, 20, 9).isoformat(),
                          "quote_received_at": pt(2026, 10, 22, 9).isoformat() if (send_quoted and k == 0) else None})
            if quote and k == 0:
                quotes.append({"id": f"q-{sid}", "rfq_id": rid, "rfq_send_id": sid if linked else None,
                               "received_after_submission": late})
        if sent == 0:
            sends.append({"id": f"{rid}-draft", "rfq_id": rid, "status": "pending", "sent_at": None,
                          "quote_received_at": None})

    rfq(1, "Gear", quote=True, linked=True)                 # covered by a linked quote
    rfq(1, "Wiring")                                        # sent, nothing back
    rfq(1, "Lighting", quote=True)                          # excluded
    rfq(1, "General Material")                              # excluded
    rfq(1, "Panels", sent=0)                                # draft: never required
    rfq(2, "Lighting", quote=True)
    rfq(2, "Demolition")
    rfq(2, "Low Voltage")                                   # all excluded: required 0
    rfq(3, "Gear", quote=True)                              # unlinked quote covers
    rfq(9, "Gear", quote=True, late=True)                   # a late quote does not cover
    rfq(9, "Panels", send_quoted=True)                      # quote_received_at covers
    rfq(11, "Wiring", sent=2)

    return FakeSb(
        projects=projects, rfqs=rfqs, material_categories=cats, rfq_sends=sends, quotes=quotes,
        estimator_submissions=[
            {"id": "e1", "project_id": P[1], "submitted_at": pt(2026, 10, 21, 9).isoformat()},
            {"id": "e2", "project_id": P[1], "submitted_at": pt(2026, 10, 20, 16).isoformat()},
        ],
        estimator_assignments=[
            {"id": "a1", "project_id": P[2], "returned_at": pt(2026, 10, 22, 9).isoformat()},
            {"id": "a2", "project_id": P[1], "returned_at": pt(2026, 10, 1, 9).isoformat()},
            {"id": "a3", "project_id": P[9], "returned_at": None},
        ],
    )


def _qr(role=VIEWER, **kw):
    return ar.rows_for(_qr_world(), "quote_readiness", at_utc=AT, role=role, **kw)


def test_qr_membership_viewer_sees_either_date():
    payload = _qr()
    assert payload["window"]["label"] == "Monday, October 26 to Friday, October 30"
    assert payload["window"]["phrase"] == "next week"
    assert [r["project"]["number"] for r in payload["rows"]] == [
        "26.10.7111", "26.10.7101", "26.10.7102", "26.10.7103", "26.10.7109"]
    # abandoned, declined, pm_only, failed to bid, the test session and Saturday are all out.


def test_qr_membership_non_viewer_uses_the_internal_date_alone():
    payload = _qr(role=Role.ESTIMATING_ENGINEER_LABOR)
    assert [r["project"]["number"] for r in payload["rows"]] == [
        "26.10.7111", "26.10.7101", "26.10.7102", "26.10.7109"]
    for row in payload["rows"]:
        assert "actual_bid_at" not in row["project"] and "bid_date" not in row["extra"]
    assert _qr(role=None)["rows"][0]["project"].get("actual_bid_at") is None


def test_qr_coverage_exclusions_and_zero_required():
    by = {r["project"]["number"]: r for r in _qr()["rows"]}
    one = by["26.10.7101"]
    assert one["extra"]["required"] == 2 and one["extra"]["covered"] == 1       # Gear yes, Wiring no
    assert one["extra"]["key_quotes"] == "1/2" and one["extra"]["complete"] is False
    assert one["missing_categories"] == ["Wiring"]
    names = {c["category"]: c for c in one["extra"]["categories"]}
    assert set(names) == {"Gear", "Wiring", "Lighting", "General Material"}      # the draft RFQ is not listed
    assert names["Lighting"]["counts_toward_key"] is False and names["Lighting"]["covered"] is True
    assert names["General Material"]["counts_toward_key"] is False
    assert names["Gear"]["quotes_received"] == 1 and names["Gear"]["vendor_emails"] == 1
    two = by["26.10.7102"]
    assert two["extra"]["required"] == 0 and two["extra"]["key_quotes"] == "n/a" and two["extra"]["complete"] is False
    assert two["missing_categories"] == []
    assert by["26.10.7103"]["extra"]["key_quotes"] == "1/1" and by["26.10.7103"]["extra"]["complete"] is True


def test_qr_late_quotes_are_skipped_but_quote_received_at_covers():
    nine = {r["project"]["number"]: r for r in _qr()["rows"]}["26.10.7109"]
    cats = {c["category"]: c for c in nine["extra"]["categories"]}
    assert cats["Gear"]["covered"] is False and cats["Gear"]["quotes_received"] == 0
    assert cats["Panels"]["covered"] is True
    assert nine["extra"]["key_quotes"] == "1/2" and nine["missing_categories"] == ["Gear"]


def test_qr_multiple_vendor_emails_and_returned_wording():
    by = {r["project"]["number"]: r["extra"] for r in _qr()["rows"]}
    assert {c["category"]: c["vendor_emails"] for c in by["26.10.7111"]["categories"]} == {"Wiring": 2}
    # First submission wins over the later round, and the assignment fallback only when there is none.
    assert by["26.10.7101"]["estimator_returned_at"] == pt(2026, 10, 20, 16).isoformat()
    assert by["26.10.7101"]["estimator_returned_text"] == "Estimator returned the estimate to us on 10/20/2026"
    assert by["26.10.7102"]["estimator_returned_at"] == pt(2026, 10, 22, 9).isoformat()
    assert by["26.10.7109"]["estimator_returned_at"] is None
    assert by["26.10.7109"]["estimator_returned_text"] == "Estimator has not returned the estimate to us yet"


def test_qr_week_param():
    payload = _qr(week="2026-10-12")
    assert payload["rows"] == [] and payload["window"]["phrase"] == "the week of 10/12/2026"
    assert _qr(week="this_week")["window"]["phrase"] == "this week"
    assert _qr(week="2026-10-19")["window"]["phrase"] == "this week"        # the Saturday's own week
    with pytest.raises(RecipeError):
        _qr(week="2026-10-27")


def test_qr_message_and_columns():
    content, blocks = recipes.message_for("quote_readiness", _qr())
    # Only 7103 (1/1) is complete: 7101 is 1/2, 7102 is n/a, 7109 is 1/2, 7111 is 0/1.
    assert content == "5 bids next week; 1 has every key quote."
    [table] = [b for b in blocks if b["type"] == "table"]
    assert table["columns"] == ["Project", "Bid date", "Internal due", "Key quotes", "Estimator returned"]
    non_viewer = recipes.message_for("quote_readiness", _qr(role=Role.ESTIMATING_ENGINEER_LABOR))
    [table2] = [b for b in non_viewer[1] if b["type"] == "table"]
    assert table2["columns"] == ["Project", "Internal due", "Key quotes", "Estimator returned"]
    assert all(len(row) == 4 for row in table2["rows"])
    for needle in ("Plaza", "26.10."):
        assert needle not in content and needle not in non_viewer[0]
    assert recipes.table_columns("quote_readiness", Role.EXECUTIVE)[1] == "Bid date"
    assert "Bid date" not in recipes.table_columns("quote_readiness", Role.ESTIMATING_ENGINEER_LABOR)
    assert recipes.table_columns("estimator_vs_bid")[3] == "Our price"
    assert EM not in json.dumps([content, blocks])


def test_qr_message_wording_variants():
    window = {"phrase": "next week"}
    none = recipes.message_for("quote_readiness", {"window": window, "rows": []})[0]
    assert none == "No bids are due next week."
    row = {"project": {"id": "p"}, "extra": {"complete": True}}
    assert recipes.message_for("quote_readiness", {"window": window, "rows": [row]})[0] == (
        "1 bid next week; 1 has every key quote.")
    assert recipes.message_for("quote_readiness", {"window": {"phrase": "this week"}, "rows": [row, row]})[0] == (
        "2 bids this week; 2 have every key quote.")


def test_qr_result_hash():
    rows = _qr()["rows"]
    h = recipes.result_hash("quote_readiness", rows)
    moved = json.loads(json.dumps(rows))
    moved[1]["extra"]["internal_due"] = "later"
    moved[1]["extra"]["estimator_returned_at"] = "2026-10-25T00:00:00+00:00"   # 7101 was returned already
    assert recipes.result_hash("quote_readiness", moved) == h        # a changed date is no change
    moved[0]["extra"]["covered"] += 1
    assert recipes.result_hash("quote_readiness", moved) != h
    again = json.loads(json.dumps(rows))
    again[2]["extra"]["estimator_returned_at"] = None if again[2]["extra"]["estimator_returned_at"] else "x"
    assert recipes.result_hash("quote_readiness", again) != h


def test_qr_email_escapes_names_and_links_to_the_project():
    html = ar.render_email("Key quotes, weekly", _qr(), recipient_name="Pat", base_url="https://bdr.example/")
    assert "Plaza &lt;1&gt;" in html and "Plaza <1>" not in html
    assert f"https://bdr.example/projects/{P[1]}" in html
    assert "Key quotes" in html and "Estimator returned" in html and "Bid date" in html
    assert EM not in html


def test_modules_have_no_em_dash():
    from pathlib import Path

    for module in (ar, recipes):
        assert EM not in Path(module.__file__).read_text(encoding="utf-8")

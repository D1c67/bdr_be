"""Cadence parsing, rule text and Pacific expansion for assistant routines
(docs/ASSISTANT_PHASE3_PLAN.md 3.1). Pure: no database, no app."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import pytest

from app.assistant import rrules
from app.assistant.rrules import ALL_DAYS, PT, WEEKDAYS, CadenceError

UTC = timezone.utc
EM = "\u2014"
EN = "\u2013"

CADENCES = {
    "weekdays": {"frequency": "weekdays", "hour": 7, "minute": 0},
    "daily": {"frequency": "daily", "hour": 6, "minute": 45},
    "weekly": {"frequency": "weekly", "hour": 6, "minute": 30, "days": ["TH", "MO"], "interval": 1},
    "weekly_2": {"frequency": "weekly", "hour": 7, "minute": 0, "days": ["MO"], "interval": 2},
    "weekly_all": {"frequency": "weekly", "hour": 9, "minute": 15, "days": list(ALL_DAYS), "interval": 3},
    "monthly": {"frequency": "monthly", "hour": 8, "minute": 0, "day_of_month": 1},
    "hourly": {"frequency": "hourly", "minute": 0, "days": list(WEEKDAYS),
               "window_start_hour": 7, "window_end_hour": 17},
    "hourly_all": {"frequency": "hourly", "minute": 30, "days": list(ALL_DAYS),
                   "window_start_hour": 0, "window_end_hour": 5},
}


def _utc(*a):
    return datetime(*a, tzinfo=UTC)


def _pt(*a):
    return datetime(*a, tzinfo=PT).astimezone(UTC)


# ── parse and round trip ─────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(CADENCES))
def test_every_frequency_round_trips(name):
    c = rrules.parse_cadence(CADENCES[name])
    rule = rrules.to_rrule(c)
    assert rrules.from_rrule(rule) == c
    assert rrules.to_rrule(rrules.from_rrule(rule)) == rule
    assert rrules.parse_cadence(c.as_dict()) == c


def test_rule_texts_match_the_plan_and_the_caps_trigger():
    expected = {
        "weekdays": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=7;BYMINUTE=0",
        "daily": "FREQ=DAILY;BYHOUR=6;BYMINUTE=45",
        "weekly": "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TH;BYHOUR=6;BYMINUTE=30",
        "weekly_2": "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;BYHOUR=7;BYMINUTE=0",
        "monthly": "FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=8;BYMINUTE=0",
        "hourly": "FREQ=HOURLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=7,8,9,10,11,12,13,14,15,16,17;BYMINUTE=0",
    }
    for name, rule in expected.items():
        assert rrules.to_rrule(rrules.parse_cadence(CADENCES[name])) == rule


@pytest.mark.parametrize("name", sorted(CADENCES))
def test_no_written_rule_has_bysecond_or_a_part_the_trigger_refuses(name):
    rule = rrules.to_rrule(rrules.parse_cadence(CADENCES[name]))
    assert "BYSECOND" not in rule
    assert re.search(r"BYMINUTE=([0-9,]+)", rule).group(1).count(",") == 0
    assert not re.search(r"(COUNT|UNTIL|DTSTART|EXDATE|EXRULE|BYSETPOS|BYWEEKNO|BYYEARDAY|BYEASTER|RDATE)", rule)
    assert 10 <= len(rule) <= 500


@pytest.mark.parametrize("rule", [
    "FREQ=HOURLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=7,8;BYMINUTE=0;BYSECOND=0",
    "FREQ=HOURLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=7,8;BYMINUTE=0;BYSECOND=0,30",
    "FREQ=DAILY;BYHOUR=7;BYMINUTE=0;BYSECOND=0",
])
def test_from_rrule_refuses_bysecond(rule):
    with pytest.raises(CadenceError) as e:
        rrules.from_rrule(rule)
    assert e.value.code == "bad_cadence"


@pytest.mark.parametrize("rule", [
    "FREQ=DAILY;BYHOUR=7;BYMINUTE=0;COUNT=3",
    "FREQ=DAILY;BYHOUR=7;BYMINUTE=0;UNTIL=20271231T000000Z",
    "DTSTART:20261023T070000\nRRULE:FREQ=DAILY;BYHOUR=7;BYMINUTE=0",
    "FREQ=DAILY;BYHOUR=7;BYMINUTE=0;EXDATE=20261225",
    "FREQ=DAILY;RDATE=20261101T070000",
    "FREQ=MONTHLY;BYDAY=MO;BYSETPOS=1;BYHOUR=7;BYMINUTE=0",
    "FREQ=YEARLY;BYYEARDAY=1;BYHOUR=7;BYMINUTE=0",
    "FREQ=MINUTELY;BYMINUTE=0",
    "FREQ=HOURLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=7;BYMINUTE=0,30",
    "FREQ=DAILY;BYHOUR=7,8;BYMINUTE=0",
    "FREQ=DAILY;BYHOUR=7;BYMINUTE=0;BYHOUR=8",
    "FREQ=DAILY;INTERVAL=2;BYHOUR=7;BYMINUTE=0",
    "FREQ=DAILY;BYMINUTE=0",
    "FREQ=MONTHLY;BYMONTHDAY=31;BYHOUR=7;BYMINUTE=0",
    "FREQ=HOURLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=7,9;BYMINUTE=0",
    "FREQ=HOURLY;BYDAY=MO;BYHOUR=7,8;BYMINUTE=0",
    "FREQ=WEEKLY;INTERVAL=5;BYDAY=MO;BYHOUR=7;BYMINUTE=0",
    "",
    "garbage",
])
def test_from_rrule_refuses_parts_and_shapes_it_never_writes(rule):
    with pytest.raises(CadenceError) as e:
        rrules.from_rrule(rule)
    assert e.value.code == "bad_cadence"
    assert rule.strip() == "" or rule not in str(e.value)


@pytest.mark.parametrize("obj", [
    {"frequency": "yearly", "hour": 7, "minute": 0},
    {"frequency": "minutely", "minute": 0},
    {"frequency": "weekdays", "minute": 0},
    {"frequency": "weekdays", "hour": 24, "minute": 0},
    {"frequency": "weekdays", "hour": 7, "minute": 60},
    {"frequency": "weekdays", "hour": 7, "minute": -1},
    {"frequency": "weekdays", "hour": True, "minute": 0},
    {"frequency": "weekdays", "hour": 7.5, "minute": 0},
    {"frequency": "weekly", "hour": 7, "minute": 0, "days": []},
    {"frequency": "weekly", "hour": 7, "minute": 0, "days": ["XX"]},
    {"frequency": "weekly", "hour": 7, "minute": 0, "days": ["MO", "MO"]},
    {"frequency": "weekly", "hour": 7, "minute": 0, "days": ["MO"], "interval": 5},
    {"frequency": "weekly", "hour": 7, "minute": 0, "days": ["MO"], "interval": 0},
    {"frequency": "daily", "hour": 7, "minute": 0, "interval": 2},
    {"frequency": "daily", "hour": 7, "minute": 0, "days": ["MO"]},
    {"frequency": "monthly", "hour": 7, "minute": 0, "day_of_month": 29},
    {"frequency": "monthly", "hour": 7, "minute": 0, "day_of_month": 31},
    {"frequency": "monthly", "hour": 7, "minute": 0},
    {"frequency": "daily", "hour": 7, "minute": 0, "day_of_month": 1},
    {"frequency": "hourly", "minute": 0, "days": list(WEEKDAYS), "window_start_hour": 17, "window_end_hour": 7},
    {"frequency": "hourly", "minute": 0, "days": list(ALL_DAYS), "window_start_hour": 0, "window_end_hour": 19},
    {"frequency": "hourly", "minute": 0, "days": ["MO", "TU"], "window_start_hour": 7, "window_end_hour": 9},
    {"frequency": "hourly", "minute": 0, "days": list(WEEKDAYS), "window_start_hour": 7},
    {"frequency": "hourly", "hour": 7, "minute": 0, "days": list(WEEKDAYS),
     "window_start_hour": 7, "window_end_hour": 9},
    {"frequency": "daily", "hour": 7, "minute": 0, "window_start_hour": 7, "window_end_hour": 9},
    {"frequency": "daily", "hour": 7, "minute": 0, "rrule": "FREQ=MINUTELY"},
    "weekdays at 7",
])
def test_bad_cadences_raise_bad_cadence(obj):
    with pytest.raises(CadenceError) as e:
        rrules.parse_cadence(obj)
    assert e.value.code == "bad_cadence"
    message = str(e.value)
    assert message.endswith(".") and EM not in message


def test_parse_defaults_and_lenient_forms():
    c = rrules.parse_cadence({"frequency": " Weekdays ", "hour": "7", "minute": None, "days": None,
                              "day_of_month": None, "interval": None, "window_start_hour": None,
                              "window_end_hour": None})
    assert c == rrules.Cadence("weekdays", 7, 0, (), None, 1, None, None)
    w = rrules.parse_cadence({"frequency": "weekly", "hour": 6, "minute": 0, "days": "th,mo"})
    assert w.days == ("MO", "TH")
    h = rrules.parse_cadence({"frequency": "hourly", "minute": 0, "days": ["FR", "TH", "WE", "TU", "MO"],
                              "window_start_hour": 7, "window_end_hour": 25 - 18})
    assert h.days == WEEKDAYS
    # An 18 hour window is the longest.
    rrules.parse_cadence({"frequency": "hourly", "minute": 0, "days": list(ALL_DAYS),
                          "window_start_hour": 0, "window_end_hour": 18})


def test_weekday_only():
    wo = {name: rrules.weekday_only(rrules.parse_cadence(obj)) for name, obj in CADENCES.items()}
    assert wo == {
        "weekdays": True, "daily": False, "weekly": True, "weekly_2": True, "weekly_all": False,
        "monthly": False, "hourly": True, "hourly_all": False,
    }


# ── words ────────────────────────────────────────────────────────────────────


def test_cadence_text_for_every_frequency():
    texts = {name: rrules.cadence_text(rrules.parse_cadence(obj)) for name, obj in CADENCES.items()}
    assert texts == {
        "weekdays": "Weekdays at 7:00 AM Pacific, holidays skipped",
        "daily": "Every day at 6:45 AM Pacific",
        "weekly": "Every Monday and Thursday at 6:30 AM Pacific, holidays skipped",
        "weekly_2": "Every 2 weeks on Monday at 7:00 AM Pacific, holidays skipped",
        "weekly_all": ("Every 3 weeks on Monday, Tuesday, Wednesday, Thursday, Friday, Saturday "
                       "and Sunday at 9:15 AM Pacific"),
        "monthly": "On day 1 of each month at 8:00 AM Pacific",
        "hourly": "Every hour from 7:00 AM to 5:00 PM Pacific on weekdays, holidays skipped",
        "hourly_all": "Every hour from 12:30 AM to 5:30 AM Pacific every day",
    }
    for text in texts.values():
        assert EM not in text and EN not in text and "-" not in text


def test_short_text_and_clock_edges():
    assert rrules.short_text(rrules.parse_cadence(CADENCES["weekdays"])) == "weekdays at 7:00 AM"
    assert rrules.short_text(rrules.parse_cadence(CADENCES["hourly"])) == (
        "every hour from 7:00 AM to 5:00 PM on weekdays")
    one = rrules.parse_cadence({"frequency": "hourly", "minute": 5, "days": list(WEEKDAYS),
                                "window_start_hour": 12, "window_end_hour": 12})
    assert rrules.cadence_text(one) == "At 12:05 PM Pacific on weekdays, holidays skipped"
    assert rrules.clock_text(0, 0) == "12:00 AM"
    assert rrules.clock_text(12, 0) == "12:00 PM"
    assert rrules.clock_text(23, 59) == "11:59 PM"


# ── expansion and DST ────────────────────────────────────────────────────────


def _rule(name_or_obj):
    obj = CADENCES[name_or_obj] if isinstance(name_or_obj, str) else name_or_obj
    return rrules.to_rrule(rrules.parse_cadence(obj))


def test_spring_forward_gap_runs_one_hour_later():
    rule = _rule({"frequency": "daily", "hour": 2, "minute": 30})
    runs = rrules.occurrences(rule, _utc(2027, 3, 1), _utc(2027, 3, 13, 12), 2, frozenset())
    assert runs == [_utc(2027, 3, 14, 10, 30), _utc(2027, 3, 15, 9, 30)]
    assert runs[0].astimezone(PT).strftime("%H:%M") == "03:30"


def test_fall_back_repeated_time_runs_once_at_the_first_instant():
    rule = _rule({"frequency": "daily", "hour": 1, "minute": 30})
    runs = rrules.occurrences(rule, _utc(2026, 10, 1), _utc(2026, 10, 31, 12), 3, frozenset())
    assert runs == [_utc(2026, 11, 1, 8, 30), _utc(2026, 11, 2, 9, 30), _utc(2026, 11, 3, 9, 30)]
    # Asking from inside the repeated hour (01:15 PST, after the 08:30Z run)
    # never runs it a second time.
    after = _utc(2026, 11, 1, 9, 15)
    assert rrules.occurrences(rule, _utc(2026, 10, 1), after, 1, frozenset()) == [_utc(2026, 11, 2, 9, 30)]


def test_hourly_on_spring_forward_day_runs_once_at_ten_utc():
    rule = _rule({"frequency": "hourly", "minute": 0, "days": list(ALL_DAYS),
                  "window_start_hour": 1, "window_end_hour": 4})
    runs = rrules.occurrences(rule, _utc(2027, 3, 1), _utc(2027, 3, 14, 8), 4, frozenset())
    # 01:00 PST, then 02:00 (gap) and 03:00 PDT are the same 10:00Z instant, then 04:00 PDT.
    assert runs == [_utc(2027, 3, 14, 9), _utc(2027, 3, 14, 10), _utc(2027, 3, 14, 11), _utc(2027, 3, 15, 8)]
    assert runs.count(_utc(2027, 3, 14, 10)) == 1


def test_weekday_rule_skips_a_holiday_and_lands_on_the_next_working_day():
    rule = _rule("weekdays")
    thanksgiving = {date(2026, 11, 26), date(2026, 11, 27)}
    now = _pt(2026, 11, 25, 9, 0)   # Wednesday after 7:00
    runs = rrules.next_runs(rule, _utc(2026, 10, 1), now, frozenset(thanksgiving))
    assert runs == [_pt(2026, 11, 30, 7, 0), _pt(2026, 12, 1, 7, 0), _pt(2026, 12, 2, 7, 0)]
    # The stored list is ISO text; it is read the same way.
    assert rrules.next_runs(rule, _utc(2026, 10, 1), now, ["2026-11-26", "2026-11-27"]) == runs


def test_weekly_and_hourly_weekday_rules_skip_holidays_too():
    holiday = frozenset({date(2026, 12, 25)})   # a Friday
    weekly = _rule({"frequency": "weekly", "hour": 8, "minute": 0, "days": ["FR"], "interval": 1})
    assert rrules.next_runs(weekly, _utc(2026, 10, 1), _pt(2026, 12, 24, 12, 0), holiday, n=1) == [
        _pt(2027, 1, 1, 8, 0)]
    hourly = _rule("hourly")
    first = rrules.next_runs(hourly, _utc(2026, 10, 1), _pt(2026, 12, 24, 18, 0), holiday, n=1)
    assert first == [_pt(2026, 12, 28, 7, 0)]


def test_daily_and_monthly_rules_run_on_holidays():
    holiday = frozenset({date(2026, 12, 25)})
    daily = _rule({"frequency": "daily", "hour": 7, "minute": 0})
    assert rrules.next_runs(daily, _utc(2026, 10, 1), _pt(2026, 12, 24, 12, 0), holiday, n=1) == [
        _pt(2026, 12, 25, 7, 0)]
    every_day = _rule("weekly_all")
    assert not rrules.weekday_only(rrules.from_rrule(every_day))


def test_a_rule_whose_every_occurrence_is_a_holiday_raises():
    rule = _rule({"frequency": "weekly", "hour": 7, "minute": 0, "days": ["MO"], "interval": 1})
    start = date(2026, 10, 26)
    mondays = frozenset(start + timedelta(weeks=i) for i in range(70))
    now = _pt(2026, 10, 24, 12, 0)
    assert rrules.occurrences(rule, _utc(2026, 10, 1), now, 1, mondays) == []
    with pytest.raises(CadenceError) as e:
        rrules.next_runs(rule, _utc(2026, 10, 1), now, mondays)
    assert str(e.value) == "This routine would never run."
    assert e.value.code == "bad_cadence"


def test_interval_two_weekly_anchors_on_dtstart():
    rule = _rule("weekly_2")
    now = _pt(2026, 10, 21, 12, 0)   # a Wednesday
    a = rrules.next_runs(rule, now, now, frozenset())
    assert a == [_pt(2026, 11, 2, 7, 0), _pt(2026, 11, 16, 7, 0), _pt(2026, 11, 30, 7, 0)]
    # Created one week later, the same rule runs on the other Mondays.
    later = now + timedelta(weeks=1)
    b = rrules.next_runs(rule, later, later, frozenset())
    assert b == [_pt(2026, 11, 9, 7, 0), _pt(2026, 11, 23, 7, 0), _pt(2026, 12, 7, 7, 0)]


def test_occurrences_are_strictly_after_and_utc_and_dtstart_seconds_are_floored():
    rule = _rule({"frequency": "daily", "hour": 7, "minute": 0})
    dtstart = _pt(2026, 10, 23, 6, 59) + timedelta(seconds=41, microseconds=5)
    at_seven = _pt(2026, 10, 23, 7, 0)
    runs = rrules.occurrences(rule, dtstart, at_seven, 2, frozenset())
    assert runs == [_pt(2026, 10, 24, 7, 0), _pt(2026, 10, 25, 7, 0)]
    assert all(r.tzinfo == UTC and r.second == 0 and r.microsecond == 0 for r in runs)
    # next_runs looks from one second before now, so a slot at now is included.
    assert rrules.next_runs(rule, dtstart, at_seven, frozenset(), n=1) == [at_seven]
    assert rrules.occurrences(rule, dtstart, at_seven, 0, frozenset()) == []


def test_monthly_runs_on_its_day():
    rule = _rule("monthly")
    runs = rrules.next_runs(rule, _utc(2026, 10, 1), _pt(2026, 10, 23, 12, 0), frozenset())
    assert runs == [_pt(2026, 11, 1, 8, 0), _pt(2026, 12, 1, 8, 0), _pt(2027, 1, 1, 8, 0)]


def test_occurrences_refuses_a_rule_it_never_writes():
    with pytest.raises(CadenceError):
        rrules.occurrences("FREQ=DAILY;BYHOUR=7;BYMINUTE=0;COUNT=2", _utc(2026, 10, 1),
                           _utc(2026, 10, 2), 1, frozenset())


def test_module_has_no_dash_characters_and_imports_nothing_from_app():
    from pathlib import Path

    text = Path(rrules.__file__).read_text(encoding="utf-8")
    assert EM not in text and EN not in text
    assert "from app" not in text and "import app" not in text

"""Calling In (docs/CALLING_IN.md): the pure membership / band / slot seam and
the poller.

Pinned here (section 3 rules and section 2 decisions):
  - date-only detection (exactly midnight Pacific) and the end-of-day T,
    DST-safe on both clock changes;
  - List 1 covers only GCs sent before T (all GCs while T is unknown), a
    no-date project stays on List 1 ("No actual bid date") until a bid
    outcome exists, and never reaches List 2;
  - List 2 runs T to T + 10 Pacific days with day bands by calendar day
    (day 0 opens_soon, 1-7 call_now, 8-10 overdue) and clears itself;
  - only `spoke` marks a GC done; voicemail / no answer keep it open;
  - a late GC sent before T re-opens List 1 and notifies again;
  - the notify trigger (section 5): List 1 max(newest eligible sent_at,
    other round's latest close), List 2 max(T, newest sent_at, other round's
    latest close), so a send recorded days after T, a missing date entered
    after the fact and a postponement back to List 1 all notify; a correction
    (Spoke edited to voicemail, clearing call deleted) re-opens silently;
  - postponement moves a project back, abandon / decline / test session drop it;
  - won / lost stays on List 2 and is reported;
  - analytics: call rate, median / average hours, missed vs open, attempts,
    slots anchored on when their window opened, the go-live cut;
  - the poller: claim idempotency (the partial unique index), the 24-hour
    burst guard, close + dismiss with the right reason.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.roles import Role
from app.services import calling_in as ci
from app.services.calling_in import CallFact, ProjectFacts, SendFact
from tests.calling_in_fake import FakeSB

UTC = timezone.utc
PT = ci.PT


def pt(y, mo, d, h=0, mi=0, s=0, us=0) -> datetime:
    """A Pacific wall time as an aware UTC datetime."""
    return datetime(y, mo, d, h, mi, s, us, tzinfo=PT).astimezone(UTC)


def send(gc, sent_at, sid=None, **row):
    return SendFact(
        id=sid or f"ps-{gc}", gc_id=gc, gc_name=f"GC {gc}", sent_at=sent_at,
        row={"material_amount": None, "labor_amount": None, **row},
    )


def call(gc, round_, outcome, at, cid=None, by="u1", contacts=("Pat",)):
    return CallFact(
        id=cid or f"c-{gc}-{round_}-{outcome}-{at.timestamp()}", gc_id=gc, round=round_,
        outcome=outcome, called_at=at, created_by=by, contact_names=tuple(contacts),
        row={
            "id": cid or f"c-{gc}-{at.timestamp()}", "project_id": "p1", "gc_id": gc,
            "round": round_, "outcome": outcome, "note": "n",
            "contacts": [{"gc_contact_id": "k1", "name": n, "phone": None, "email": None}
                         for n in contacts],
            "called_at": at.isoformat(), "created_by": by, "updated_at": at.isoformat(),
        },
    )


def project(actual=None, sends=(), calls=(), pid="p1", **kw):
    return ProjectFacts(
        id=pid, name=kw.pop("name", f"Project {pid}"), number=kw.pop("number", "1001"),
        actual_bid_at=actual, sends=list(sends), calls=list(calls), **kw,
    )


# T: Tuesday 2026-09-29 15:00 PDT. GCs A and B got our proposal the week before.
T = pt(2026, 9, 29, 15)
SENT = pt(2026, 9, 22, 10)


# ── effective bid time ───────────────────────────────────────────────────────


def test_null_bid_time():
    assert ci.effective_bid_time(None) == (None, False)


def test_a_real_time_is_kept():
    assert ci.effective_bid_time(T) == (T, False)


def test_midnight_pacific_is_date_only_and_moves_to_end_of_day():
    bid_at, date_only = ci.effective_bid_time(pt(2026, 9, 29))
    assert date_only is True
    assert bid_at == pt(2026, 9, 29, 23, 59, 59, 999000)


def test_midnight_utc_is_not_midnight_pacific():
    # 00:00 UTC is 17:00 PDT the day before: a real time, not date-only.
    midnight_utc = datetime(2026, 9, 29, tzinfo=UTC)
    assert ci.effective_bid_time(midnight_utc) == (midnight_utc, False)


def test_one_second_past_midnight_is_a_real_time():
    assert ci.effective_bid_time(pt(2026, 9, 29, 0, 0, 1))[1] is False


def test_date_only_end_of_day_on_both_dst_days():
    # 2026-11-01 is the 25-hour fall-back day; 2026-03-08 the 23-hour spring day.
    fall, _ = ci.effective_bid_time(pt(2026, 11, 1))
    assert fall == datetime(2026, 11, 2, 7, 59, 59, 999000, tzinfo=UTC)  # 23:59:59.999 PST
    spring, _ = ci.effective_bid_time(pt(2026, 3, 8))
    assert spring == datetime(2026, 3, 9, 6, 59, 59, 999000, tzinfo=UTC)  # 23:59:59.999 PDT


def test_post_bid_close_is_ten_pacific_days_at_the_same_wall_time_across_dst():
    t = pt(2026, 10, 30, 14)  # PDT
    closes = ci.post_bid_closes_at(t)
    assert closes == pt(2026, 11, 9, 14)  # 14:00 PST
    assert closes - t == timedelta(days=10, hours=1)  # the fall-back hour


# ── List 1 (pre_bid) ─────────────────────────────────────────────────────────


def test_list1_before_bid_with_gcs_sent_before_t():
    p = project(T, [send("A", SENT), send("B", SENT)])
    now = T - timedelta(days=2)
    assert ci.is_member(p, ci.PRE_BID, now)
    assert not ci.is_member(p, ci.POST_BID, now)
    entry = ci.build_entry(p, ci.PRE_BID, now)
    assert entry["band"] == "open"
    assert entry["bid_at"] == T.isoformat()
    assert entry["window_closes_at"] == T.isoformat()
    assert entry["days_since_bid"] is None
    assert (entry["gcs_total"], entry["gcs_done"]) == (2, 0)
    assert entry["bid_at_missing"] is False and entry["bid_at_date_only"] is False


def test_list1_only_covers_gcs_sent_before_t():
    late = send("C", T + timedelta(hours=2))
    p = project(T, [send("A", SENT), late])
    assert [s.gc_id for s in ci.round_sends(p, ci.PRE_BID)] == ["A"]
    assert [s.gc_id for s in ci.round_sends(p, ci.POST_BID)] == ["A", "C"]
    now = T + timedelta(days=3)
    assert not ci.gc_window_open(p, ci.PRE_BID, "C", now)
    assert ci.gc_window_open(p, ci.POST_BID, "C", now)


def test_list1_leaves_at_bid_time():
    p = project(T, [send("A", SENT)])
    assert ci.is_member(p, ci.PRE_BID, T - timedelta(microseconds=1))
    assert not ci.is_member(p, ci.PRE_BID, T)
    assert ci.close_reason(p, ci.PRE_BID, T) == "window_closed"


def test_no_bid_date_stays_on_list1_and_never_reaches_list2():
    p = project(None, [send("A", SENT)])
    now = SENT + timedelta(days=90)
    assert ci.is_member(p, ci.PRE_BID, now)
    assert not ci.is_member(p, ci.POST_BID, now)
    entry = ci.build_entry(p, ci.PRE_BID, now)
    assert entry["band"] == "no_bid_date"
    assert entry["bid_at_missing"] is True
    assert entry["bid_at"] is None and entry["window_closes_at"] is None
    assert ci.gc_window_open(p, ci.PRE_BID, "A", now)
    assert not ci.gc_window_open(p, ci.POST_BID, "A", now)


def test_no_bid_date_leaves_list1_once_an_outcome_is_recorded():
    recorded = SENT + timedelta(days=20)
    p = project(None, [send("A", SENT)], outcome_result="no_award", outcome_recorded_at=recorded)
    now = recorded + timedelta(hours=1)
    assert not ci.is_member(p, ci.PRE_BID, now)
    assert not ci.gc_window_open(p, ci.PRE_BID, "A", now)
    assert ci.close_reason(p, ci.PRE_BID, now) == "window_closed"


def test_voicemail_and_no_answer_keep_the_gc_open_only_spoke_marks_done():
    now = T - timedelta(days=1)
    attempts = [
        call("A", ci.PRE_BID, "voicemail", now - timedelta(hours=3)),
        call("A", ci.PRE_BID, "no_answer", now - timedelta(hours=2)),
    ]
    p = project(T, [send("A", SENT)], attempts)
    assert ci.is_member(p, ci.PRE_BID, now)
    assert ci.build_entry(p, ci.PRE_BID, now)["gcs_done"] == 0
    p.calls.append(call("A", ci.PRE_BID, "spoke", now - timedelta(hours=1)))
    assert not ci.is_member(p, ci.PRE_BID, now)
    assert ci.close_reason(p, ci.PRE_BID, now) == "cleared"
    assert ci.done_at_by_gc(p, ci.PRE_BID) == {"A": now - timedelta(hours=1)}


def test_a_spoke_call_in_the_other_round_does_not_count():
    now = T + timedelta(days=2)
    p = project(T, [send("A", SENT)], [call("A", ci.PRE_BID, "spoke", T - timedelta(days=1))])
    assert ci.is_member(p, ci.POST_BID, now)


def test_project_stays_until_every_gc_is_done():
    now = T - timedelta(days=1)
    p = project(T, [send("A", SENT), send("B", SENT)],
                [call("A", ci.PRE_BID, "spoke", now - timedelta(hours=1))])
    assert ci.is_member(p, ci.PRE_BID, now)
    entry = ci.build_entry(p, ci.PRE_BID, now)
    assert (entry["gcs_total"], entry["gcs_done"]) == (2, 1)


def test_done_gc_still_accepts_extra_calls_inside_the_window():
    now = T - timedelta(days=1)
    p = project(T, [send("A", SENT)], [call("A", ci.PRE_BID, "spoke", now - timedelta(hours=1))])
    assert ci.gc_window_open(p, ci.PRE_BID, "A", now)


def test_late_gc_reopens_list1_with_only_that_gc_outstanding_and_notifies():
    now = T - timedelta(days=1)
    p = project(T, [send("A", SENT)], [call("A", ci.PRE_BID, "spoke", now - timedelta(days=1))])
    assert not ci.is_member(p, ci.PRE_BID, now)
    late_sent = now - timedelta(hours=2)
    p.sends.append(send("L", late_sent))
    assert ci.is_member(p, ci.PRE_BID, now)
    entry = ci.build_entry(p, ci.PRE_BID, now)
    assert (entry["gcs_total"], entry["gcs_done"]) == (2, 1)
    assert ci.notify_trigger(p, ci.PRE_BID) == late_sent
    assert ci.should_notify(p, ci.PRE_BID, now)


# ── List 2 (post_bid) ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "now,days,band",
    [
        (T, 0, "opens_soon"),
        (pt(2026, 9, 29, 23, 59), 0, "opens_soon"),
        # Pacific midnight flips the day, well under 24 hours after T.
        (pt(2026, 9, 30, 0, 0), 1, "call_now"),
        (pt(2026, 10, 6, 23, 59), 7, "call_now"),
        (pt(2026, 10, 7, 0, 0), 8, "overdue"),
        (pt(2026, 10, 9, 14, 59), 10, "overdue"),
    ],
)
def test_list2_bands_by_pacific_calendar_day(now, days, band):
    p = project(T, [send("A", SENT)])
    assert ci.is_member(p, ci.POST_BID, now)
    entry = ci.build_entry(p, ci.POST_BID, now)
    assert (entry["days_since_bid"], entry["band"]) == (days, band)
    assert entry["window_closes_at"] == pt(2026, 10, 9, 15).isoformat()


def test_list2_band_boundary_uses_pacific_not_utc_days():
    # 2026-09-30 06:00 UTC is still 9/29 23:00 PDT: day 0, not day 1.
    p = project(T, [send("A", SENT)])
    entry = ci.build_entry(p, ci.POST_BID, datetime(2026, 9, 30, 6, 0, tzinfo=UTC))
    assert (entry["days_since_bid"], entry["band"]) == (0, "opens_soon")


def test_list2_clears_itself_ten_days_after_t():
    p = project(T, [send("A", SENT)])
    closes = pt(2026, 10, 9, 15)
    assert ci.is_member(p, ci.POST_BID, closes - timedelta(microseconds=1))
    assert not ci.is_member(p, ci.POST_BID, closes)
    assert ci.close_reason(p, ci.POST_BID, closes) == "window_closed"
    assert not ci.gc_window_open(p, ci.POST_BID, "A", closes)


def test_list2_across_the_fall_back_change():
    t = pt(2026, 10, 30, 14)
    p = project(t, [send("A", t - timedelta(days=3))])
    # 11/2 is day 3 whatever the 25-hour 11/1 did to elapsed hours.
    assert ci.build_entry(p, ci.POST_BID, pt(2026, 11, 2, 0, 30))["days_since_bid"] == 3
    assert ci.is_member(p, ci.POST_BID, pt(2026, 11, 9, 13, 59))
    assert not ci.is_member(p, ci.POST_BID, pt(2026, 11, 9, 14))


def test_date_only_bid_moves_to_list2_at_the_end_of_that_pacific_day():
    p = project(pt(2026, 9, 29), [send("A", pt(2026, 9, 25, 9))])
    evening = pt(2026, 9, 29, 20)
    assert ci.is_member(p, ci.PRE_BID, evening)
    assert not ci.is_member(p, ci.POST_BID, evening)
    entry = ci.build_entry(p, ci.PRE_BID, evening)
    assert entry["bid_at_date_only"] is True
    assert entry["bid_at"] == pt(2026, 9, 29, 23, 59, 59, 999000).isoformat()
    next_morning = pt(2026, 9, 30, 0, 0)
    assert not ci.is_member(p, ci.PRE_BID, next_morning)
    assert ci.is_member(p, ci.POST_BID, next_morning)
    assert ci.build_entry(p, ci.POST_BID, next_morning)["band"] == "call_now"
    assert ci.build_entry(p, ci.POST_BID, next_morning)["window_closes_at"] == (
        pt(2026, 10, 9, 23, 59, 59, 999000).isoformat()
    )


def test_postponement_moves_the_project_back_to_list1():
    p = project(T, [send("A", SENT)])
    now = T + timedelta(days=2)
    assert ci.is_member(p, ci.POST_BID, now)
    p.actual_bid_at = now + timedelta(days=5)  # the GC pushed the bid
    assert not ci.is_member(p, ci.POST_BID, now)
    assert ci.close_reason(p, ci.POST_BID, now) == "left"
    assert ci.is_member(p, ci.PRE_BID, now)


@pytest.mark.parametrize(
    "kw", [{"abandoned": True}, {"stage": "declined"}, {"test_session": True}]
)
def test_abandoned_declined_and_test_projects_drop(kw):
    p = project(T, [send("A", SENT)], **kw)
    for now in (T - timedelta(days=1), T + timedelta(days=2)):
        assert ci.on_list(p, now) is None
        assert not ci.gc_window_open(p, ci.PRE_BID, "A", now)
    assert ci.close_reason(p, ci.POST_BID, T + timedelta(days=2)) == "left"
    assert ci.project_slots(p, T + timedelta(days=2)) == []


def test_project_with_no_sent_gc_is_never_on_a_list():
    p = project(T, [])
    assert ci.on_list(p, T - timedelta(days=1)) is None
    assert ci.on_list(p, T + timedelta(days=1)) is None


@pytest.mark.parametrize("result,label", [("won", "won"), ("lost", "lost"), ("no_award", None)])
def test_win_loss_before_calling_stays_on_list2_and_is_reported(result, label):
    p = project(T, [send("A", SENT)], outcome_result=result, outcome_recorded_at=T)
    now = T + timedelta(days=2)
    assert ci.is_member(p, ci.POST_BID, now)
    assert ci.build_entry(p, ci.POST_BID, now)["outcome"] == label


def test_entry_for_a_round_the_project_is_not_on():
    p = project(T, [send("A", SENT)])
    now = T - timedelta(days=1)
    entry = ci.build_entry(p, ci.POST_BID, now)
    assert entry["on_list"] is False
    assert entry["band"] == "opens_soon" and entry["days_since_bid"] is None
    no_date = ci.build_entry(project(None, [send("A", SENT)]), ci.POST_BID, now)
    assert no_date["band"] == "no_bid_date"


# ── lists, summary, burst guard ──────────────────────────────────────────────


def test_lists_sorted_by_bid_at_with_missing_dates_last():
    now = pt(2026, 9, 28, 12)
    a = project(pt(2026, 10, 5, 10), [send("A", SENT)], pid="late", name="Late")
    b = project(pt(2026, 9, 30, 10), [send("A", SENT)], pid="soon", name="Soon")
    c = project(None, [send("A", SENT)], pid="nodate", name="Aardvark")
    d = project(pt(2026, 9, 20, 10), [send("A", pt(2026, 9, 1))], pid="old", name="Old")
    e = project(pt(2026, 9, 26, 10), [send("A", pt(2026, 9, 1))], pid="newer", name="Newer")
    lists = ci.compute_lists([a, b, c, d, e], now, {("soon", ci.PRE_BID): pt(2026, 9, 27)})
    assert [x["project_id"] for x in lists[ci.PRE_BID]] == ["soon", "late", "nodate"]
    assert [x["project_id"] for x in lists[ci.POST_BID]] == ["old", "newer"]
    assert lists[ci.PRE_BID][0]["entered_at"] == pt(2026, 9, 27).isoformat()
    assert lists[ci.PRE_BID][1]["entered_at"] is None
    assert ci.summary_counts([a, b, c, d, e], now) == {"open_count": 5, "pre_bid": 3, "post_bid": 2}


def test_burst_guard_is_24_hours_from_the_trigger():
    p = project(T, [send("A", SENT)])
    assert ci.notify_trigger(p, ci.POST_BID) == T
    assert ci.should_notify(p, ci.POST_BID, T + timedelta(hours=24))
    assert not ci.should_notify(p, ci.POST_BID, T + timedelta(hours=24, seconds=1))
    assert ci.notify_trigger(p, ci.PRE_BID) == SENT
    assert ci.should_notify(p, ci.PRE_BID, SENT + timedelta(hours=1))
    assert not ci.should_notify(p, ci.PRE_BID, SENT + timedelta(days=2))


def test_trigger_counts_a_proposal_recorded_as_sent_after_t():
    # Mark as submitted / the 0140 wizard days after T stamps sent_at = now.
    late = T + timedelta(days=3)
    p = project(T, [send("A", late)])
    assert ci.notify_trigger(p, ci.POST_BID) == late
    assert ci.should_notify(p, ci.POST_BID, late + timedelta(minutes=1))
    assert ci.round_sends(p, ci.PRE_BID) == []  # never on List 1


def test_trigger_uses_the_other_rounds_latest_close():
    p = project(T, [send("A", SENT)])
    moved = T + timedelta(days=2)
    entries = [
        {"round": ci.PRE_BID, "opened_at": SENT.isoformat(), "closed_at": T.isoformat(),
         "notified_at": SENT.isoformat()},
        {"round": ci.POST_BID, "opened_at": T.isoformat(), "closed_at": moved.isoformat(),
         "notified_at": T.isoformat()},
    ]
    # Back on List 1 after a postponement: the post_bid close is the trigger.
    assert ci.notify_trigger(p, ci.PRE_BID, entries) == moved
    # The most recent other-round entry wins, even when an older one closed later.
    older = {"round": ci.PRE_BID, "opened_at": (SENT - timedelta(days=9)).isoformat(),
             "closed_at": (T + timedelta(days=5)).isoformat()}
    assert ci.notify_trigger(p, ci.POST_BID, [older, entries[0]]) == T
    # Still open (no close yet): no contribution.
    assert ci.notify_trigger(p, ci.POST_BID, [{**entries[0], "closed_at": None}]) == T


def test_trigger_caps_another_workers_close_at_now():
    # The other worker stamped the List 1 close a moment after this tick began.
    p = project(T, [send("A", SENT)])
    now = T + timedelta(minutes=5)
    entries = [{"round": ci.PRE_BID, "opened_at": SENT.isoformat(),
                "closed_at": (now + timedelta(milliseconds=40)).isoformat()}]
    assert ci.notify_trigger(p, ci.POST_BID, entries, now) == now
    assert ci.should_notify(p, ci.POST_BID, now, entries)


def test_no_re_notify_for_the_same_trigger_but_a_newer_one_notifies():
    now = T - timedelta(days=1)
    first_open = SENT + timedelta(minutes=1)
    notified = [{"round": ci.PRE_BID, "opened_at": first_open.isoformat(),
                 "closed_at": (SENT + timedelta(hours=2)).isoformat(),
                 "notified_at": first_open.isoformat()}]
    p = project(T, [send("A", SENT)])
    at = SENT + timedelta(hours=3)
    assert ci.should_notify(p, ci.PRE_BID, at)  # the burst guard alone would notify
    assert ci.already_notified(notified, ci.PRE_BID, SENT)
    assert not ci.should_notify(p, ci.PRE_BID, at, notified)
    # A silent (never notified) earlier entry does not suppress anything.
    silent = [{**notified[0], "notified_at": None}]
    assert ci.should_notify(p, ci.PRE_BID, at, silent)
    # A late GC sent after the earlier entry opened is a newer trigger.
    late = now - timedelta(hours=2)
    p.sends.append(send("L", late))
    assert ci.should_notify(p, ci.PRE_BID, late + timedelta(hours=1), notified)
    # An earlier entry of the OTHER round never suppresses this one.
    other = [{**notified[0], "round": ci.POST_BID}]
    assert not ci.already_notified(other, ci.PRE_BID, SENT)


def test_notification_message_counts_only_the_gcs_still_to_call():
    now = T - timedelta(days=1)
    p = project(
        T, [send("A", SENT), send("B", SENT), send("L", now - timedelta(hours=1))],
        [call("A", ci.PRE_BID, "spoke", now - timedelta(days=1)),
         call("B", ci.PRE_BID, "spoke", now - timedelta(days=1)),
         call("L", ci.PRE_BID, "voicemail", now - timedelta(minutes=5))],
    )
    pre = ci.notification_message(p, ci.PRE_BID)
    assert "1 GC still to call" in pre and "3 GC" not in pre
    # post_bid has no spoke calls yet: all three are still to call.
    assert "3 GCs still to call" in ci.notification_message(p, ci.POST_BID)


def test_notification_messages_have_no_em_dash():
    p = project(T, [send("A", SENT), send("B", SENT)], name="Main St", number="4412")
    pre = ci.notification_message(p, ci.PRE_BID)
    post = ci.notification_message(p, ci.POST_BID)
    assert "Main St (#4412)" in pre and "2 GCs" in pre and "Before bid" in pre
    assert "After bid" in post
    assert chr(0x2014) not in pre + post  # the em dash
    no_date = project(None, [send("A", SENT)])
    assert "no actual bid date" in ci.notification_message(no_date, ci.PRE_BID)


# ── wire helpers ─────────────────────────────────────────────────────────────


def test_send_amounts_are_the_stamped_figures_the_gc_holds():
    row = {"material_amount": "1000.00", "gear_amount": None, "underground_amount": "250.50",
           "low_voltage_amount": None, "labor_amount": "500"}
    assert ci.send_amounts(row) == {
        "total": 1750.5,
        "sections": [
            {"key": "material", "label": "Material", "amount": 1000.0},
            {"key": "underground", "label": "Underground", "amount": 250.5},
            {"key": "labor", "label": "Labor", "amount": 500.0},
        ],
    }
    assert ci.send_amounts({}) == {"total": None, "sections": []}


def test_serialize_call_permissions():
    row = call("A", ci.PRE_BID, "spoke", T, by="author").row
    names = {"author": "Ann Author"}
    own = ci.serialize_call(row, user_id="author", role=Role.ESTIMATING_ADMIN, user_names=names)
    assert own["can_edit"] is True and own["can_delete"] is False
    assert own["created_by"] == {"id": "author", "name": "Ann Author"}
    other = ci.serialize_call(row, user_id="x", role=Role.EXECUTIVE, user_names=names)
    assert other["can_edit"] is False and other["can_delete"] is True
    it = ci.serialize_call(row, user_id="x", role=Role.IT_ADMIN, user_names=names)
    assert it["can_delete"] is True
    # An author who is now read-only cannot edit.
    ro = ci.serialize_call(row, user_id="author", role=Role.ACCOUNTANT, user_names=names)
    assert ro["can_edit"] is False and ro["can_delete"] is False
    assert set(own) == {
        "id", "project_id", "gc_id", "round", "outcome", "note", "contacts", "called_at",
        "created_by", "updated_at", "can_edit", "can_delete",
    }
    assert own["contacts"] == [{"gc_contact_id": "k1", "name": "Pat", "phone": None, "email": None}]


def test_call_log_redaction_and_history():
    now = T + timedelta(days=2)
    p = project(
        T, [send("A", SENT), send("C", T + timedelta(hours=1))],
        [call("A", ci.PRE_BID, "spoke", T - timedelta(days=1)),
         call("A", ci.POST_BID, "voicemail", now - timedelta(hours=1))],
    )
    kwargs = {"user_id": "u", "role": Role.EXECUTIVE, "user_names": {}}
    shown = ci.build_call_log(p, now, show_bid_at=True, **kwargs)
    hidden = ci.build_call_log(p, now, show_bid_at=False, **kwargs)
    assert shown["bid_at"] == T.isoformat() and hidden["bid_at"] is None
    assert hidden["bid_at_date_only"] is False
    assert shown["on_list"] == ci.POST_BID
    pre = {g["gc_id"]: g for g in shown["rounds"][ci.PRE_BID]["gcs"]}
    post = {g["gc_id"]: g for g in shown["rounds"][ci.POST_BID]["gcs"]}
    assert set(pre) == {"A"} and pre["A"]["done"] is True
    assert set(post) == {"A", "C"} and post["A"]["done"] is False
    assert len(post["A"]["calls"]) == 1


def test_call_log_for_a_no_date_project_lists_no_post_bid_gcs():
    p = project(None, [send("A", SENT)])
    log = ci.build_call_log(p, SENT + timedelta(days=1), user_id="u", role=Role.EXECUTIVE,
                            user_names={}, show_bid_at=True)
    assert log["rounds"][ci.POST_BID]["gcs"] == []
    assert log["on_list"] == ci.PRE_BID


# ── analytics ────────────────────────────────────────────────────────────────


def _analytics_fixture():
    """Two projects with T in September plus one with no T.

    P1 (T = 9/29 15:00): A called pre_bid 2 h after send after one voicemail,
    called post_bid 26 h after T; B never called pre_bid (missed), post_bid open.
    P2 (T = 9/10 10:00): A called post_bid 30 h after T; pre_bid missed; B
    missed post_bid (10 days ran out).
    P3 (no T, first sent 9/15): A open pre_bid; no post_bid slots at all.
    """
    now = pt(2026, 9, 30, 18)
    p1 = project(
        T, [send("A", SENT), send("B", SENT)],
        [call("A", ci.PRE_BID, "voicemail", SENT + timedelta(hours=1), cid="v1"),
         call("A", ci.PRE_BID, "spoke", SENT + timedelta(hours=2), cid="s1", by="u1",
              contacts=("Pat", "Lee")),
         call("A", ci.POST_BID, "spoke", T + timedelta(hours=26), cid="s2", by="u2")],
        pid="p1", number="1001", name="One",
    )
    t2 = pt(2026, 9, 10, 10)
    p2 = project(
        t2, [send("A", t2 - timedelta(days=2)), send("B", t2 - timedelta(days=2))],
        [call("A", ci.POST_BID, "spoke", t2 + timedelta(hours=30), cid="s3", by="u1")],
        pid="p2", number="1002", name="Two",
    )
    p3 = project(None, [send("A", pt(2026, 9, 15, 9))], pid="p3", number=None, name="Three")
    return now, [p1, p2, p3]


def test_analytics_round_math():
    now, projects = _analytics_fixture()
    report = ci.analytics_report(projects, now, pt(2026, 9, 1), pt(2026, 9, 30, 23, 59),
                                 {"u1": "Una", "u2": "Ugo"})
    pre, post = report["rounds"][ci.PRE_BID], report["rounds"][ci.POST_BID]
    # pre_bid: P1 A called, P1 B missed, P2 A+B missed, P3 A open.
    assert (pre["slots"], pre["called"], pre["missed"], pre["open"]) == (5, 1, 3, 1)
    assert pre["call_rate"] == 0.25
    assert pre["median_hours_to_call"] == 2.0 and pre["avg_hours_to_call"] == 2.0
    # post_bid: P1 A called (26 h), P1 B open, P2 A called (30 h), P2 B missed.
    assert (post["slots"], post["called"], post["missed"], post["open"]) == (4, 2, 1, 1)
    assert post["call_rate"] == round(2 / 3, 4)
    assert post["median_hours_to_call"] == 28.0 and post["avg_hours_to_call"] == 28.0


def test_analytics_by_gc_calls_and_missed_rows():
    now, projects = _analytics_fixture()
    report = ci.analytics_report(projects, now, pt(2026, 9, 1), pt(2026, 9, 30, 23, 59),
                                 {"u1": "Una", "u2": "Ugo"})
    by_gc = {g["gc_id"]: g for g in report["by_gc"]}
    assert (by_gc["A"]["called"], by_gc["A"]["missed"]) == (3, 1)
    assert by_gc["A"]["median_hours_to_call"] == 26.0
    assert (by_gc["B"]["called"], by_gc["B"]["missed"]) == (0, 3)
    assert by_gc["B"]["median_hours_to_call"] is None

    first = next(c for c in report["calls"] if c["project_id"] == "p1" and c["round"] == ci.PRE_BID)
    assert first["attempts_before"] == 1
    assert first["hours_to_call"] == 2.0
    assert first["called_by_name"] == "Una"
    assert first["contact_names"] == ["Pat", "Lee"]
    assert [c["called_at"] for c in report["calls"]] == sorted(
        (c["called_at"] for c in report["calls"]), reverse=True
    )

    missed = {(m["project_id"], m["gc_id"], m["round"]): m for m in report["missed"]}
    assert set(missed) == {
        ("p1", "B", ci.PRE_BID), ("p2", "A", ci.PRE_BID), ("p2", "B", ci.PRE_BID),
        ("p2", "B", ci.POST_BID),
    }
    assert missed[("p1", "B", ci.PRE_BID)]["window_closed_at"] == T.isoformat()
    assert missed[("p2", "B", ci.POST_BID)]["window_closed_at"] == (
        ci.post_bid_closes_at(pt(2026, 9, 10, 10)).isoformat()
    )
    # No bid dates in the payload.
    assert "bid_at" not in str(report)


def test_analytics_range_anchors_each_slot_on_its_window_open():
    now, projects = _analytics_fixture()
    # Only 9/12 to 9/20: P1 (sent 9/22, T 9/29) and P2 (sent 9/8, T 9/10)
    # fall out; P3's pre_bid window opened at its send on 9/15.
    report = ci.analytics_report(projects, now, pt(2026, 9, 12), pt(2026, 9, 20))
    assert report["rounds"][ci.PRE_BID]["slots"] == 1
    assert report["rounds"][ci.POST_BID]["slots"] == 0
    assert report["rounds"][ci.PRE_BID]["call_rate"] is None


def test_analytics_no_date_project_with_outcome_is_missed_at_the_outcome():
    recorded = pt(2026, 9, 20, 9)
    p = project(None, [send("A", pt(2026, 9, 15, 9))], outcome_result="lost",
                outcome_recorded_at=recorded)
    report = ci.analytics_report([p], pt(2026, 9, 30), pt(2026, 9, 1), pt(2026, 9, 30))
    assert report["rounds"][ci.PRE_BID]["missed"] == 1
    assert report["missed"][0]["window_closed_at"] == recorded.isoformat()


def test_analytics_counts_pre_bid_slots_of_a_bid_tomorrow_but_not_its_post_bid():
    now = pt(2026, 9, 30, 12)
    tomorrow = project(pt(2026, 10, 1, 14), [send("A", pt(2026, 9, 27, 9))], pid="soon")
    # The preset ranges end at now; T is after it, the send is inside it.
    report = ci.analytics_report([tomorrow], now, now - timedelta(days=30), now)
    assert report["rounds"][ci.PRE_BID]["slots"] == 1
    assert report["rounds"][ci.PRE_BID]["open"] == 1
    # A custom range reaching past now still leaves out the unopened post_bid window.
    wide = ci.analytics_report([tomorrow], now, pt(2026, 9, 1), pt(2026, 10, 31))
    assert wide["rounds"][ci.POST_BID]["slots"] == 0


def test_analytics_go_live_cut_drops_only_missed_slots_closed_before_it():
    now, projects = _analytics_fixture()
    # Go-live 9/20: P2's pre_bid windows (closed 9/10) drop; P2 B post_bid
    # (closed 9/20 10:00, after go-live midnight) and P1 B pre_bid (9/29) stay.
    report = ci.analytics_report(projects, now, pt(2026, 9, 1), pt(2026, 9, 30, 23, 59),
                                 started_at=pt(2026, 9, 20))
    missed = {(m["project_id"], m["gc_id"], m["round"]) for m in report["missed"]}
    assert missed == {("p1", "B", ci.PRE_BID), ("p2", "B", ci.POST_BID)}
    pre = report["rounds"][ci.PRE_BID]
    assert (pre["slots"], pre["called"], pre["missed"], pre["open"]) == (3, 1, 1, 1)
    # Go-live at now: nothing historic is missed; calls and open slots stay.
    at_now = ci.analytics_report(projects, now, pt(2026, 9, 1), pt(2026, 9, 30, 23, 59),
                                 started_at=now)
    assert at_now["missed"] == []
    assert at_now["rounds"][ci.POST_BID]["called"] == 2
    assert at_now["rounds"][ci.PRE_BID]["open"] == 1


# ── poller (claim, burst guard, close) ───────────────────────────────────────


@pytest.fixture
def poller(monkeypatch):
    """A fake database plus captured notify / dismiss calls."""
    sent: list[tuple] = []
    dismissed: list[dict] = []
    db = FakeSB()
    monkeypatch.setattr(ci, "get_supabase", lambda: db)
    monkeypatch.setattr(
        ci, "notify_role",
        lambda role, pid, type_, msg, **kw: sent.append((role, pid, type_, kw.get("metadata"))),
    )
    monkeypatch.setattr(ci, "dismiss_notifications", lambda **kw: dismissed.append(kw))
    return db, sent, dismissed


def _seed(db, *, actual, sent_at, pid="p1", gcs=("A",), **project_kw):
    db.tables.setdefault("projects", []).append(
        {"id": pid, "name": f"Project {pid}", "number": "1001",
         "actual_bid_at": actual.isoformat() if actual else None,
         "abandoned_at": None, "current_stage": "submitted", "test_session_id": None,
         **project_kw}
    )
    for gc in gcs:
        db.tables.setdefault("proposal_sends", []).append(
            {"id": f"ps-{pid}-{gc}", "project_id": pid, "gc_id": gc, "gc_name": f"GC {gc}",
             "status": "sent", "sent_at": sent_at.isoformat(), "sent_via": "email",
             "material_amount": "10", "gear_amount": None, "underground_amount": None,
             "low_voltage_amount": None, "labor_amount": "5"}
        )


def test_poller_claims_once_and_notifies_executive_and_labor(poller):
    db, sent, _ = poller
    now = SENT + timedelta(hours=3)
    _seed(db, actual=T, sent_at=SENT)
    assert ci.poll_once(now) == {"claimed": 1, "notified": 1, "closed": 0}
    entries = db.tables["call_in_entries"]
    assert len(entries) == 1 and entries[0]["round"] == ci.PRE_BID
    assert entries[0]["notified_at"] == now.isoformat()
    assert [s[0] for s in sent] == [Role.EXECUTIVE, Role.ESTIMATING_ENGINEER_LABOR]
    assert {s[2] for s in sent} == {"call_in_pre_bid"}
    assert sent[0][3] == {"round": ci.PRE_BID, "entry_id": entries[0]["id"]}
    # Second tick: already claimed, nothing new.
    assert ci.poll_once(now + timedelta(minutes=1)) == {"claimed": 0, "notified": 0, "closed": 0}
    assert len(sent) == 2


def test_poller_losing_the_claim_to_another_worker_never_notifies(poller):
    db, sent, _ = poller
    _seed(db, actual=T, sent_at=SENT)
    db.force_claim_conflict = True  # the other worker's insert won
    assert ci.poll_once(SENT + timedelta(hours=3)) == {"claimed": 0, "notified": 0, "closed": 0}
    assert sent == []


def test_claim_entry_is_idempotent_against_the_partial_unique_index(poller):
    db, _, _ = poller
    first = ci.claim_entry(db, "p1", ci.PRE_BID, SENT)
    assert first is not None
    assert ci.claim_entry(db, "p1", ci.PRE_BID, SENT) is None
    # A different round, or the same round after the first closed, claims fine.
    assert ci.claim_entry(db, "p1", ci.POST_BID, SENT) is not None
    first_row = next(e for e in db.tables["call_in_entries"] if e["id"] == first["id"])
    first_row["closed_at"], first_row["close_reason"] = SENT.isoformat(), "cleared"
    assert ci.claim_entry(db, "p1", ci.PRE_BID, SENT) is not None


def test_poller_burst_guard_claims_old_entries_silently(poller):
    db, sent, _ = poller
    _seed(db, actual=T, sent_at=SENT)
    now = SENT + timedelta(days=3)  # sent 3 days ago: a release or an outage
    assert ci.poll_once(now) == {"claimed": 1, "notified": 0, "closed": 0}
    assert db.tables["call_in_entries"][0]["notified_at"] is None
    assert sent == []
    # It still counts in the badge.
    assert ci.current_summary(now) == {"open_count": 1, "pre_bid": 1, "post_bid": 0}


def test_poller_closes_at_bid_time_and_opens_list2_with_a_notice(poller):
    db, sent, dismissed = poller
    _seed(db, actual=T, sent_at=SENT)
    ci.poll_once(SENT + timedelta(hours=1))
    pre_entry = db.tables["call_in_entries"][0]
    stats = ci.poll_once(T + timedelta(minutes=5))
    assert stats == {"claimed": 1, "notified": 1, "closed": 1}
    assert pre_entry["closed_at"] is not None and pre_entry["close_reason"] == "window_closed"
    assert dismissed[-1] == {
        "project_id": "p1", "types": ["call_in_pre_bid"],
        "metadata_eq": {"entry_id": pre_entry["id"]},
    }
    post = [e for e in db.tables["call_in_entries"] if e["round"] == ci.POST_BID]
    assert len(post) == 1 and post[0]["closed_at"] is None
    assert sent[-1][2] == "call_in_post_bid"


def test_poller_closes_left_when_abandoned(poller):
    db, _, dismissed = poller
    _seed(db, actual=T, sent_at=SENT)
    ci.poll_once(SENT + timedelta(hours=1))
    db.tables["projects"][0]["abandoned_at"] = (SENT + timedelta(hours=2)).isoformat()
    assert ci.poll_once(SENT + timedelta(hours=3))["closed"] == 1
    entry = db.tables["call_in_entries"][0]
    assert entry["close_reason"] == "left"
    assert dismissed and dismissed[-1]["types"] == ["call_in_pre_bid"]


def test_poller_closes_cleared_when_every_gc_spoke(poller):
    db, _, _ = poller
    _seed(db, actual=T, sent_at=SENT)
    ci.poll_once(SENT + timedelta(hours=1))
    db.tables.setdefault("call_in_calls", []).append(
        {"id": "c1", "project_id": "p1", "gc_id": "A", "round": ci.PRE_BID, "outcome": "spoke",
         "note": "ok", "contacts": [{"name": "Pat"}],
         "called_at": (SENT + timedelta(hours=2)).isoformat(), "created_by": "u1"}
    )
    assert ci.poll_once(SENT + timedelta(hours=3))["closed"] == 1
    assert db.tables["call_in_entries"][0]["close_reason"] == "cleared"


def test_poller_late_gc_opens_a_new_entry_and_notifies_again(poller):
    db, sent, _ = poller
    _seed(db, actual=T, sent_at=SENT)
    ci.poll_once(SENT + timedelta(hours=1))
    db.tables.setdefault("call_in_calls", []).append(
        {"id": "c1", "project_id": "p1", "gc_id": "A", "round": ci.PRE_BID, "outcome": "spoke",
         "note": "ok", "contacts": [{"name": "Pat"}],
         "called_at": (SENT + timedelta(hours=2)).isoformat(), "created_by": "u1"}
    )
    ci.poll_once(SENT + timedelta(hours=3))
    late = T - timedelta(days=1)
    db.tables["proposal_sends"].append(
        {"id": "ps-late", "project_id": "p1", "gc_id": "L", "gc_name": "GC L", "status": "sent",
         "sent_at": late.isoformat(), "sent_via": "external"}
    )
    before = len(sent)
    assert ci.poll_once(late + timedelta(hours=1)) == {"claimed": 1, "notified": 1, "closed": 0}
    assert len(sent) == before + 2
    pre = [e for e in db.tables["call_in_entries"] if e["round"] == ci.PRE_BID]
    assert len(pre) == 2 and [e["close_reason"] for e in pre] == ["cleared", None]


def _add_call(db, cid, gc, round_, outcome, at, pid="p1"):
    db.tables.setdefault("call_in_calls", []).append(
        {"id": cid, "project_id": pid, "gc_id": gc, "round": round_, "outcome": outcome,
         "note": "ok", "contacts": [{"name": "Pat"}], "called_at": at.isoformat(),
         "created_by": "u1"}
    )


def _entries(db, round_=None):
    rows = db.tables.get("call_in_entries", [])
    return [e for e in rows if round_ is None or e["round"] == round_]


def test_poller_notifies_a_proposal_recorded_as_sent_days_after_t(poller):
    db, sent, _ = poller
    recorded = T + timedelta(days=3)  # Mark as submitted, days after the bid
    _seed(db, actual=T, sent_at=recorded)
    assert ci.poll_once(recorded + timedelta(minutes=2)) == {
        "claimed": 1, "notified": 1, "closed": 0}
    (entry,) = _entries(db)
    assert entry["round"] == ci.POST_BID and entry["notified_at"] is not None
    assert {s[2] for s in sent} == {"call_in_post_bid"}


def test_poller_null_date_then_past_date_notifies_via_the_transition(poller):
    db, sent, _ = poller
    _seed(db, actual=None, sent_at=SENT)
    ci.poll_once(SENT + timedelta(hours=1))
    assert len(sent) == 2  # List 1 notice
    now = SENT + timedelta(days=5)
    # The date is entered after the fact, and it is already two days past.
    db.tables["projects"][0]["actual_bid_at"] = (now - timedelta(days=2)).isoformat()
    assert ci.poll_once(now) == {"claimed": 1, "notified": 1, "closed": 1}
    (pre,) = _entries(db, ci.PRE_BID)
    (post,) = _entries(db, ci.POST_BID)
    assert pre["close_reason"] == "window_closed"
    # Closed before the claim in the same tick: the new entry's trigger is it.
    assert pre["closed_at"] == now.isoformat() == post["opened_at"]
    assert post["notified_at"] == now.isoformat()
    assert sent[-1][2] == "call_in_post_bid"


def test_poller_postponement_back_to_list1_notifies(poller):
    db, sent, _ = poller
    _seed(db, actual=T, sent_at=SENT)
    ci.poll_once(SENT + timedelta(hours=1))
    ci.poll_once(T + timedelta(minutes=5))
    assert [s[2] for s in sent] == ["call_in_pre_bid"] * 2 + ["call_in_post_bid"] * 2
    now = T + timedelta(days=2)
    db.tables["projects"][0]["actual_bid_at"] = (now + timedelta(days=5)).isoformat()
    assert ci.poll_once(now) == {"claimed": 1, "notified": 1, "closed": 1}
    (post,) = _entries(db, ci.POST_BID)
    assert post["close_reason"] == "left"
    reopened = [e for e in _entries(db, ci.PRE_BID) if e["closed_at"] is None]
    assert len(reopened) == 1 and reopened[0]["notified_at"] == now.isoformat()
    assert sent[-1][2] == "call_in_pre_bid"


def _cleared_list1(db):
    """List 1 notified, then cleared by a spoke call with the only GC."""
    _seed(db, actual=T, sent_at=SENT)
    ci.poll_once(SENT + timedelta(hours=1))
    _add_call(db, "c-spoke", "A", ci.PRE_BID, "spoke", SENT + timedelta(hours=2))
    assert ci.poll_once(SENT + timedelta(hours=2, minutes=1))["closed"] == 1


def test_poller_edit_of_the_clearing_call_reopens_silently(poller):
    db, sent, _ = poller
    _cleared_list1(db)
    before = len(sent)
    db.tables["call_in_calls"][0]["outcome"] = "voicemail"  # corrected by its author
    now = SENT + timedelta(hours=3)
    assert ci.poll_once(now) == {"claimed": 1, "notified": 0, "closed": 0}
    assert len(sent) == before
    reopened = [e for e in _entries(db, ci.PRE_BID) if e["closed_at"] is None]
    assert len(reopened) == 1 and reopened[0]["notified_at"] is None
    # Claimed all the same: the badge and the page are right.
    assert ci.current_summary(now) == {"open_count": 1, "pre_bid": 1, "post_bid": 0}


def test_poller_delete_of_the_clearing_call_reopens_silently(poller):
    db, sent, _ = poller
    _cleared_list1(db)
    before = len(sent)
    db.tables["call_in_calls"].clear()  # an Executive deleted it
    assert ci.poll_once(SENT + timedelta(hours=3)) == {"claimed": 1, "notified": 0, "closed": 0}
    assert len(sent) == before


def test_poller_a_later_late_gc_still_notifies_after_a_silent_reopen(poller):
    db, sent, _ = poller
    _cleared_list1(db)
    db.tables["call_in_calls"][0]["outcome"] = "voicemail"
    ci.poll_once(SENT + timedelta(hours=3))  # silent re-open
    _add_call(db, "c-spoke-2", "A", ci.PRE_BID, "spoke", SENT + timedelta(hours=4))
    assert ci.poll_once(SENT + timedelta(hours=4, minutes=1))["closed"] == 1
    before = len(sent)
    late = T - timedelta(days=1)
    db.tables["proposal_sends"].append(
        {"id": "ps-late", "project_id": "p1", "gc_id": "L", "gc_name": "GC L", "status": "sent",
         "sent_at": late.isoformat(), "sent_via": "external"}
    )
    assert ci.poll_once(late + timedelta(hours=1)) == {"claimed": 1, "notified": 1, "closed": 0}
    assert len(sent) == before + 2


def test_poller_list2_correction_after_the_transition_stays_silent(poller):
    db, sent, _ = poller
    _seed(db, actual=T, sent_at=SENT)
    ci.poll_once(SENT + timedelta(hours=1))
    ci.poll_once(T + timedelta(minutes=5))  # List 1 closes, List 2 notifies
    # All inside the 24 hours after T, where the burst guard alone would notify.
    _add_call(db, "c-post", "A", ci.POST_BID, "spoke", T + timedelta(hours=2))
    assert ci.poll_once(T + timedelta(hours=2, minutes=1))["closed"] == 1
    before = len(sent)
    db.tables["call_in_calls"].clear()
    assert ci.poll_once(T + timedelta(hours=3)) == {"claimed": 1, "notified": 0, "closed": 0}
    assert len(sent) == before


def test_poller_a_late_gc_on_list2_notifies_again(poller):
    db, sent, _ = poller
    _seed(db, actual=T, sent_at=SENT)
    ci.poll_once(T + timedelta(minutes=5))
    _add_call(db, "c-post", "A", ci.POST_BID, "spoke", T + timedelta(days=1))
    ci.poll_once(T + timedelta(days=1, minutes=1))
    before = len(sent)
    recorded = T + timedelta(days=3)
    db.tables["proposal_sends"].append(
        {"id": "ps-l", "project_id": "p1", "gc_id": "L", "gc_name": "GC L", "status": "sent",
         "sent_at": recorded.isoformat(), "sent_via": "external"}
    )
    assert ci.poll_once(recorded + timedelta(minutes=1)) == {
        "claimed": 1, "notified": 1, "closed": 0}
    assert len(sent) == before + 2


def test_poller_ignores_projects_with_no_sent_proposal(poller):
    db, _, _ = poller
    _seed(db, actual=T, sent_at=SENT)
    db.tables["proposal_sends"][0]["status"] = "generated"
    assert ci.poll_once(SENT + timedelta(hours=1)) == {"claimed": 0, "notified": 0, "closed": 0}


def test_current_lists_shape(poller):
    db, _, _ = poller
    _seed(db, actual=None, sent_at=SENT, pid="nodate")
    _seed(db, actual=T, sent_at=SENT, pid="dated")
    ci.poll_once(SENT + timedelta(hours=1))
    out = ci.current_lists(SENT + timedelta(hours=2))
    assert set(out) == {"now", "pre_bid", "post_bid"}
    assert [e["project_id"] for e in out["pre_bid"]] == ["dated", "nodate"]
    assert out["pre_bid"][1]["band"] == "no_bid_date"
    assert all(e["entered_at"] for e in out["pre_bid"])
    required = {
        "project_id", "project_number", "project_name", "round", "bid_at", "bid_at_date_only",
        "bid_at_missing", "window_closes_at", "band", "days_since_bid", "outcome", "gcs_total",
        "gcs_done", "entered_at",
    }
    assert required <= set(out["pre_bid"][0])


def test_settings_default_on_and_pinned_off_in_tests():
    from app.core.config import Settings, get_settings

    assert Settings.model_fields["call_in_enabled"].default is True
    assert get_settings().call_in_enabled is False


def test_notification_email_heading_and_deep_link():
    from app.services import notification_email as ne

    assert ne.heading_for("call_in_pre_bid") == "A project is ready for before-bid calls"
    assert ne.heading_for("call_in_post_bid") == "A project is ready for after-bid calls"
    base = ne.get_settings().frontend_url.rstrip("/")
    assert ne._deep_link("p1", Role.EXECUTIVE.value, "call_in_pre_bid") == (
        f"{base}/calling-in?round=pre_bid&project=p1"
    )
    assert ne._deep_link("p1", Role.ESTIMATING_ENGINEER_LABOR.value, "call_in_post_bid") == (
        f"{base}/calling-in?round=post_bid&project=p1"
    )

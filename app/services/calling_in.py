"""Calling In (docs/CALLING_IN.md): who still has to be called about our proposal.

After a proposal goes to a GC, someone calls that GC twice: once before the
project's actual bid time (round `pre_bid`, "List 1") and once in the 10 days
after it (round `post_bid`, "List 2"). A GC is done for a round only when a
call with outcome `spoke` is logged for it in that round.

Layout of this module:

- PURE membership, band and slot computation over plain dataclasses
  (`ProjectFacts` and friends). No I/O: this is the tested seam, and every
  route and the poller go through it, so list membership is always computed
  live from current data and never stored as a flag.
- Loaders that read the facts through the sync Supabase SDK (call them from
  plain `def` handlers or through `asyncio.to_thread`).
- The poller (every 60 s, main.py lifespan, `CALL_IN_ENABLED`): closes the
  `call_in_entries` row (dismissing its notifications) once the project
  leaves a list, then claims a row when a project lands on a list and
  notifies once per trigger (24-hour burst guard; a correction that re-opens
  a list is claimed silently).

All times are aware datetimes; Pacific (America/Los_Angeles) is used only for
date-only detection, the end-of-day rule and the day bands.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone

from postgrest.exceptions import APIError

from app.core.config import get_settings
from app.core.roles import WRITER_ROLES, Role
from app.core.supabase_client import get_supabase
from app.services.bid_invitations import REPORT_TZ
from app.services.notifications import dismiss_notifications, notify_role

logger = logging.getLogger(__name__)

UTC = timezone.utc
# Company time (the same zone Bids Today and Bid Invitations use).
PT = REPORT_TZ

PRE_BID = "pre_bid"
POST_BID = "post_bid"
ROUNDS = (PRE_BID, POST_BID)
OUTCOMES = ("spoke", "voicemail", "no_answer")
DONE_OUTCOME = "spoke"

# List 2 runs from T for this many Pacific calendar days (T's wall time on
# day 10 is when it closes); days 1 to 7 are the target call window.
POST_BID_DAYS = 10
CALL_NOW_LAST_DAY = 7

# A newly claimed entry notifies only when its trigger moment is this recent.
BURST_GUARD = timedelta(hours=24)

NOTIFY_TYPES = {PRE_BID: "call_in_pre_bid", POST_BID: "call_in_post_bid"}
NOTIFY_ROLES = (Role.EXECUTIVE, Role.ESTIMATING_ENGINEER_LABOR)
# Who may delete a logged call (docs/CALLING_IN.md section 6).
DELETE_ROLES = frozenset({Role.EXECUTIVE, Role.IT_ADMIN})

# Stages that are never on a list: declined bids and never-bid work.
EXCLUDED_STAGES = ("declined", "pm_only", "cp_only")

# The per-GC figures on a proposal_sends row, in display order (the same
# stamps the Win/Loss grid sums into "our amount").
AMOUNT_SECTIONS = (
    ("material", "Material"),
    ("gear", "Gear"),
    ("underground", "Underground"),
    ("low_voltage", "Low voltage"),
    ("labor", "Labor"),
)

PROJECT_COLS = "id, name, number, actual_bid_at, abandoned_at, current_stage, test_session_id"
SEND_COLS = (
    "id, project_id, gc_id, gc_name, sent_at, sent_via, material_amount, gear_amount,"
    " underground_amount, low_voltage_amount, labor_amount"
)
CALL_COLS = (
    "id, project_id, gc_id, round, outcome, note, contacts, called_at, created_by,"
    " edited_by, created_at, updated_at"
)
ENTRY_COLS = "id, project_id, round, opened_at, notified_at, closed_at, close_reason"

_PAGE = 1000
_CHUNK = 100


# ── time helpers (pure) ──────────────────────────────────────────────────────


def parse_ts(value) -> datetime | None:
    """An ISO timestamp (PostgREST text) or datetime as an aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt is not None else None


def is_pacific_midnight(dt: datetime) -> bool:
    local = dt.astimezone(PT)
    return (local.hour, local.minute, local.second, local.microsecond) == (0, 0, 0, 0)


def end_of_pacific_day(dt: datetime) -> datetime:
    """23:59:59.999 America/Los_Angeles on dt's Pacific calendar day, as UTC."""
    local = dt.astimezone(PT)
    return datetime.combine(local.date(), time(23, 59, 59, 999000), tzinfo=PT).astimezone(UTC)


def effective_bid_time(actual_bid_at: datetime | None) -> tuple[datetime | None, bool]:
    """(T, date_only) from projects.actual_bid_at.

    Date-only detection (section 3): the 0130 flag
    rfp_created_projects.bid_time_unknown only ever marks a value stored at
    exactly midnight Pacific, and a value at exactly 00:00:00 Pacific is
    date-only on its own (nobody bids at midnight). The flag case is therefore
    a subset of the midnight rule, so the flag never needs to be read. A
    date-only T becomes the end of that Pacific day.
    """
    if actual_bid_at is None:
        return None, False
    if is_pacific_midnight(actual_bid_at):
        return end_of_pacific_day(actual_bid_at), True
    return actual_bid_at.astimezone(UTC), False


def post_bid_closes_at(bid_at: datetime) -> datetime:
    """T plus 10 Pacific calendar days at T's wall time (DST-safe: a window
    spanning a clock change is 239 or 241 real hours, never shifted by one)."""
    return (bid_at.astimezone(PT) + timedelta(days=POST_BID_DAYS)).astimezone(UTC)


def days_since_bid(bid_at: datetime, now: datetime) -> int:
    """Whole Pacific calendar days from T's day to now's day (0 on T's day)."""
    return (now.astimezone(PT).date() - bid_at.astimezone(PT).date()).days


def post_bid_band(days: int) -> str:
    if days <= 0:
        return "opens_soon"
    if days <= CALL_NOW_LAST_DAY:
        return "call_now"
    return "overdue"


# ── facts (pure) ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SendFact:
    """One proposal_sends row at status 'sent' (one per project and GC)."""

    id: str
    gc_id: str
    gc_name: str
    sent_at: datetime
    sent_via: str = "email"
    row: dict = field(default_factory=dict, compare=False, hash=False)


@dataclass(frozen=True)
class CallFact:
    id: str
    gc_id: str
    round: str
    outcome: str
    called_at: datetime
    created_by: str | None = None
    contact_names: tuple[str, ...] = ()
    row: dict = field(default_factory=dict, compare=False, hash=False)


@dataclass
class ProjectFacts:
    id: str
    name: str
    number: str | None = None
    actual_bid_at: datetime | None = None
    abandoned: bool = False
    stage: str | None = None
    test_session: bool = False
    outcome_result: str | None = None  # bid_outcomes.result: won / lost / no_award
    outcome_recorded_at: datetime | None = None
    sends: list[SendFact] = field(default_factory=list)
    calls: list[CallFact] = field(default_factory=list)

    @property
    def excluded(self) -> bool:
        """Abandoned or declined (or never-bid) work and test-session projects
        are never on a list, and no call window is open on them."""
        return bool(self.abandoned or self.test_session or self.stage in EXCLUDED_STAGES)

    @property
    def has_outcome(self) -> bool:
        return self.outcome_result is not None

    @property
    def bid_time(self) -> tuple[datetime | None, bool]:
        return effective_bid_time(self.actual_bid_at)


@dataclass(frozen=True)
class Window:
    """A round's call window at the project level."""

    open: bool
    opens_at: datetime | None
    closes_at: datetime | None


def round_window(p: ProjectFacts, round_: str, now: datetime) -> Window:
    """pre_bid: open while now < T; with no T, open until a bid outcome is
    recorded (the project has clearly bid). post_bid: T <= now < T + 10 days;
    never open without a T. Always closed on an excluded project."""
    bid_at, _ = p.bid_time
    if round_ == PRE_BID:
        if bid_at is None:
            closes = p.outcome_recorded_at if p.has_outcome else None
            is_open = not p.has_outcome
        else:
            closes = bid_at
            is_open = now < bid_at
        opens = None
    else:
        if bid_at is None:
            return Window(False, None, None)
        opens, closes = bid_at, post_bid_closes_at(bid_at)
        is_open = opens <= now < closes
    return Window(is_open and not p.excluded, opens, closes)


def round_sends(p: ProjectFacts, round_: str) -> list[SendFact]:
    """The GCs a round covers: pre_bid only the GCs sent before T (all of them
    while T is unknown); post_bid every GC we sent to, whenever."""
    bid_at, _ = p.bid_time
    if round_ == PRE_BID and bid_at is not None:
        return [s for s in p.sends if s.sent_at < bid_at]
    return list(p.sends)


def done_at_by_gc(p: ProjectFacts, round_: str) -> dict[str, datetime]:
    """gc_id -> the first `spoke` call in the round (only spoke marks done)."""
    out: dict[str, datetime] = {}
    for c in p.calls:
        if c.round == round_ and c.outcome == DONE_OUTCOME:
            if c.gc_id not in out or c.called_at < out[c.gc_id]:
                out[c.gc_id] = c.called_at
    return out


def is_member(p: ProjectFacts, round_: str, now: datetime) -> bool:
    """On the round's list: window open and at least one covered GC not done."""
    if not round_window(p, round_, now).open:
        return False
    done = done_at_by_gc(p, round_)
    return any(s.gc_id not in done for s in round_sends(p, round_))


def on_list(p: ProjectFacts, now: datetime) -> str | None:
    """The list the project is on now. The rounds never overlap (pre_bid needs
    now < T, post_bid needs T <= now)."""
    for round_ in ROUNDS:
        if is_member(p, round_, now):
            return round_
    return None


def gc_window_open(p: ProjectFacts, round_: str, gc_id: str, now: datetime) -> bool:
    """A call may be logged for this GC in this round right now (section 3,
    logging window): the round's window is open and the round covers the GC.
    An already-done GC still qualifies (extra calls are history)."""
    if not round_window(p, round_, now).open:
        return False
    return any(s.gc_id == gc_id for s in round_sends(p, round_))


def close_reason(p: ProjectFacts | None, round_: str, now: datetime) -> str:
    """Why an open entry is no longer on its list: `cleared` (every GC done),
    `window_closed` (the bid time passed, the 10 days ran out, or a no-date
    project recorded its outcome) or `left` (postponed, abandoned, dates
    cleared, GCs removed)."""
    if p is None or p.excluded:
        return "left"
    window = round_window(p, round_, now)
    if not window.open:
        if window.closes_at is not None and now >= window.closes_at:
            return "window_closed"
        return "left"
    gcs = round_sends(p, round_)
    done = done_at_by_gc(p, round_)
    if gcs and all(s.gc_id in done for s in gcs):
        return "cleared"
    return "left"


def other_round_closed_at(entries: Iterable[dict], round_: str) -> datetime | None:
    """closed_at of this project's most recent call_in_entries row (by
    opened_at) in the OTHER round, or None (no such row, or it is still open).
    This is the moment the project moved between lists: List 1 to List 2 at
    the bid time or when a missing date is entered, List 2 back to List 1 on a
    postponement."""
    other = [e for e in entries if e.get("round") in ROUNDS and e.get("round") != round_]
    if not other:
        return None
    floor = datetime.min.replace(tzinfo=UTC)
    latest = max(other, key=lambda e: parse_ts(e.get("opened_at")) or floor)
    return parse_ts(latest.get("closed_at"))


def notify_trigger(
    p: ProjectFacts,
    round_: str,
    entries: Iterable[dict] = (),
    now: datetime | None = None,
) -> datetime | None:
    """The moment an entry became due (section 5).

    List 1: max(newest pre_bid-eligible sent_at, the other round's latest
    close). List 2: max(T, newest eligible sent_at, the other round's latest
    close). So a late GC (sent before T, or recorded as sent after T through
    Mark as submitted / the 0140 wizard), a missing date entered after the
    fact and a postponement all count from when they actually happened, not
    from T. `entries` are this project's call_in_entries rows (any round);
    the other round's close is capped at `now` (another worker may have
    stamped it a few milliseconds after this tick started)."""
    times = [s.sent_at for s in round_sends(p, round_)]
    if round_ == POST_BID:
        bid_at, _ = p.bid_time
        if bid_at is not None:
            times.append(bid_at)
    moved = other_round_closed_at(entries, round_)
    if moved is not None:
        times.append(min(moved, now) if now is not None else moved)
    return max(times) if times else None


def already_notified(entries: Iterable[dict], round_: str, trigger: datetime) -> bool:
    """An earlier entry for this round already notified for this trigger (it
    opened at or after it). A correction that re-opens the list (a Spoke call
    edited to voicemail, or the clearing call deleted) is then claimed
    silently; a genuinely newer trigger still notifies."""
    for e in entries:
        if e.get("round") != round_ or not e.get("notified_at"):
            continue
        opened = parse_ts(e.get("opened_at"))
        if opened is not None and opened >= trigger:
            return True
    return False


def should_notify(
    p: ProjectFacts, round_: str, now: datetime, entries: Iterable[dict] = ()
) -> bool:
    """Notify a newly claimed entry only when its trigger is within the last
    24 hours (the burst guard: the release, with no prior entries, claims
    everything already due silently) and no earlier entry for the round has
    already notified for that trigger. `entries` must not include the entry
    being claimed."""
    entries = list(entries)
    trigger = notify_trigger(p, round_, entries, now)
    if trigger is None or not (timedelta(0) <= now - trigger <= BURST_GUARD):
        return False
    return not already_notified(entries, round_, trigger)


def outcome_label(result: str | None) -> str | None:
    """The project-level outcome as the page shows it (no_award is not shown)."""
    return result if result in ("won", "lost") else None


def build_entry(
    p: ProjectFacts, round_: str, now: datetime, entered_at: datetime | None = None
) -> dict:
    """The Entry wire shape (section 6) for any round, on the list or not.

    `on_list` is an additive field (not in the original contract) telling a
    history view whether the project is currently on this round's list."""
    bid_at, date_only = p.bid_time
    gcs = round_sends(p, round_)
    done = done_at_by_gc(p, round_)
    days: int | None = None
    if round_ == PRE_BID:
        band = "no_bid_date" if bid_at is None else "open"
        closes = bid_at
    else:
        closes = post_bid_closes_at(bid_at) if bid_at else None
        if bid_at is None:
            band = "no_bid_date"
        elif now < bid_at:
            band = "opens_soon"
        else:
            days = days_since_bid(bid_at, now)
            band = post_bid_band(days)
    return {
        "project_id": p.id,
        "project_number": p.number,
        "project_name": p.name,
        "round": round_,
        "bid_at": iso(bid_at),
        "bid_at_date_only": date_only,
        "bid_at_missing": bid_at is None,
        "window_closes_at": iso(closes),
        "band": band,
        "days_since_bid": days,
        "outcome": outcome_label(p.outcome_result),
        "gcs_total": len(gcs),
        "gcs_done": sum(1 for s in gcs if s.gc_id in done),
        "entered_at": iso(entered_at),
        "on_list": is_member(p, round_, now),
    }


def _sort_key(p: ProjectFacts) -> tuple:
    bid_at, _ = p.bid_time
    return (bid_at is None, bid_at or datetime.max.replace(tzinfo=UTC), (p.name or "").lower())


def compute_lists(
    projects: Iterable[ProjectFacts],
    now: datetime,
    entered: dict[tuple[str, str], datetime] | None = None,
) -> dict[str, list[dict]]:
    """Both lists, sorted by bid_at ascending (missing dates last)."""
    entered = entered or {}
    out: dict[str, list[dict]] = {PRE_BID: [], POST_BID: []}
    for round_ in ROUNDS:
        members = [p for p in projects if is_member(p, round_, now)]
        members.sort(key=_sort_key)
        out[round_] = [build_entry(p, round_, now, entered.get((p.id, round_))) for p in members]
    return out


def summary_counts(projects: Iterable[ProjectFacts], now: datetime) -> dict:
    projects = list(projects)
    pre = sum(1 for p in projects if is_member(p, PRE_BID, now))
    post = sum(1 for p in projects if is_member(p, POST_BID, now))
    return {"open_count": pre + post, "pre_bid": pre, "post_bid": post}


def send_amounts(row: dict) -> dict:
    """What this GC actually holds: the stamped section figures on its
    proposal_sends row (the Win/Loss grid's source), total = their sum."""
    from app.services.outcome import our_amount_of

    sections = []
    for key, label in AMOUNT_SECTIONS:
        value = row.get(f"{key}_amount")
        if value is not None:
            sections.append({"key": key, "label": label, "amount": float(value)})
    total = our_amount_of(row)
    return {"total": float(total) if total is not None else None, "sections": sections}


def serialize_call(
    row: dict, *, user_id: str, role: Role, user_names: dict[str, str | None]
) -> dict:
    """The Call wire shape (section 6). can_edit: the author, while they hold a
    writer role; can_delete: Executive and IT Admin."""
    created_by = row.get("created_by")
    contacts = [
        {
            "gc_contact_id": c.get("gc_contact_id"),
            "name": c.get("name") or "",
            "phone": c.get("phone"),
            "email": c.get("email"),
        }
        for c in (row.get("contacts") or [])
        if isinstance(c, dict)
    ]
    called_at = parse_ts(row.get("called_at"))
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "gc_id": row["gc_id"],
        "round": row["round"],
        "outcome": row["outcome"],
        "note": row.get("note") or "",
        "contacts": contacts,
        "called_at": iso(called_at),
        "created_by": {
            "id": created_by,
            "name": user_names.get(created_by) if created_by else None,
        },
        "updated_at": iso(parse_ts(row.get("updated_at")) or called_at),
        "can_edit": bool(created_by) and created_by == user_id and role in WRITER_ROLES,
        "can_delete": role in DELETE_ROLES,
    }


def _calls_newest_first(p: ProjectFacts, round_: str, gc_id: str) -> list[dict]:
    calls = [c for c in p.calls if c.round == round_ and c.gc_id == gc_id]
    calls.sort(key=lambda c: (c.called_at, c.id), reverse=True)
    return [c.row for c in calls]


def call_user_ids(p: ProjectFacts) -> list[str]:
    return sorted({c.created_by for c in p.calls if c.created_by})


def build_detail(
    p: ProjectFacts,
    round_: str,
    now: datetime,
    *,
    entered_at: datetime | None,
    contacts_by_gc: dict[str, list[dict]],
    project_contact_ids: set[str],
    gc_outcomes: dict[str, str],
    user_id: str,
    role: Role,
    user_names: dict[str, str | None],
) -> dict:
    """GET /calling-in/projects/{id}?round= (section 6), for any round, on the
    list or not (the call log and history use it)."""
    done = done_at_by_gc(p, round_)
    gcs = []
    for s in sorted(round_sends(p, round_), key=lambda s: (s.gc_name or "").lower()):
        contacts = [
            {
                "id": c["id"],
                "name": c.get("name") or "",
                "phone": c.get("phone"),
                "email": c.get("email"),
                "is_project_contact": c["id"] in project_contact_ids,
            }
            for c in contacts_by_gc.get(s.gc_id, [])
        ]
        # Project contacts first (the people this bid went to), then by name.
        contacts.sort(key=lambda c: (not c["is_project_contact"], c["name"].lower()))
        gc_result = gc_outcomes.get(s.gc_id)
        gcs.append(
            {
                "gc_id": s.gc_id,
                "gc_name": s.gc_name,
                "proposal_send_id": s.id,
                "sent_at": iso(s.sent_at),
                "sent_via": s.sent_via if s.sent_via in ("email", "external") else "email",
                "amounts": send_amounts(s.row),
                "gc_outcome": gc_result if gc_result in ("won", "lost") else None,
                "done": s.gc_id in done,
                "done_at": iso(done.get(s.gc_id)),
                "window_open": gc_window_open(p, round_, s.gc_id, now),
                "contacts": contacts,
                "calls": [
                    serialize_call(r, user_id=user_id, role=role, user_names=user_names)
                    for r in _calls_newest_first(p, round_, s.gc_id)
                ],
            }
        )
    return {"entry": build_entry(p, round_, now, entered_at), "gcs": gcs}


def default_round(p: ProjectFacts, now: datetime) -> str:
    """The round a detail view opens on when none is asked for: the list the
    project is on, else the round its bid time points at."""
    current = on_list(p, now)
    if current:
        return current
    bid_at, _ = p.bid_time
    return POST_BID if bid_at is not None and now >= bid_at else PRE_BID


def build_call_log(
    p: ProjectFacts,
    now: datetime,
    *,
    user_id: str,
    role: Role,
    user_names: dict[str, str | None],
    extra_gc_names: dict[str, str] | None = None,
    show_bid_at: bool,
) -> dict:
    """GET /projects/{id}/call-log (section 6): both rounds, every GC the round
    covers plus any GC that has calls in it (history survives a date change).
    post_bid lists covered GCs only once the project has a bid date. bid_at
    follows the normal actual-bid redaction (`show_bid_at`)."""
    bid_at, date_only = p.bid_time
    names = {s.gc_id: s.gc_name for s in p.sends}
    for gid, name in (extra_gc_names or {}).items():
        names.setdefault(gid, name)
    rounds: dict[str, dict] = {}
    for round_ in ROUNDS:
        covered = (
            [s.gc_id for s in round_sends(p, round_)]
            if round_ == PRE_BID or bid_at is not None
            else []
        )
        called = [c.gc_id for c in p.calls if c.round == round_]
        gc_ids = list(dict.fromkeys(covered + called))
        done = done_at_by_gc(p, round_)
        gcs = [
            {
                "gc_id": gid,
                "gc_name": names.get(gid) or "",
                "done": gid in done,
                "calls": [
                    serialize_call(r, user_id=user_id, role=role, user_names=user_names)
                    for r in _calls_newest_first(p, round_, gid)
                ],
            }
            for gid in gc_ids
        ]
        gcs.sort(key=lambda g: g["gc_name"].lower())
        rounds[round_] = {"gcs": gcs}
    return {
        "on_list": on_list(p, now),
        "bid_at": iso(bid_at) if show_bid_at else None,
        "bid_at_date_only": date_only if show_bid_at else False,
        "rounds": rounds,
    }


# ── analytics (pure) ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Slot:
    """One (project, GC, round) the analytics count."""

    project: ProjectFacts
    gc_id: str
    gc_name: str
    round: str
    status: str  # called / missed / open
    first_spoke: CallFact | None
    hours_to_call: float | None
    attempts_before: int
    window_closed_at: datetime | None
    # When this slot's call window opened (its analytics anchor): pre_bid the
    # GC's sent_at, post_bid the effective T.
    window_opened_at: datetime | None = None


def project_slots(p: ProjectFacts, now: datetime) -> list[Slot]:
    """Every slot on a project, including ones whose window has not opened
    yet (analytics_report drops those). Excluded projects have none; a
    project with no T has no post_bid slots (that window never opens)."""
    if p.excluded:
        return []
    bid_at, _ = p.bid_time
    out: list[Slot] = []
    for round_ in ROUNDS:
        if round_ == POST_BID and bid_at is None:
            continue
        window = round_window(p, round_, now)
        calls = sorted((c for c in p.calls if c.round == round_), key=lambda c: c.called_at)
        for s in round_sends(p, round_):
            gc_calls = [c for c in calls if c.gc_id == s.gc_id]
            spoke = next((c for c in gc_calls if c.outcome == DONE_OUTCOME), None)
            hours = None
            attempts = 0
            if spoke is not None:
                base = s.sent_at if round_ == PRE_BID else bid_at
                hours = round((spoke.called_at - base).total_seconds() / 3600, 2)
                attempts = sum(
                    1 for c in gc_calls
                    if c.outcome != DONE_OUTCOME and c.called_at < spoke.called_at
                )
                status = "called"
            elif window.closes_at is not None and now >= window.closes_at:
                status = "missed"
            else:
                status = "open"
            out.append(
                Slot(
                    project=p,
                    gc_id=s.gc_id,
                    gc_name=s.gc_name,
                    round=round_,
                    status=status,
                    first_spoke=spoke,
                    hours_to_call=hours,
                    attempts_before=attempts,
                    window_closed_at=window.closes_at,
                    window_opened_at=s.sent_at if round_ == PRE_BID else bid_at,
                )
            )
    return out


def slot_counts(
    s: Slot,
    now: datetime,
    date_from: datetime,
    date_to: datetime,
    started_at: datetime | None,
) -> bool:
    """Whether analytics count this slot: its window opened inside the range
    and not after now (a bid tomorrow shows its pre_bid slots, not its
    post_bid ones), and, against the go-live marker, a missed slot counts
    only when its window closed at or after `started_at` (a called or still
    open slot always counts). `started_at` None applies no go-live cut."""
    opened = s.window_opened_at
    if opened is None or opened > now or not (date_from <= opened <= date_to):
        return False
    if s.status == "missed" and started_at is not None:
        return s.window_closed_at is not None and s.window_closed_at >= started_at
    return True


def _rate(numer: int, denom: int) -> float | None:
    return round(numer / denom, 4) if denom else None


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _round_summary(slots: list[Slot]) -> dict:
    called = [s for s in slots if s.status == "called"]
    missed = sum(1 for s in slots if s.status == "missed")
    hours = [s.hours_to_call for s in called if s.hours_to_call is not None]
    return {
        "slots": len(slots),
        "called": len(called),
        "missed": missed,
        "open": sum(1 for s in slots if s.status == "open"),
        "call_rate": _rate(len(called), len(called) + missed),
        "median_hours_to_call": _median(hours),
        "avg_hours_to_call": _mean(hours),
    }


def analytics_report(
    projects: Iterable[ProjectFacts],
    now: datetime,
    date_from: datetime,
    date_to: datetime,
    user_names: dict[str, str | None] | None = None,
    started_at: datetime | None = None,
) -> dict:
    """GET /analytics/calling-in (section 6). Each slot is anchored on when
    its window opened (slot_counts); `started_at` is the go-live marker
    (call_in_meta, which the loader always passes). Carries no bid_at field."""
    user_names = user_names or {}
    slots: list[Slot] = [
        s
        for p in projects
        for s in project_slots(p, now)
        if slot_counts(s, now, date_from, date_to, started_at)
    ]

    rounds = {r: _round_summary([s for s in slots if s.round == r]) for r in ROUNDS}

    by_gc: dict[str, dict] = {}
    for s in slots:
        agg = by_gc.setdefault(
            s.gc_id, {"gc_id": s.gc_id, "gc_name": s.gc_name, "called": 0, "missed": 0, "_h": []}
        )
        if s.status == "called":
            agg["called"] += 1
            if s.hours_to_call is not None:
                agg["_h"].append(s.hours_to_call)
        elif s.status == "missed":
            agg["missed"] += 1
    by_gc_rows = []
    for agg in sorted(by_gc.values(), key=lambda a: (a["gc_name"] or "").lower()):
        hours = agg.pop("_h")
        by_gc_rows.append({**agg, "median_hours_to_call": _median(hours)})

    calls = []
    for s in slots:
        if s.status != "called" or s.first_spoke is None:
            continue
        calls.append(
            {
                "project_id": s.project.id,
                "project_number": s.project.number,
                "project_name": s.project.name,
                "gc_id": s.gc_id,
                "gc_name": s.gc_name,
                "round": s.round,
                "called_at": iso(s.first_spoke.called_at),
                "called_by_name": user_names.get(s.first_spoke.created_by or ""),
                "contact_names": list(s.first_spoke.contact_names),
                "hours_to_call": s.hours_to_call,
                "attempts_before": s.attempts_before,
            }
        )
    calls.sort(key=lambda r: r["called_at"] or "", reverse=True)

    missed = [
        {
            "project_id": s.project.id,
            "project_number": s.project.number,
            "project_name": s.project.name,
            "gc_id": s.gc_id,
            "gc_name": s.gc_name,
            "round": s.round,
            "window_closed_at": iso(s.window_closed_at),
        }
        for s in slots
        if s.status == "missed"
    ]
    missed.sort(key=lambda r: r["window_closed_at"] or "", reverse=True)
    return {"rounds": rounds, "by_gc": by_gc_rows, "calls": calls, "missed": missed}


# ── loaders (sync Supabase SDK) ──────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _page_all(build: Callable[[int, int], object]) -> list[dict]:
    """Drain a query past PostgREST's max-rows cap. `build(lo, hi)` returns a
    fresh ORDERED builder limited to that inclusive range."""
    out: list[dict] = []
    lo = 0
    while True:
        rows = (build(lo, lo + _PAGE - 1).execute()).data or []
        out.extend(rows)
        if len(rows) < _PAGE:
            return out
        lo += _PAGE


def _chunks(ids: list[str], size: int = _CHUNK) -> Iterable[list[str]]:
    for i in range(0, len(ids), size):
        yield ids[i : i + size]


def _rows_for_projects(
    sb, table: str, cols: str, project_ids: list[str], narrow=None
) -> list[dict]:
    """Every row of `table` for these projects, chunked (URL length) and paged."""
    out: list[dict] = []
    for chunk in _chunks(sorted(set(project_ids))):
        def build(lo, hi, chunk=chunk):
            q = sb.table(table).select(cols).in_("project_id", chunk)
            if narrow is not None:
                q = narrow(q)
            return q.order("id").range(lo, hi)

        out.extend(_page_all(build))
    return out


def _contact_names(contacts) -> tuple[str, ...]:
    names = []
    for c in contacts or []:
        if isinstance(c, dict) and (c.get("name") or "").strip():
            names.append(c["name"].strip())
    return tuple(names)


def call_fact(row: dict) -> CallFact:
    return CallFact(
        id=row["id"],
        gc_id=row["gc_id"],
        round=row["round"],
        outcome=row["outcome"],
        called_at=parse_ts(row.get("called_at")) or _now(),
        created_by=row.get("created_by"),
        contact_names=_contact_names(row.get("contacts")),
        row=row,
    )


def facts_from_rows(
    project_rows: list[dict],
    send_rows: list[dict],
    call_rows: list[dict],
    outcome_rows: list[dict],
) -> dict[str, ProjectFacts]:
    """Assemble ProjectFacts from raw rows (pure; the loaders feed it)."""
    out: dict[str, ProjectFacts] = {}
    for r in project_rows:
        out[r["id"]] = ProjectFacts(
            id=r["id"],
            name=r.get("name") or "",
            number=r.get("number"),
            actual_bid_at=parse_ts(r.get("actual_bid_at")),
            abandoned=bool(r.get("abandoned_at")),
            stage=r.get("current_stage"),
            test_session=bool(r.get("test_session_id")),
        )
    for s in send_rows:
        p = out.get(s.get("project_id"))
        sent_at = parse_ts(s.get("sent_at"))
        # A sent row always carries sent_at; one without it cannot be placed
        # before or after T, so it is not eligible for either round.
        if p is None or sent_at is None or s.get("status", "sent") != "sent":
            continue
        p.sends.append(
            SendFact(
                id=s["id"],
                gc_id=s["gc_id"],
                gc_name=s.get("gc_name") or "",
                sent_at=sent_at,
                sent_via=s.get("sent_via") or "email",
                row=s,
            )
        )
    for c in call_rows:
        p = out.get(c.get("project_id"))
        if p is not None:
            p.calls.append(call_fact(c))
    for o in outcome_rows:
        p = out.get(o.get("project_id"))
        if p is not None:
            p.outcome_result = o.get("result")
            p.outcome_recorded_at = parse_ts(o.get("recorded_at"))
    return out


def load_facts(
    sb, project_ids: Iterable[str], project_rows: list[dict] | None = None
) -> dict[str, ProjectFacts]:
    """ProjectFacts for exactly these projects (no membership filtering)."""
    ids = sorted({pid for pid in project_ids if pid})
    if not ids:
        return {}
    if project_rows is None:
        project_rows = []
        for chunk in _chunks(ids):
            project_rows.extend(
                (sb.table("projects").select(PROJECT_COLS).in_("id", chunk).execute()).data or []
            )
    sends = _rows_for_projects(
        sb, "proposal_sends", SEND_COLS + ", status", ids, lambda q: q.eq("status", "sent")
    )
    calls = _rows_for_projects(sb, "call_in_calls", CALL_COLS, ids)
    outcomes = _rows_for_projects(sb, "bid_outcomes", "id, project_id, result, recorded_at", ids)
    return facts_from_rows(project_rows, sends, calls, outcomes)


def _sent_projects_query(sb):
    """Projects with at least one sent proposal that are not excluded."""
    return (
        sb.table("projects")
        .select(PROJECT_COLS + ", proposal_sends!inner(id)")
        .eq("proposal_sends.status", "sent")
        .is_("abandoned_at", "null")
        .is_("test_session_id", "null")
        .not_.in_("current_stage", list(EXCLUDED_STAGES))
    )


def _pg_ts(dt: datetime) -> str:
    # Z-suffixed UTC: no '+' (URL-decodes to a space) and no commas (they
    # delimit PostgREST or=() conditions).
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_candidates(sb, now: datetime) -> dict[str, ProjectFacts]:
    """Every project that could be on a list right now: not excluded, has a
    sent proposal, and either no actual bid date or one recent enough that
    List 2 can still be open (two days of slack cover the date-only shift)."""
    lo_ts = _pg_ts(now - timedelta(days=POST_BID_DAYS + 2))
    rows = _page_all(
        lambda lo, hi: _sent_projects_query(sb)
        .or_(f"actual_bid_at.is.null,actual_bid_at.gte.{lo_ts}")
        .order("id")
        .range(lo, hi)
    )
    return load_facts(sb, [r["id"] for r in rows], project_rows=rows)


def load_open_entries(sb, project_id: str | None = None) -> list[dict]:
    def build(lo, hi):
        q = sb.table("call_in_entries").select(ENTRY_COLS).is_("closed_at", "null")
        if project_id is not None:
            q = q.eq("project_id", project_id)
        return q.order("id").range(lo, hi)

    return _page_all(build)


def entered_map(open_rows: list[dict]) -> dict[tuple[str, str], datetime]:
    out: dict[tuple[str, str], datetime] = {}
    for r in open_rows:
        opened = parse_ts(r.get("opened_at"))
        if opened is not None:
            out[(r["project_id"], r["round"])] = opened
    return out


def current_lists(now: datetime | None = None) -> dict:
    """GET /calling-in."""
    sb = get_supabase()
    now = now or _now()
    facts = load_candidates(sb, now)
    lists = compute_lists(facts.values(), now, entered_map(load_open_entries(sb)))
    return {"now": iso(now), **lists}


def current_summary(now: datetime | None = None) -> dict:
    """GET /calling-in/summary."""
    now = now or _now()
    return summary_counts(load_candidates(get_supabase(), now).values(), now)


def load_started_at(sb, now: datetime) -> datetime:
    """The go-live marker: call_in_meta.started_at (the single row 0142
    inserts; its insert time is when Calling In went live). A missing row, or
    a database without the table, reads as `now`, so nothing historic counts
    as missed rather than the analytics erroring."""
    try:
        rows = (sb.table("call_in_meta").select("started_at").limit(1).execute()).data or []
    except APIError:
        logger.warning("calling in: call_in_meta unreadable; treating go-live as now")
        return now
    started = parse_ts(rows[0].get("started_at")) if rows else None
    return started or now


def load_analytics(date_from: datetime, date_to: datetime, now: datetime | None = None) -> dict:
    """GET /analytics/calling-in: every project with a slot whose window
    opened in the range (pre_bid: a GC's sent_at; post_bid: T)."""
    sb = get_supabase()
    now = now or _now()
    # One day of slack either side covers the date-only end-of-day shift;
    # the exact window-open test runs in analytics_report.
    lo_ts = _pg_ts(date_from - timedelta(days=1))
    hi_ts = _pg_ts(date_to + timedelta(days=1))
    # post_bid windows open at T.
    rows = _page_all(
        lambda lo, hi: _sent_projects_query(sb)
        .gte("actual_bid_at", lo_ts)
        .lte("actual_bid_at", hi_ts)
        .order("id")
        .range(lo, hi)
    )
    # pre_bid windows open at each GC's sent_at, whenever (or whether) T is.
    sent_in_range = _page_all(
        lambda lo, hi: sb.table("proposal_sends")
        .select("id, project_id")
        .eq("status", "sent")
        .gte("sent_at", lo_ts)
        .lte("sent_at", hi_ts)
        .order("id")
        .range(lo, hi)
    )
    have = {r["id"] for r in rows}
    more = sorted({s["project_id"] for s in sent_in_range if s.get("project_id")} - have)
    for chunk in _chunks(more):
        rows.extend((_sent_projects_query(sb).in_("id", chunk).execute()).data or [])
    facts = load_facts(sb, [r["id"] for r in rows], project_rows=rows)
    apply_live_gc_names(sb, facts.values())
    user_ids = sorted({c.created_by for p in facts.values() for c in p.calls if c.created_by})
    return analytics_report(
        facts.values(),
        now,
        date_from,
        date_to,
        profile_names(sb, user_ids),
        started_at=load_started_at(sb, now),
    )


def gc_names(sb, gc_ids: Iterable[str]) -> dict[str, str]:
    ids = sorted({g for g in gc_ids if g})
    out: dict[str, str] = {}
    for chunk in _chunks(ids):
        rows = (
            sb.table("general_contractors").select("id, name").in_("id", chunk).execute()
        ).data or []
        out.update({r["id"]: r.get("name") or "" for r in rows})
    return out


def apply_live_gc_names(sb, projects: Iterable[ProjectFacts]) -> None:
    """Swap each send's gc_name snapshot for the GC's current name."""
    projects = list(projects)
    names = gc_names(sb, (s.gc_id for p in projects for s in p.sends))
    for p in projects:
        p.sends = [
            SendFact(
                id=s.id, gc_id=s.gc_id, gc_name=names.get(s.gc_id) or s.gc_name,
                sent_at=s.sent_at, sent_via=s.sent_via, row=s.row,
            )
            for s in p.sends
        ]


def profile_names(sb, user_ids: Iterable[str]) -> dict[str, str | None]:
    ids = sorted({u for u in user_ids if u})
    out: dict[str, str | None] = {}
    for chunk in _chunks(ids):
        rows = (
            sb.table("profiles").select("id, full_name").in_("id", chunk).execute()
        ).data or []
        out.update({r["id"]: r.get("full_name") for r in rows})
    return out


# ── entries: claim, notify, close ────────────────────────────────────────────


def _is_unique_violation(exc: Exception) -> bool:
    return "23505" in str(getattr(exc, "code", "") or "") or "23505" in str(exc)


def claim_entry(sb, project_id: str, round_: str, now: datetime) -> dict | None:
    """Insert the open entry. None when another worker already holds it (the
    partial unique index call_in_entries_one_open refused the insert)."""
    try:
        res = (
            sb.table("call_in_entries")
            .insert({"project_id": project_id, "round": round_, "opened_at": iso(now)})
            .execute()
        )
    except APIError as exc:
        if _is_unique_violation(exc):
            return None
        raise
    rows = res.data or []
    return rows[0] if rows else None


def _project_label(p: ProjectFacts) -> str:
    label = p.name or "A project"
    if p.number:
        label = f"{label} (#{p.number})"
    return label


def notification_message(p: ProjectFacts, round_: str) -> str:
    """The notice text. Counts the GCs still to call for this round (a late
    GC re-opening a list names only what is left, not every GC we sent to)."""
    done = done_at_by_gc(p, round_)
    n = sum(1 for s in round_sends(p, round_) if s.gc_id not in done)
    gcs = f"{n} GC{'s' if n != 1 else ''}"
    label = _project_label(p)
    if round_ == PRE_BID:
        bid_at, _ = p.bid_time
        tail = " It has no actual bid date yet." if bid_at is None else ""
        return (
            f"{label} is on the Calling In Before bid list: {gcs} still to call "
            f"about our proposal before the bid.{tail}"
        )
    return (
        f"The bid time has passed for {label}. It is on the Calling In After bid "
        f"list: {gcs} still to call, within 7 days of the bid."
    )


def notify_entry(sb, p: ProjectFacts, round_: str, entry: dict, now: datetime) -> bool:
    """Bell + email mirror to Executive and Estimating Engineer Labor, then
    stamp notified_at. Best-effort per role."""
    type_ = NOTIFY_TYPES[round_]
    message = notification_message(p, round_)
    metadata = {"round": round_, "entry_id": entry["id"]}
    sent = False
    for role in NOTIFY_ROLES:
        try:
            notify_role(role, p.id, type_, message, metadata=metadata)
            sent = True
        except Exception:  # noqa: BLE001 - one role's failure never stops the other
            logger.exception("calling in: notify failed (role=%s project=%s)", role, p.id)
    if sent:
        sb.table("call_in_entries").update({"notified_at": iso(now)}).eq(
            "id", entry["id"]
        ).execute()
    return sent


def close_entry(sb, entry: dict, reason: str, now: datetime) -> None:
    """Close an open entry and dismiss the notifications it raised."""
    sb.table("call_in_entries").update(
        {"closed_at": iso(now), "close_reason": reason}
    ).eq("id", entry["id"]).is_("closed_at", "null").execute()
    try:
        dismiss_notifications(
            project_id=entry["project_id"],
            types=[NOTIFY_TYPES[entry["round"]]],
            metadata_eq={"entry_id": entry["id"]},
        )
    except Exception:  # noqa: BLE001 - the close stands; the bell row is cosmetic
        logger.exception("calling in: dismiss failed (entry=%s)", entry.get("id"))


def sync_project_entries(project_id: str, now: datetime | None = None) -> list[str]:
    """Close this project's open entries that are no longer on their list
    (called inside the request that logged, edited or deleted a call, so the
    badge and the bell update at once; the poller is the backstop). Never
    opens an entry: claiming and notifying belong to the poller alone.
    Returns the rounds it closed."""
    sb = get_supabase()
    now = now or _now()
    open_rows = load_open_entries(sb, project_id)
    if not open_rows:
        return []
    p = load_facts(sb, [project_id]).get(project_id)
    closed = []
    for row in open_rows:
        if p is not None and is_member(p, row["round"], now):
            continue
        close_entry(sb, row, close_reason(p, row["round"], now), now)
        closed.append(row["round"])
    return closed


def load_entry_history(sb, project_ids: Iterable[str]) -> dict[str, list[dict]]:
    """Every call_in_entries row (open or closed) for these projects."""
    out: dict[str, list[dict]] = {}
    for r in _rows_for_projects(sb, "call_in_entries", ENTRY_COLS, list(project_ids)):
        out.setdefault(r["project_id"], []).append(r)
    return out


def poll_once(now: datetime | None = None) -> dict[str, int]:
    """One poller tick (section 5): close stale entries, then claim + notify
    new ones."""
    sb = get_supabase()
    now = now or _now()
    # Open entries BEFORE the facts: a request that closes an entry between
    # the two reads then shows up in the facts too (no stale re-claim of a
    # list the request just cleared); an entry another worker opens in
    # between just makes our claim lose on the unique index.
    open_rows = load_open_entries(sb)
    facts = load_candidates(sb, now)
    missing = {r["project_id"] for r in open_rows} - set(facts)
    if missing:
        facts.update(load_facts(sb, missing))
    open_by_key = {(r["project_id"], r["round"]): r for r in open_rows}
    stats = {"claimed": 0, "notified": 0, "closed": 0}

    # 1. Close FIRST, so a List 1 to List 2 move (or back) in this same tick
    #    sees the fresh closed_at as the new entry's trigger.
    for (project_id, round_), row in open_by_key.items():
        p = facts.get(project_id)
        if p is not None and is_member(p, round_, now):
            continue
        close_entry(sb, row, close_reason(p, round_, now), now)
        stats["closed"] += 1

    # 2. Claim. An entry that was open is either still a member (keep it) or
    #    was just closed as a non-member, so it never re-claims here.
    due = [
        (p, round_)
        for p in facts.values()
        for round_ in ROUNDS
        if (p.id, round_) not in open_by_key and is_member(p, round_, now)
    ]
    if not due:
        return stats
    # Read after the closes (they are committed, and another worker's closes
    # too) and before the claims, so no history row is the entry we claim.
    history = load_entry_history(sb, {p.id for p, _ in due})
    for p, round_ in due:
        rows = history.get(p.id, [])
        # Never stamp the new entry before the other round's close (another
        # worker may have closed it a few milliseconds after this tick began),
        # so opened_at >= its trigger and a later correction stays silent.
        moved = other_round_closed_at(rows, round_)
        at = max(now, moved) if moved is not None else now
        entry = claim_entry(sb, p.id, round_, at)
        if entry is None:
            continue  # another worker won the claim; it notifies
        stats["claimed"] += 1
        if should_notify(p, round_, at, rows) and notify_entry(sb, p, round_, entry, at):
            stats["notified"] += 1
    return stats


async def polling_loop() -> None:
    interval = get_settings().call_in_poll_interval_seconds
    while True:
        try:
            stats = await asyncio.to_thread(poll_once)
            if any(stats.values()):
                logger.info("calling in poll: %s", stats)
        except Exception:  # noqa: BLE001 - the loop must survive any tick failure
            logger.exception("Calling In poll failed")
        await asyncio.sleep(interval)

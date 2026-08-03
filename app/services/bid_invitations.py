"""Bid Invitations report — the app's version of the front office's bid-log
spreadsheet (assets/Bid Invitations Template.xlsx): one row per bid, ordered by
bid date, with the per-GC needs-by dates, estimator/quote progress, send times
and the win/loss tally across the top.

Unlike the rolling windows in analytics_metrics, this report's ranges are
CALENDAR-anchored (week-to-date, last month, …) because it mirrors how the
sheet is read in scheduling meetings. The *-to-date ranges are deliberately
open-ended into the future: "week to date" means "this week and everything
coming up", so upcoming bids — however far out — stay visible for
prioritisation. Backward-looking ranges (yesterday, last week/month/year,
past 5 years, custom) are closed on both sides.

A project is in range when its bid deadline lands in the window. The actual
bid date is confidential: for ACTUAL_BID_VIEWER_ROLES the anchor is
actual_bid_at falling back to internal_bid_at, and rows carry bid_at. For
every other role the rows omit the bid_at field entirely (the columns
disappear client-side too) AND the anchor is internal_bid_at alone — if the
confidential date still drove membership/ordering, a one-day custom window
would recover it to the day. A project with only an actual date therefore
drops out of the non-privileged view, matching how redact_for_role presents
it (no bid date at all) everywhere else.
"""

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.roles import ACTUAL_BID_VIEWER_ROLES
from app.core.supabase_client import get_supabase
from app.services.analytics_metrics import WAGE_LABELS, _iso, _on_time, _parse
from app.services.project_status import derive_status

# Calendar ranges are anchored to the office's local calendar (Las Vegas), not
# UTC — "today" must mean the office's today.
REPORT_TZ = ZoneInfo("America/Los_Angeles")

RANGE_NAMES = (
    "today", "wtd", "mtd", "ytd",
    "yesterday", "last_week", "last_month", "last_year", "past_5_years",
    "custom",
)

_PROJECT_COLS = (
    "id, name, number, current_stage, abandoned_at, "
    "internal_bid_at, actual_bid_at, invitation_at, "
    "est_start_date, est_finish_date, wage_type, labor_time, labor_note"
)


def _day_start(d: date) -> datetime:
    """Local midnight of a calendar day, as aware UTC."""
    return datetime.combine(d, time.min, tzinfo=REPORT_TZ).astimezone(timezone.utc)


def _years_back(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:  # Feb 29 → Feb 28
        return d.replace(year=d.year - years, day=28)


def resolve_range(
    range_: str, date_from: str | None, date_to: str | None, now: datetime | None = None
) -> tuple[datetime, datetime | None]:
    """Resolve a named/custom range to an aware UTC (from, to) pair. `to` is
    None for the open-ended *-to-date ranges (no upper bound), otherwise an
    exclusive end. Custom bounds are calendar dates in the report timezone,
    end-inclusive of its whole day; future custom windows are allowed."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(REPORT_TZ).date()

    if range_ == "custom":
        if not date_from or not date_to:
            raise ValueError("custom range requires date_from and date_to")
        try:
            a, b = date.fromisoformat(date_from[:10]), date.fromisoformat(date_to[:10])
        except ValueError as exc:
            raise ValueError("invalid custom range") from exc
        if a > b:
            raise ValueError("invalid custom range")
        try:
            return _day_start(a), _day_start(b + timedelta(days=1))
        except OverflowError as exc:  # date_to=9999-12-31 must 400, not 500
            raise ValueError("invalid custom range") from exc
    if range_ == "today":
        return _day_start(today), None
    if range_ == "wtd":  # weeks start Monday, like the sheet's Mon–Fri legend
        return _day_start(today - timedelta(days=today.weekday())), None
    if range_ == "mtd":
        return _day_start(today.replace(day=1)), None
    if range_ == "ytd":
        return _day_start(today.replace(month=1, day=1)), None
    if range_ == "yesterday":
        return _day_start(today - timedelta(days=1)), _day_start(today)
    if range_ == "last_week":
        monday = today - timedelta(days=today.weekday())
        return _day_start(monday - timedelta(days=7)), _day_start(monday)
    if range_ == "last_month":
        first = today.replace(day=1)
        prev_first = (first - timedelta(days=1)).replace(day=1)
        return _day_start(prev_first), _day_start(first)
    if range_ == "last_year":
        return _day_start(date(today.year - 1, 1, 1)), _day_start(date(today.year, 1, 1))
    if range_ == "past_5_years":
        return _day_start(_years_back(today, 5)), _day_start(today + timedelta(days=1))
    raise ValueError(f"unknown range '{range_}'")


def _date_deadline(d: str | None) -> datetime | None:
    """A per-GC needs-by DATE is met any time that local day — deadline is the
    following local midnight."""
    if not d:
        return None
    try:
        return _day_start(date.fromisoformat(d[:10]) + timedelta(days=1))
    except OverflowError:
        # Writes are bounded (schemas._check_needs_by) but a pre-existing
        # extreme row must degrade to "no per-GC deadline", not 500 the report.
        return None


# One in.() per 200 ids keeps the PostgREST URL comfortably under gateway
# length limits and each response under PostgREST's default row cap, however
# large the requested window gets.
_IN_CHUNK = 200


def _in(make_query, column: str, ids: list[str]):
    rows: list[dict] = []
    for i in range(0, len(ids), _IN_CHUNK):
        chunk = ids[i : i + _IN_CHUNK]
        rows.extend(make_query().in_(column, chunk).execute().data or [])
    return rows


def report(
    date_from: datetime, date_to: datetime | None, role: str, range_: str
) -> dict:
    sb = get_supabase()
    bid_dates_visible = role in ACTUAL_BID_VIEWER_ROLES

    projects = (
        sb.table("projects")
        .select(_PROJECT_COLS)
        .neq("current_stage", "pm_only")
        .neq("current_stage", "cp_only")
        .execute()
    ).data or []

    anchored: list[tuple[datetime, dict]] = []
    for p in projects:
        if bid_dates_visible:
            anchor = _parse(p.get("actual_bid_at")) or _parse(p.get("internal_bid_at"))
        else:
            # Membership/ordering must be a function of dates this role can
            # see: anchoring on the confidential actual_bid_at would let a
            # one-day custom window recover it to the day (see module docstring).
            anchor = _parse(p.get("internal_bid_at"))
        if anchor is None or anchor < date_from:
            continue
        if date_to is not None and anchor >= date_to:
            continue
        anchored.append((anchor, p))
    anchored.sort(key=lambda t: (t[0], (t[1].get("name") or "").lower()))
    pids = [p["id"] for _, p in anchored]

    memberships: dict[str, list[dict]] = defaultdict(list)
    sent: dict[str, dict[str, dict]] = defaultdict(dict)  # pid -> gc_id -> send
    returned_at: dict[str, datetime] = {}
    outcomes: dict[str, str] = {}
    requested: dict[str, int] = defaultdict(int)
    received: dict[str, int] = defaultdict(int)

    if pids:
        for r in _in(
            lambda: sb.table("project_gcs").select(
                "project_id, needs_by, general_contractors(id, name)"
            ),
            "project_id",
            pids,
        ):
            gc = r.get("general_contractors")
            if gc:
                memberships[r["project_id"]].append(
                    {"gc_id": gc["id"], "name": gc["name"], "needs_by": r.get("needs_by")}
                )
        for s in _in(
            lambda: sb.table("proposal_sends")
            .select("project_id, gc_id, gc_name, sent_at")
            .eq("status", "sent"),
            "project_id",
            pids,
        ):
            sent[s["project_id"]][s["gc_id"]] = s
        for a in _in(
            lambda: sb.table("estimator_assignments").select("project_id, returned_at"),
            "project_id",
            pids,
        ):
            at = _parse(a.get("returned_at"))
            # First submission wins — matches how returned_at itself is stamped.
            if at and (a["project_id"] not in returned_at or at < returned_at[a["project_id"]]):
                returned_at[a["project_id"]] = at
        for o in _in(
            lambda: sb.table("bid_outcomes").select("project_id, result"),
            "project_id",
            pids,
        ):
            outcomes[o["project_id"]] = o["result"]

        rfq_pid = {
            r["id"]: r["project_id"]
            for r in _in(
                lambda: sb.table("rfqs").select("id, project_id"), "project_id", pids
            )
        }
        if rfq_pid:
            rfq_ids = list(rfq_pid.keys())
            quoted_send_ids: set[str] = set()
            unlinked: dict[str, int] = defaultdict(int)
            # Manual quote entry doesn't stamp rfq_sends.quote_received_at, so a
            # send also counts as answered when a quotes row points at it; quotes
            # not tied to any send (fully manual) still count toward "received".
            for q in _in(
                lambda: sb.table("quotes").select("rfq_id, rfq_send_id"),
                "rfq_id",
                rfq_ids,
            ):
                if q.get("rfq_send_id"):
                    quoted_send_ids.add(q["rfq_send_id"])
                else:
                    unlinked[rfq_pid[q["rfq_id"]]] += 1
            for s in _in(
                lambda: sb.table("rfq_sends")
                .select("id, rfq_id, status, quote_received_at")
                .eq("status", "sent"),
                "rfq_id",
                rfq_ids,
            ):
                pid = rfq_pid[s["rfq_id"]]
                requested[pid] += 1
                if s.get("quote_received_at") or s["id"] in quoted_send_ids:
                    received[pid] += 1
            for pid, n in unlinked.items():
                received[pid] += n

    rows = []
    summary = {"bids": len(anchored), "won": 0, "lost": 0, "no_award": 0,
               "no_bid": 0, "waiting": 0, "active": 0}
    for anchor, p in anchored:
        pid = p["id"]
        status = derive_status(
            p.get("current_stage"), p.get("abandoned_at"), outcomes.get(pid)
        )
        if status in ("won", "lost", "no_award"):
            summary[status] += 1
        elif status in ("declined", "abandoned"):
            summary["no_bid"] += 1
        elif status == "sent":
            summary["waiting"] += 1
        else:
            summary["active"] += 1

        sends = sent.get(pid, {})
        gcs = []
        for m in sorted(memberships.get(pid, []), key=lambda g: g["name"].lower()):
            s = sends.pop(m["gc_id"], None)
            sent_at = _parse(s.get("sent_at")) if s else None
            gcs.append({
                "gc_id": m["gc_id"],
                "name": m["name"],
                "needs_by": m["needs_by"],
                "sent_at": _iso(sent_at),
                # Judge each send against that GC's own deadline when one is
                # set; otherwise against the project bid deadline.
                "on_time": _on_time(sent_at, _date_deadline(m["needs_by"]) or anchor),
            })
        # GCs dropped from the project after we bid them: the send row is the
        # history — keep them on the report.
        for s in sends.values():
            sent_at = _parse(s.get("sent_at"))
            gcs.append({
                "gc_id": s["gc_id"], "name": s["gc_name"], "needs_by": None,
                "sent_at": _iso(sent_at), "on_time": _on_time(sent_at, anchor),
            })

        sent_ats = [g["sent_at"] for g in gcs if g["sent_at"]]
        verdicts = [g["on_time"] for g in gcs if g["on_time"]]
        row = {
            "project_id": pid,
            "number": p.get("number"),
            "name": p.get("name"),
            "status": status,
            "invitation_at": _iso(_parse(p.get("invitation_at"))),
            "est_start_date": p.get("est_start_date"),
            "est_finish_date": p.get("est_finish_date"),
            "wage_type": p.get("wage_type"),
            "wage_label": WAGE_LABELS.get(p.get("wage_type"), p.get("wage_type")),
            "labor_note": p.get("labor_note"),
            "gcs": gcs,
            "estimator_returned_at": _iso(returned_at.get(pid)),
            "quotes_requested": requested.get(pid, 0),
            "quotes_received": received.get(pid, 0),
            "sent_at": min(sent_ats) if sent_ats else None,
            "on_time": "late" if "late" in verdicts else ("on_time" if verdicts else None),
        }
        if bid_dates_visible:
            row["bid_at"] = _iso(anchor)
            row["bid_at_is_internal"] = p.get("actual_bid_at") is None
        rows.append(row)

    return {
        "meta": {
            "range": range_,
            "date_from": _iso(date_from),
            "date_to": _iso(date_to),
            "open_ended": date_to is None,
            "timezone": str(REPORT_TZ),
            "bid_dates_visible": bid_dates_visible,
        },
        "summary": summary,
        "rows": rows,
    }

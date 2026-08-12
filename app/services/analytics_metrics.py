"""Analytics computation — all the metric math behind the /analytics endpoints.

Kept separate from routers/analytics.py (which stays a thin HTTP layer) the same
way pricing.py keeps its pure helpers out of its handlers. Everything here works
on bulk PostgREST selects aggregated in Python — project volume is small (dozens
to low hundreds), so per-row Python is simpler and fast enough, and matches the
existing summary endpoint's approach.

Time windows are ROLLING (last N days from now), which sidesteps calendar/timezone
boundary issues. A project enters a window via the date most relevant to the
section's question (its "anchor" — echoed back in each response so the UI can
label the range correctly):

  overview / send_out / cycle_time / win_loss → submitted_at (the bid date)
  estimator                                   → returned_at (fallback sent_to_estimator_at)
  quotes                                      → latest quote received_at

On-time/late is always derived at query time by comparing an actual against a
deadline; it is never stored.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.roles import ACTUAL_BID_VIEWER_ROLES, Role
from app.routers.pricing import pick_material_amount, pricing_summary_numbers, taxed_amount
from app.core.supabase_client import get_supabase
from app.services.outcome import our_amount_of
from app.services.project_status import derive_status
from app.services.workflow import STAGES

# Rolling-window lengths for the named ranges.
RANGE_DAYS = {"day": 1, "week": 7, "month": 30, "quarter": 91, "year": 365}

WAGE_LABELS = {"prevailing_wage": "Prevailing Wage", "non_prevailing_wage": "Non-Prevailing Wage"}
LABOR_TIME_LABELS = {"day_work": "Day Work", "night_work": "Night Work"}
VALUE_BAND_LABELS = {
    "under_50k": "Under $50k",
    "50k_150k": "$50k–150k",
    "150k_500k": "$150k–500k",
    "500k_1m": "$500k–1M",
    "1m_3m": "$1M–3M",
    "over_3m": "Over $3M",
}
GROUP_LABELS = {
    "wage_type": WAGE_LABELS,
    "labor_time": LABOR_TIME_LABELS,
    "est_value_band": VALUE_BAND_LABELS,
}


def _group_label(labels: dict[str, str], key: str) -> str:
    """Display label for a breakdown key. Presets use their mapped label; a custom
    labor_time / wage_type value shows as typed; the missing-value sentinel is
    'Unknown'."""
    if key == "unknown":
        return "Unknown"
    return labels.get(key, key)

_PROJECT_COLS = (
    "id, name, number, current_stage, abandoned_at, created_at, "
    "internal_bid_at, actual_bid_at, due_from_estimator_at, due_from_vendors_at, "
    "wage_type, labor_time, est_value_band"
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str | None) -> datetime | None:
    """Parse an ISO timestamp/date, always returning an aware UTC datetime. DATE
    columns (rfqs.due_date, custom range bounds) parse as naive — coerce them so
    they can be compared against timestamptz values without raising."""
    if not ts:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _eod(ts: str | None) -> datetime | None:
    """A date-only deadline (e.g. rfqs.due_date) is met any time that day, so treat
    it as the end of the day (next midnight) when classifying on-time/late."""
    dt = _parse(ts)
    if dt is None:
        return None
    return dt + timedelta(days=1) if (dt.hour, dt.minute, dt.second) == (0, 0, 0) else dt


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _hours(later: datetime | None, earlier: datetime | None) -> float | None:
    if later is None or earlier is None:
        return None
    return round((later - earlier).total_seconds() / 3600, 2)


def _on_time(actual: datetime | None, due: datetime | None) -> str | None:
    """on_time / late vs a deadline, or None when either side is missing."""
    if actual is None or due is None:
        return None
    return "on_time" if actual <= due else "late"


def _rate(numer: int, denom: int) -> float | None:
    return round(numer / denom, 4) if denom else None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _mean_signed(values: list[float]) -> float | None:
    """Mean that keeps its sign, at fraction precision (like _rate) — for signed
    percentages such as how much higher/lower we bid a GC vs. the other GCs."""
    return round(sum(values) / len(values), 4) if values else None


def resolve_range(
    range_: str, date_from: str | None, date_to: str | None
) -> tuple[datetime, datetime]:
    """Resolve a named or custom range into an aware (from, to) UTC pair."""
    if range_ == "custom":
        if not date_from or not date_to:
            raise ValueError("custom range requires date_from and date_to")
        a, b = _parse(date_from), _eod(date_to)  # to-date inclusive of its whole day
        if a is None or b is None or a > b:
            raise ValueError("invalid custom range")
        return a, b
    days = RANGE_DAYS.get(range_)
    if days is None:
        raise ValueError(f"unknown range '{range_}'")
    now = utcnow()
    return now - timedelta(days=days), now


# ── Trend bucketing ────────────────────────────────────────────────────────


def _granularity(date_from: datetime, date_to: datetime) -> str:
    days = (date_to - date_from).days
    if days <= 2:
        return "hour"
    if days <= 31:
        return "day"
    if days <= 120:
        return "week"
    return "month"


def _bucket_start(dt: datetime, gran: str) -> datetime:
    if gran == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    base = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if gran == "day":
        return base
    if gran == "week":
        return base - timedelta(days=base.weekday())
    return base.replace(day=1)


def _advance(dt: datetime, gran: str) -> datetime:
    if gran == "hour":
        return dt + timedelta(hours=1)
    if gran == "day":
        return dt + timedelta(days=1)
    if gran == "week":
        return dt + timedelta(weeks=1)
    return (dt.replace(day=1) + timedelta(days=32)).replace(day=1)


def _bucket_label(dt: datetime, gran: str) -> str:
    if gran == "hour":
        h = dt.hour % 12 or 12
        return f"{h}{'am' if dt.hour < 12 else 'pm'}"
    if gran == "month":
        return f"{dt:%b} {dt.year}"
    return f"{dt:%b} {dt.day}"


def _build_trend(
    pairs: list[tuple[datetime, float]], date_from: datetime, date_to: datetime
) -> list[dict]:
    """Bucket (timestamp, value) pairs across the window, filling empty buckets so
    the chart has a continuous x-axis."""
    gran = _granularity(date_from, date_to)
    sums: dict[datetime, float] = defaultdict(float)
    for ts, val in pairs:
        sums[_bucket_start(ts, gran)] += val
    out: list[dict] = []
    cur = _bucket_start(date_from, gran)
    guard = 0
    while cur < date_to and guard < 2000:
        v = sums.get(cur, 0.0)
        out.append(
            {
                "label": _bucket_label(cur, gran),
                "bucket_start": cur.isoformat(),
                "value": round(v, 2),
            }
        )
        cur = _advance(cur, gran)
        guard += 1
    return out


# ── Window loader ──────────────────────────────────────────────────────────


@dataclass
class WindowData:
    date_from: datetime
    date_to: datetime
    project_id: str | None
    projects: dict[str, dict]
    submitted_at: dict[str, datetime] = field(default_factory=dict)
    first_event_at: dict[str, datetime] = field(default_factory=dict)
    events_by_project: dict[str, list[dict]] = field(default_factory=dict)

    @property
    def is_project_mode(self) -> bool:
        return self.project_id is not None

    def in_window(self, dt: datetime | None) -> bool:
        """Whether an anchor date falls in the window. Per-project mode ignores the
        window entirely (the single project is always in scope)."""
        if self.is_project_mode:
            return True
        if dt is None:
            return False
        return self.date_from <= dt < self.date_to

    def ref(self, pid: str) -> dict:
        p = self.projects.get(pid, {})
        return {
            "project_id": pid,
            "number": p.get("number"),
            "name": p.get("name"),
            "current_stage": p.get("current_stage"),
        }


def load_window(
    date_from: datetime,
    date_to: datetime,
    project_id: str | None = None,
    status: str | None = None,
) -> WindowData:
    sb = get_supabase()
    # Direct-created PM projects ('pm_only') and legacy Certified Payroll imports
    # ('cp_only') never entered the bidding pipeline — exclude them here, the
    # single choke point every windowed section's cohort is built from, so they
    # can't pollute any bidding metric.
    pq = (
        sb.table("projects")
        .select(_PROJECT_COLS)
        .neq("current_stage", "pm_only")
        .neq("current_stage", "cp_only")
    )
    if project_id:
        pq = pq.eq("id", project_id)
    projects = {p["id"]: p for p in (pq.execute().data or [])}

    # Lifecycle-status filter: prune the project set to those whose derived status
    # matches. Skipped in per-project (drill-down) mode — the selected project is
    # always shown. Because every section's cohort is built from `w.projects` /
    # `submitted_at`, pruning here applies the filter across all sections at once.
    if status and not project_id and projects:
        results = {
            o["project_id"]: o["result"]
            for o in (
                sb.table("bid_outcomes")
                .select("project_id, result")
                .in_("project_id", list(projects.keys()))
                .execute()
            ).data
            or []
        }
        projects = {
            pid: p
            for pid, p in projects.items()
            if derive_status(p.get("current_stage"), p.get("abandoned_at"), results.get(pid))
            == status
        }

    eq = sb.table("stage_events").select("project_id, to_stage, entered_at").order(
        "project_id"
    ).order("entered_at")
    if project_id:
        eq = eq.eq("project_id", project_id)
    events = eq.execute().data or []

    w = WindowData(date_from, date_to, project_id, projects)
    for e in events:
        pid = e["project_id"]
        # Keep the event-derived maps (submitted_at, first_event_at, …) in lockstep
        # with the (possibly status-pruned) project set.
        if pid not in projects:
            continue
        w.events_by_project.setdefault(pid, []).append(e)
        at = _parse(e["entered_at"])
        if at is None:
            continue
        if pid not in w.first_event_at:
            w.first_event_at[pid] = at
        # First-submitted-wins. This guard is also what makes re-verification safe:
        # a post-verify pricing edit can bounce a `submitted` project back to verify
        # and re-commit it back to `submitted`, writing a second `to_stage=submitted`
        # event — but the bid date / end-to-end time stays pinned to the ORIGINAL
        # submission, not the re-commit (see workflow.reopen_verify / return_from_reverify).
        if e["to_stage"] == "submitted" and pid not in w.submitted_at:
            w.submitted_at[pid] = at
    return w


def meta(w: WindowData, range_: str, anchor: str) -> dict:
    return {
        "range": range_,
        "date_from": _iso(w.date_from),
        "date_to": _iso(w.date_to),
        "project_id": w.project_id,
        "anchor": anchor,
    }


# ── Pricing (batched bid prices for a cohort) ──────────────────────────────


def _materials_amounts(
    rfqs_by_proj: dict[str, list[dict]],
    quotes_by_rfq: dict[str, list[dict]],
    gen_by_proj: dict[str, dict],
    pid: str,
) -> list[Decimal | None]:
    """Per-category price basis for a project (mirrors pricing._materials_rows):
    the SELECTED quote per RFQ and nothing else — no lowest-received fallback, no
    hand-entered override outranking it, and General Material priced by its own
    selected candidate like every other category. Returns one amount per category
    (None where no winner has been picked).

    The estimate figure is still used for the one edge pricing keeps: a project that
    has a general estimate but never had a General Material RFQ to attach it to."""
    amounts: list[Decimal | None] = []
    gen = gen_by_proj.get(pid)
    # The general figure carries its own tax answer — taxed like a quote.
    gen_amount = taxed_amount(gen) if gen and gen.get("amount") is not None else None
    saw_general = False
    for r in rfqs_by_proj.get(pid, []):
        cat = r.get("material_categories") or {}
        saw_general = saw_general or bool(cat.get("is_general"))
        selected = next(
            (q for q in quotes_by_rfq.get(r["id"], []) if q.get("is_selected")), None
        )
        amt, _ = pick_material_amount(selected)
        amounts.append(amt)
    if not saw_general and gen_amount is not None:
        amounts.append(gen_amount)
    return amounts


def _load_pricing(pids: list[str]) -> dict[str, dict]:
    """Batched pricing-summary numbers (incl. bid_price) for many projects."""
    if not pids:
        return {}
    sb = get_supabase()
    rfqs = (
        sb.table("rfqs")
        .select("id, project_id, material_categories(is_general)")
        .in_("project_id", pids)
        .execute()
    ).data or []
    rfqs_by_proj: dict[str, list[dict]] = defaultdict(list)
    rfq_ids: list[str] = []
    for r in rfqs:
        rfqs_by_proj[r["project_id"]].append(r)
        rfq_ids.append(r["id"])
    quotes = (
        (
            sb.table("quotes")
            .select("rfq_id, amount, is_selected, tax_included, tax_rate, origin")
            .in_("rfq_id", rfq_ids)
            .execute()
        ).data
        if rfq_ids
        else []
    ) or []
    quotes_by_rfq: dict[str, list[dict]] = defaultdict(list)
    for q in quotes:
        quotes_by_rfq[q["rfq_id"]].append(q)
    gen_by_proj = {
        g["project_id"]: g
        for g in (
            sb.table("general_material_estimates")
            .select("project_id, amount, source, tax_included, tax_rate")
            .in_("project_id", pids)
            .execute()
        ).data
        or []
    }
    labor_by_proj = {
        x["project_id"]: x
        for x in (
            sb.table("labor_reviews").select("project_id, labor_amount").in_("project_id", pids).execute()
        ).data
        or []
    }
    markup_by_proj = {
        x["project_id"]: x
        for x in (
            sb.table("markups")
            .select(
                "project_id, labor_markup_amount, materials_markup_amount,"
                " gear_markup_amount, underground_markup_amount,"
                " low_voltage_markup_amount"
            )
            .in_("project_id", pids)
            .execute()
        ).data
        or []
    }
    verif_by_proj = {
        x["project_id"]: x
        for x in (sb.table("verifications").select("*").in_("project_id", pids).execute()).data or []
    }

    def _num(row: dict | None, key: str) -> Decimal | None:
        return Decimal(str(row[key])) if row and row.get(key) is not None else None

    out: dict[str, dict] = {}
    for pid in pids:
        mats = [a for a in _materials_amounts(rfqs_by_proj, quotes_by_rfq, gen_by_proj, pid) if a is not None]
        markup = markup_by_proj.get(pid)
        originals = {
            "labor_amount": _num(labor_by_proj.get(pid), "labor_amount"),
            # The TOTAL across every category (sections included), matching the
            # project summary box's aggregate semantics.
            "materials_amount": sum(mats, Decimal(0)) if mats else None,
            # Live section amounts are deliberately None here: bid_price only
            # exists once committed, and a committed snapshot is read as-is
            # (the section resolution rule), so the live figures never feed it.
            "gear_amount": None,
            "underground_amount": None,
            "low_voltage_amount": None,
            "labor_markup_amount": _num(markup, "labor_markup_amount"),
            "materials_markup_amount": _num(markup, "materials_markup_amount"),
            "gear_markup_amount": _num(markup, "gear_markup_amount"),
            "underground_markup_amount": _num(markup, "underground_markup_amount"),
            "low_voltage_markup_amount": _num(markup, "low_voltage_markup_amount"),
        }
        out[pid] = pricing_summary_numbers(originals, verif_by_proj.get(pid))
    return out


def _to_float(s: str | None) -> float | None:
    return float(s) if s is not None else None


# ── Sections ───────────────────────────────────────────────────────────────


def _send_out_cohort(w: WindowData) -> list[str]:
    return [pid for pid, at in w.submitted_at.items() if w.in_window(at)]


def send_out(w: WindowData, range_: str, benchmark: str, viewer_role: Role) -> dict:
    """Sent out: total / on-time / late vs the chosen bid-date benchmark."""
    can_actual = viewer_role in ACTUAL_BID_VIEWER_ROLES
    redacted = benchmark == "actual_bid_at" and not can_actual
    effective = "internal_bid_at" if redacted else benchmark

    rows: list[dict] = []
    counts = {"on_time": 0, "late": 0, "no_benchmark": 0}
    for pid in _send_out_cohort(w):
        p = w.projects[pid]
        sub = w.submitted_at.get(pid)
        # `effective` is the actual date only when the viewer is allowed to see it,
        # so the effective benchmark date is always safe to return.
        bench = _parse(p.get(effective))
        status = _on_time(sub, bench) or "no_benchmark"
        counts["no_benchmark" if status == "no_benchmark" else status] += 1
        rows.append(
            {
                **w.ref(pid),
                "submitted_at": _iso(sub),
                "benchmark_at": _iso(bench),
                "status": status,
            }
        )
    rows.sort(key=lambda r: r["submitted_at"] or "", reverse=True)
    trend = _build_trend(
        [(w.submitted_at[pid], 1.0) for pid in _send_out_cohort(w) if w.submitted_at.get(pid)],
        w.date_from,
        w.date_to,
    )
    return {
        "meta": meta(w, range_, "submitted_at"),
        "benchmark": effective,
        "benchmark_redacted": redacted,
        "total": len(rows),
        "on_time": counts["on_time"],
        "late": counts["late"],
        "no_benchmark": counts["no_benchmark"],
        "trend": trend,
        "projects": rows,
    }


def estimator(w: WindowData, range_: str) -> dict:
    """Estimator turnaround: received → returned, on time vs the estimator due date."""
    sb = get_supabase()
    pids = list(w.projects.keys())
    assigns = (
        (
            sb.table("estimator_assignments")
            .select("project_id, due_at, sent_to_estimator_at, returned_at, revoked_at")
            .in_("project_id", pids)
            .is_("revoked_at", "null")
            .execute()
        ).data
        if pids
        else []
    ) or []
    # One active assignment per project; if several, keep the most recently sent.
    by_proj: dict[str, dict] = {}
    for a in assigns:
        cur = by_proj.get(a["project_id"])
        if cur is None or (a.get("sent_to_estimator_at") or "") > (cur.get("sent_to_estimator_at") or ""):
            by_proj[a["project_id"]] = a

    rows: list[dict] = []
    counts = {"on_time": 0, "late": 0, "incomplete": 0}
    turnarounds: list[float] = []
    for pid, a in by_proj.items():
        sent = _parse(a.get("sent_to_estimator_at"))
        ret = _parse(a.get("returned_at"))
        anchor = ret or sent
        if not w.in_window(anchor):
            continue
        due = _parse(a.get("due_at")) or _parse(w.projects[pid].get("due_from_estimator_at"))
        turn = _hours(ret, sent)
        if turn is None:
            status = "incomplete"
        else:
            turnarounds.append(turn)
            status = _on_time(ret, due) or "incomplete"
        counts["incomplete" if status == "incomplete" else status] += 1
        rows.append(
            {
                **w.ref(pid),
                "sent_to_estimator_at": _iso(sent),
                "returned_at": _iso(ret),
                "due_at": _iso(due),
                "turnaround_hours": turn,
                "status": status,
            }
        )
    rows.sort(key=lambda r: r["returned_at"] or r["sent_to_estimator_at"] or "", reverse=True)
    return {
        "meta": meta(w, range_, "returned_at"),
        "total": len(rows),
        "on_time": counts["on_time"],
        "late": counts["late"],
        "incomplete": counts["incomplete"],
        "avg_turnaround_hours": _mean(turnarounds),
        "projects": rows,
    }


def _quotes_data(w: WindowData) -> dict:
    """Shared quote loading + per-project aggregation (used by section + detail)."""
    sb = get_supabase()
    pids = list(w.projects.keys())
    rfqs = (
        (
            sb.table("rfqs")
            .select(
                "id, project_id, due_date, material_categories(is_general)"
            )
            .in_("project_id", pids)
            .execute()
        ).data
        if pids
        else []
    ) or []
    rfqs_by_proj: dict[str, list[dict]] = defaultdict(list)
    rfq_proj: dict[str, str] = {}
    rfq_due: dict[str, datetime | None] = {}
    rfq_ids: list[str] = []
    for r in rfqs:
        rfqs_by_proj[r["project_id"]].append(r)
        rfq_proj[r["id"]] = r["project_id"]
        rfq_due[r["id"]] = _eod(r.get("due_date"))
        rfq_ids.append(r["id"])

    quotes = (
        (
            sb.table("quotes")
            # tax fields so _materials_amounts prices by tax-inclusive totals.
            .select("rfq_id, amount, is_selected, received_at, source, tax_included, tax_rate, origin")
            .in_("rfq_id", rfq_ids)
            .execute()
        ).data
        if rfq_ids
        else []
    ) or []
    quotes_by_rfq: dict[str, list[dict]] = defaultdict(list)
    quotes_by_proj: dict[str, list[dict]] = defaultdict(list)
    latest_received: dict[str, datetime] = {}
    for q in quotes:
        pid = rfq_proj.get(q["rfq_id"])
        if pid is None:
            continue
        quotes_by_rfq[q["rfq_id"]].append(q)
        quotes_by_proj[pid].append(q)
        rec = _parse(q.get("received_at"))
        if rec and (pid not in latest_received or rec > latest_received[pid]):
            latest_received[pid] = rec

    sends = (
        (
            sb.table("rfq_sends")
            .select("rfq_id, quote_received_at")
            .in_("rfq_id", rfq_ids)
            .execute()
        ).data
        if rfq_ids
        else []
    ) or []
    sends_by_proj: dict[str, list[dict]] = defaultdict(list)
    for s in sends:
        pid = rfq_proj.get(s["rfq_id"])
        if pid:
            sends_by_proj[pid].append(s)

    gen_by_proj = {
        g["project_id"]: g
        for g in (
            sb.table("general_material_estimates")
            .select("project_id, amount, source, tax_included, tax_rate")
            .in_("project_id", pids)
            .execute()
        ).data
        or []
    }
    return {
        "rfqs_by_proj": rfqs_by_proj,
        "rfq_due": rfq_due,
        "quotes_by_rfq": quotes_by_rfq,
        "quotes_by_proj": quotes_by_proj,
        "latest_received": latest_received,
        "sends_by_proj": sends_by_proj,
        "gen_by_proj": gen_by_proj,
    }


def _project_quote_row(w: WindowData, pid: str, d: dict) -> dict:
    p = w.projects[pid]
    vendor_due = _parse(p.get("due_from_vendors_at"))
    # Coverage: every category priced (custom > selected > lowest; general from estimate).
    amounts = _materials_amounts(d["rfqs_by_proj"], d["quotes_by_rfq"], d["gen_by_proj"], pid)
    total_cats = len(amounts)
    priced = sum(1 for a in amounts if a is not None)
    coverage_pct = round(priced / total_cats, 4) if total_cats else 0.0
    coverage_complete = total_cats > 0 and priced == total_cats

    on_time = late = manual = ai = 0
    for q in d["quotes_by_proj"].get(pid, []):
        rec = _parse(q.get("received_at"))
        due = d["rfq_due"].get(q["rfq_id"]) or vendor_due
        cls = _on_time(rec, due)
        if cls == "on_time":
            on_time += 1
        elif cls == "late":
            late += 1
        if q.get("source") == "ai_extracted":
            ai += 1
        else:
            manual += 1
    sends = d["sends_by_proj"].get(pid, [])
    responded = sum(1 for s in sends if s.get("quote_received_at"))
    return {
        **w.ref(pid),
        "coverage_complete": coverage_complete,
        "coverage_pct": coverage_pct,
        "quotes_on_time": on_time,
        "quotes_late": late,
        "manual_count": manual,
        "ai_extracted_count": ai,
        "vendor_response_rate": _rate(responded, len(sends)),
    }


def quotes(w: WindowData, range_: str) -> dict:
    d = _quotes_data(w)
    cohort = [pid for pid in w.projects if w.in_window(d["latest_received"].get(pid))]
    rows = [_project_quote_row(w, pid, d) for pid in cohort]
    rows.sort(key=lambda r: r["coverage_pct"])
    coverage_complete_count = sum(1 for r in rows if r["coverage_complete"])
    on_time = sum(r["quotes_on_time"] for r in rows)
    late = sum(r["quotes_late"] for r in rows)
    manual = sum(r["manual_count"] for r in rows)
    ai = sum(r["ai_extracted_count"] for r in rows)
    responded = sum(
        sum(1 for s in d["sends_by_proj"].get(pid, []) if s.get("quote_received_at")) for pid in cohort
    )
    sent = sum(len(d["sends_by_proj"].get(pid, [])) for pid in cohort)
    return {
        "meta": meta(w, range_, "quote_received_at"),
        "total": len(rows),
        "coverage_complete_count": coverage_complete_count,
        "avg_coverage_pct": _mean([r["coverage_pct"] for r in rows]) or 0.0,
        "quotes_on_time": on_time,
        "quotes_late": late,
        "manual_count": manual,
        "ai_extracted_count": ai,
        "ai_extraction_rate": _rate(ai, ai + manual),
        "vendor_response_rate": _rate(responded, sent),
        "projects": rows,
    }


def _stage_durations(w: WindowData, cohort: set[str]) -> tuple[list[dict], list[float]]:
    """Average time-in-stage (diff consecutive events) + per-project end-to-end."""
    durations: dict[str, list[float]] = defaultdict(list)
    end_to_end: list[float] = []
    for pid in cohort:
        evs = w.events_by_project.get(pid, [])
        for cur, nxt in zip(evs, evs[1:]):
            secs = (_parse(nxt["entered_at"]) - _parse(cur["entered_at"])).total_seconds()
            durations[cur["to_stage"]].append(secs / 3600)
        sub = w.submitted_at.get(pid)
        first = w.first_event_at.get(pid)
        if sub and first:
            end_to_end.append((sub - first).total_seconds() / 3600)
    time_in_stage = []
    for key, defn in sorted(STAGES.items(), key=lambda kv: kv[1].order):
        if key in ("pm_only", "cp_only"):
            continue  # never receive stage_events — permanent zero rows otherwise
        samples = durations.get(key, [])
        time_in_stage.append(
            {"stage": key, "label": defn.label, "avg_hours": _mean(samples), "samples": len(samples)}
        )
    return time_in_stage, end_to_end


def cycle_time(w: WindowData, range_: str) -> dict:
    cohort = set(_send_out_cohort(w))
    time_in_stage, end_to_end = _stage_durations(w, cohort)

    breakdowns: dict[str, list[dict]] = {}
    for dim in ("wage_type", "est_value_band", "labor_time"):
        groups: dict[str, list[float]] = defaultdict(list)
        for pid in cohort:
            sub = w.submitted_at.get(pid)
            first = w.first_event_at.get(pid)
            if not (sub and first):
                continue
            key = w.projects[pid].get(dim) or "unknown"
            groups[key].append((sub - first).total_seconds() / 3600)
        labels = GROUP_LABELS[dim]
        breakdowns[dim] = [
            {"key": k, "label": _group_label(labels, k), "avg_hours": _mean(v), "samples": len(v)}
            for k, v in sorted(groups.items())
        ]
    return {
        "meta": meta(w, range_, "submitted_at"),
        "time_in_stage": time_in_stage,
        "avg_end_to_end_hours": _mean(end_to_end),
        "breakdowns": breakdowns,
    }


# ── Outcomes / KPIs ────────────────────────────────────────────────────────


def _outcomes_for(pids: list[str]) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    sb = get_supabase()
    if not pids:
        return {}, {}
    outs = {
        o["project_id"]: o
        for o in (
            sb.table("bid_outcomes").select("project_id, result").in_("project_id", pids).execute()
        ).data
        or []
    }
    gc_rows: dict[str, list[dict]] = defaultdict(list)
    for g in (
        sb.table("bid_gc_outcomes")
        .select("project_id, our_bid_selection, our_amount, winning_amount")
        .in_("project_id", pids)
        .execute()
    ).data or []:
        gc_rows[g["project_id"]].append(g)
    return outs, gc_rows


def _kpis(w: WindowData, cohort: list[str], pricing: dict[str, dict]) -> dict:
    outs, gc_rows = _outcomes_for(cohort)
    won = sum(1 for o in outs.values() if o["result"] == "won")
    lost = sum(1 for o in outs.values() if o["result"] == "lost")
    no_award = sum(1 for o in outs.values() if o["result"] == "no_award")

    variances: list[float] = []
    used_us = used_known = 0
    for rows in gc_rows.values():
        for g in rows:
            sel = g.get("our_bid_selection")
            if sel in ("used_us", "used_other"):
                used_known += 1
                if sel == "used_us":
                    used_us += 1
            ours, theirs = g.get("our_amount"), g.get("winning_amount")
            if ours is not None and theirs not in (None, 0):
                variances.append(abs(float(ours) - float(theirs)) / float(theirs))

    # Estimator on-time across the cohort (going-forward data only).
    est = estimator(w, "")
    quo = quotes(w, "")
    decided = won + lost
    return {
        "win_rate": _rate(won, decided),
        "won": won,
        "lost": lost,
        "no_award": no_award,
        "bid_accuracy_pct": round(sum(variances) / len(variances), 4) if variances else None,
        "used_us_rate": _rate(used_us, used_known),
        "estimator_on_time_rate": _rate(est["on_time"], est["on_time"] + est["late"]),
        "vendor_response_rate": quo["vendor_response_rate"],
        "quote_coverage_rate": _rate(quo["coverage_complete_count"], quo["total"]),
        "ai_extraction_rate": quo["ai_extraction_rate"],
        "avg_quotes_per_project": round(
            (quo["manual_count"] + quo["ai_extracted_count"]) / quo["total"], 2
        )
        if quo["total"]
        else None,
        "decline_rate": _decline_rate(w),
    }


def _decline_rate(w: WindowData) -> float | None:
    """Share of go/no-go decisions in the window that ended in 'declined'. A
    go_no_go decision is the only source of a 'declined' or 'to_estimator' event."""
    declined = decided = 0
    for evs in w.events_by_project.values():
        for e in evs:
            if e["to_stage"] in ("declined", "to_estimator") and w.in_window(_parse(e["entered_at"])):
                decided += 1
                if e["to_stage"] == "declined":
                    declined += 1
    return _rate(declined, decided)


def _delta(value: float | None, previous: float | None) -> dict:
    if value is None or previous in (None, 0):
        delta_pct = None
    else:
        delta_pct = round((value - previous) / abs(previous), 4)
    return {"value": value, "previous": previous, "delta_pct": delta_pct}


def _overview_core(w: WindowData) -> dict:
    cohort = _send_out_cohort(w)
    pricing = _load_pricing(cohort)
    bid_values = [_to_float(pricing[pid]["bid_price"]) for pid in cohort]
    bid_values = [v for v in bid_values if v is not None]
    times = [
        (w.submitted_at[pid] - w.first_event_at[pid]).total_seconds() / 3600
        for pid in cohort
        if w.submitted_at.get(pid) and w.first_event_at.get(pid)
    ]
    send = send_out(w, "", "internal_bid_at", Role.ESTIMATING_ADMIN)
    classifiable = send["on_time"] + send["late"]
    return {
        "cohort": cohort,
        "pricing": pricing,
        "projects_bid": len(cohort),
        "total_bid_amount": round(sum(bid_values), 2) if bid_values else None,
        "avg_time_to_bid_hours": _mean(times),
        "on_time_rate": _rate(send["on_time"], classifiable),
    }


def overview(w: WindowData, range_: str) -> dict:
    core = _overview_core(w)
    cohort = core["cohort"]
    pricing = core["pricing"]

    # Previous equal-length window for deltas (skipped in per-project mode).
    prev = None
    if not w.is_project_mode:
        span = w.date_to - w.date_from
        pw = load_window(w.date_from - span, w.date_from, None)
        prev = _overview_core(pw)

    def d(key: str) -> dict:
        return _delta(core[key], prev[key] if prev else None)

    # Breakdowns by dimension: count, $ bid, avg time-to-bid.
    breakdowns: dict[str, list[dict]] = {}
    for dim in ("wage_type", "est_value_band", "labor_time"):
        groups: dict[str, dict] = defaultdict(lambda: {"count": 0, "values": [], "times": []})
        for pid in cohort:
            key = w.projects[pid].get(dim) or "unknown"
            g = groups[key]
            g["count"] += 1
            bv = _to_float(pricing[pid]["bid_price"])
            if bv is not None:
                g["values"].append(bv)
            if w.submitted_at.get(pid) and w.first_event_at.get(pid):
                g["times"].append(
                    (w.submitted_at[pid] - w.first_event_at[pid]).total_seconds() / 3600
                )
        labels = GROUP_LABELS[dim]
        breakdowns[dim] = [
            {
                "key": k,
                "label": _group_label(labels, k),
                "count": g["count"],
                "total_bid_amount": round(sum(g["values"]), 2) if g["values"] else None,
                "avg_time_to_bid_hours": _mean(g["times"]),
            }
            for k, g in sorted(groups.items())
        ]

    trend = _build_trend(
        [(w.submitted_at[pid], 1.0) for pid in cohort if w.submitted_at.get(pid)],
        w.date_from,
        w.date_to,
    )
    value_trend = _build_trend(
        [
            (w.submitted_at[pid], _to_float(pricing[pid]["bid_price"]) or 0.0)
            for pid in cohort
            if w.submitted_at.get(pid)
        ],
        w.date_from,
        w.date_to,
    )
    kpis = _kpis(w, cohort, pricing)
    return {
        "meta": meta(w, range_, "submitted_at"),
        "projects_bid": d("projects_bid"),
        "total_bid_amount": d("total_bid_amount"),
        "avg_time_to_bid_hours": d("avg_time_to_bid_hours"),
        "on_time_rate": d("on_time_rate"),
        "win_rate": _delta(kpis["win_rate"], None),
        "kpis": kpis,
        "breakdowns": breakdowns,
        "trend": trend,
        "value_trend": value_trend,
    }


def win_loss(w: WindowData, range_: str) -> dict:
    cohort = _send_out_cohort(w)
    outs, gc_rows = _outcomes_for(cohort)
    won = sum(1 for o in outs.values() if o["result"] == "won")
    lost = sum(1 for o in outs.values() if o["result"] == "lost")
    no_award = sum(1 for o in outs.values() if o["result"] == "no_award")
    variances: list[float] = []
    rows: list[dict] = []
    for pid in cohort:
        o = outs.get(pid)
        if not o:
            continue
        proj_var: list[float] = []
        for g in gc_rows.get(pid, []):
            ours, theirs = g.get("our_amount"), g.get("winning_amount")
            if ours is not None and theirs not in (None, 0):
                v = abs(float(ours) - float(theirs)) / float(theirs)
                proj_var.append(v)
                variances.append(v)
        rows.append(
            {
                **w.ref(pid),
                "result": o["result"],
                "variance_pct": _mean(proj_var),
            }
        )
    return {
        "meta": meta(w, range_, "submitted_at"),
        "won": won,
        "lost": lost,
        "no_award": no_award,
        "win_rate": _rate(won, won + lost),
        "bid_accuracy": {
            "avg_variance_pct": round(sum(variances) / len(variances), 4) if variances else None,
            "samples": len(variances),
        },
        "projects": rows,
    }


# ── GC pricing spread ──────────────────────────────────────────────────────


def _gc_spread_compute(
    sends_by_project: dict[str, list[tuple[str, str, float]]],
) -> dict:
    """Pure per-job pricing-spread math (no DB), so it unit-tests like
    outcome.merge_gc_outcomes. Input: per project, a list of (gc_id, gc_name,
    total) where total = material + labor for one status='sent' proposal_send.

    On every job we bid to ≥2 GCs, each GC's number is compared to the *mean of
    the OTHER GCs on that same job* (leave-one-out): a positive delta means we bid
    that GC higher than its peers. Single-GC jobs still count toward the totals and
    the per-GC leaderboard, but carry no spread (no peers to compare against).

    Aggregation rules:
      * `avg_abs_spread_pct` pools |pct| over every per-(job, GC) entry — NOT an
        average of per-GC averages — so a GC seen on one wild job can't skew it.
      * each GC's `avg_signed_pct` is the simple mean of that GC's per-job signed
        pcts (one entry per shared job), keeping sign for higher/lower direction.
    """
    by_gc: dict[str, dict] = defaultdict(
        lambda: {
            "gc_name": "",
            "times_bid": 0,
            "total_amount": 0.0,
            "signed_pcts": [],  # one signed pct per shared (multi-GC) job
            "times_higher": 0,
            "times_lower": 0,
        }
    )
    abs_entries: list[float] = []  # pooled |pct| over every multi-GC (job, GC) entry
    spread_by_project: dict[str, dict] = {}
    gc_send_count = 0
    jobs_with_sends = 0
    multi_gc_jobs = 0
    total_bid_amount = 0.0

    for pid, sends in sends_by_project.items():
        # One valid, positive number per GC. proposal_sends is unique per
        # (project, gc), so dupes shouldn't occur — guard by keeping the max.
        valid: dict[str, tuple[str, float]] = {}
        for gc_id, gc_name, total in sends:
            if total is None or total <= 0:
                continue
            prev = valid.get(gc_id)
            if prev is None or total > prev[1]:
                valid[gc_id] = (gc_name, total)
        if not valid:
            continue
        jobs_with_sends += 1
        gc_send_count += len(valid)

        # Leaderboard + grand total count every valid send (incl. single-GC jobs).
        for gc_id, (gc_name, total) in valid.items():
            agg = by_gc[gc_id]
            agg["gc_name"] = gc_name
            agg["times_bid"] += 1
            agg["total_amount"] += total
            total_bid_amount += total

        n = len(valid)
        if n < 2:
            continue  # no peers → no spread
        multi_gc_jobs += 1
        sum_all = sum(t for _, t in valid.values())
        job_abs: list[float] = []
        for gc_id, (gc_name, total) in valid.items():
            mean_others = (sum_all - total) / (n - 1)
            if mean_others <= 0:
                continue  # all peers filtered out — can't form a baseline
            delta = total - mean_others
            pct = delta / mean_others
            agg = by_gc[gc_id]
            agg["signed_pcts"].append(pct)
            if delta > 0:
                agg["times_higher"] += 1
            elif delta < 0:
                agg["times_lower"] += 1
            abs_entries.append(abs(pct))
            job_abs.append(abs(pct))
        spread_by_project[pid] = {
            "n": n,
            "total": sum_all,
            "avg_pct": round(sum(job_abs) / len(job_abs), 4) if job_abs else None,
            "max_abs_pct": round(max(job_abs), 4) if job_abs else None,
        }

    leaderboard = [
        {
            "gc_id": gc_id,
            "gc_name": agg["gc_name"],
            "times_bid": agg["times_bid"],
            "total_amount": round(agg["total_amount"], 2),
            "avg_signed_pct": _mean_signed(agg["signed_pcts"]),
            "times_higher": agg["times_higher"],
            "times_lower": agg["times_lower"],
            "multi_gc_jobs": len(agg["signed_pcts"]),
        }
        for gc_id, agg in by_gc.items()
    ]
    leaderboard.sort(key=lambda r: r["total_amount"], reverse=True)

    return {
        "gc_send_count": gc_send_count,
        "jobs_with_sends": jobs_with_sends,
        "multi_gc_jobs": multi_gc_jobs,
        "total_bid_amount": round(total_bid_amount, 2) if gc_send_count else None,
        "avg_abs_spread_pct": (
            round(sum(abs_entries) / len(abs_entries), 4) if abs_entries else None
        ),
        "by_gc": leaderboard,
        "_spread_by_project": spread_by_project,
    }


def gc_spread(w: WindowData, range_: str) -> dict:
    """GC pricing spread: on jobs we bid to multiple GCs, how much higher/lower we
    bid each GC vs. the others on that same job. Cohort + every filter come from
    the standard send-out machinery; the per-GC numbers from sent proposal_sends."""
    cohort = _send_out_cohort(w)
    rows: list[dict] = []
    if cohort:
        rows = (
            get_supabase()
            .table("proposal_sends")
            .select(
                "project_id, gc_id, gc_name, material_amount, gear_amount,"
                " underground_amount, low_voltage_amount, labor_amount"
            )
            .in_("project_id", cohort)
            .eq("status", "sent")
            .execute()
        ).data or []

    sends_by_project: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    for r in rows:
        amt = our_amount_of(r)
        if amt is None:
            continue
        sends_by_project[r["project_id"]].append((r["gc_id"], r["gc_name"], float(amt)))

    res = _gc_spread_compute(sends_by_project)
    spread_by_project = res.pop("_spread_by_project")

    projects = [
        {
            **w.ref(pid),
            "gc_count": sp["n"],
            "avg_spread_pct": sp["avg_pct"],
            "max_abs_spread_pct": sp["max_abs_pct"],
            "total_amount": round(sp["total"], 2),
        }
        for pid, sp in spread_by_project.items()
    ]
    projects.sort(key=lambda r: r["max_abs_spread_pct"] or 0.0, reverse=True)

    return {
        "meta": meta(w, range_, "submitted_at"),
        "gc_send_count": res["gc_send_count"],
        "jobs_with_sends": res["jobs_with_sends"],
        "multi_gc_jobs": res["multi_gc_jobs"],
        "total_bid_amount": res["total_bid_amount"],
        "avg_abs_spread_pct": res["avg_abs_spread_pct"],
        "by_gc": res["by_gc"],
        "projects": projects,
    }


def project_detail(w: WindowData, range_: str, viewer_role: Role) -> dict:
    """All sections for a single project (drives the drill-down modal)."""
    pid = w.project_id
    p = w.projects.get(pid, {})
    can_actual = viewer_role in ACTUAL_BID_VIEWER_ROLES

    est_rows = estimator(w, range_)["projects"]
    quo = _quotes_data(w)
    q_row = _project_quote_row(w, pid, quo) if pid in w.projects else None
    tis, e2e = _stage_durations(w, {pid})
    pricing = _load_pricing([pid]).get(pid, {})
    outs, gc_rows = _outcomes_for([pid])
    out = outs.get(pid)
    gc = gc_rows.get(pid, [])
    variances = [
        abs(float(g["our_amount"]) - float(g["winning_amount"])) / float(g["winning_amount"])
        for g in gc
        if g.get("our_amount") is not None and g.get("winning_amount") not in (None, 0)
    ]

    sub = w.submitted_at.get(pid)
    return {
        "project": {
            **w.ref(pid),
            "wage_type": p.get("wage_type"),
            "labor_time": p.get("labor_time"),
            "est_value_band": p.get("est_value_band"),
        },
        "send_out": {
            "submitted_at": _iso(sub),
            "internal_bid_at": _iso(_parse(p.get("internal_bid_at"))),
            "actual_bid_at": _iso(_parse(p.get("actual_bid_at"))) if can_actual else None,
            "on_time_internal": _on_time(sub, _parse(p.get("internal_bid_at"))),
            "on_time_actual": _on_time(sub, _parse(p.get("actual_bid_at"))) if can_actual else None,
        },
        "estimator": est_rows[0] if est_rows else None,
        "quotes": q_row,
        "cycle_time": {"time_in_stage": tis, "total_hours": _mean(e2e)},
        "pricing": {k: _to_float(v) for k, v in pricing.items()},
        "outcome": {
            "result": out["result"] if out else None,
            "avg_variance_pct": _mean(variances),
            "gcs": [
                {
                    "our_amount": _to_float(str(g["our_amount"])) if g.get("our_amount") is not None else None,
                    "winning_amount": _to_float(str(g["winning_amount"]))
                    if g.get("winning_amount") is not None
                    else None,
                    "used_us": g.get("our_bid_selection") == "used_us",
                }
                for g in gc
            ],
        }
        if out
        else None,
    }


# ── Activity log (who did what) ────────────────────────────────────────────
# The Analytics → Activity sub-page is the first read surface for the audit_log
# table (every recorded write: project edits, pricing, verify commits, file
# uploads, votes, user management …). We merge it with stage_events (who advanced
# each pipeline step) into one chronological "who did what / who inputted what"
# feed. Read-only; the route gates it to internal roles (the accountant included).

# Friendly labels for the audit `action` taxonomy. Unknown actions fall back to
# the raw key so newly-added audit actions still render.
ACTIVITY_ACTION_LABELS: dict[str, str] = {
    "stage.advance": "Advanced stage",
    "project.create": "Created project",
    "project.update": "Edited project details",
    "project.abandon": "Abandoned project",
    "project.reactivate": "Reactivated project",
    "project.gc_add": "Added a GC",
    "project.gc_remove": "Removed a GC",
    "project.send_out": "Sent proposals",
    "project.send_out_complete": "Completed send-out",
    "project.bid_outcome": "Recorded win/loss",
    "estimator.assign": "Assigned estimator",
    "estimator.revoke": "Revoked estimator",
    "estimator.email_sent": "Emailed estimator",
    "estimator.submit": "Estimator submitted estimate",
    "estimator.log_view": "Estimator viewed the send log",
    "estimator.updates_sent": "Sent file updates to estimator",
    "estimator.revision_submit": "Estimator sent revisions",
    "gono.go": "Go/No-Go: Go",
    "gono.no_go": "Go/No-Go: No-Go",
    "gono.vote": "Go/No-Go vote",
    "labor.review": "Set labor numbers",
    "markup.set": "Set markup",
    "pricing.verify_edit": "Edited verify numbers",
    "pricing.commit": "Committed pricing (verify)",
    "boq.analyze": "Ran BOQ analysis",
    "boq.refine": "Refined BOQ analysis",
    "boq.edit": "Edited BOQ analysis",
    "boq.confirm": "Confirmed BOQ → RFQs",
    "general_material.extract": "Extracted general material",
    "general_material.set": "Set general material price",
    "general_material.tax": "Set general material tax",
    "rfq.create": "Created RFQ",
    "rfq.bulk_send": "Sent RFQs",
    "rfq.send_one": "Sent an RFQ",
    "rfq.quotes_confirmed": "Confirmed quotes complete",
    "rfq.attachment_skipped": "Skipped an RFQ attachment",
    "rfq.reply_received": "RFQ reply received",
    "rfq.reply_sender_mismatch": "RFQ reply sender mismatch",
    "quote.add": "Added a quote",
    "quote.select": "Selected a quote",
    "quote.custom_price": "Set a custom quote price",
    "quote.override": "Overrode a quote",
    "quote.delete": "Removed a quote",
    "quote.received": "Recorded a quote received",
    "quote.tax": "Set quote tax",
    "note.create": "Posted a note",
    "proposal.lines_generate": "Generated proposal scope",
    "proposal.lines_save": "Saved proposal scope",
    "proposal.approve": "Approved proposal scope",
    "proposal.amounts_set": "Set per-GC amounts",
    "proposal.generate": "Generated proposal",
    "proposal.generate_docs": "Generated proposal docs",
    "proposal.send": "Sent a proposal",
    "proposal.send_failed": "Proposal send failed",
    "file.upload": "Uploaded a file",
    "file.download": "Downloaded a file",
    "file.preview": "Previewed a file",
    "file.delete": "Deleted a file",
    "file.note_updated": "Edited a file note",
    "file.export": "Exported project files",
    "files.review_changes": "Marked estimator changes reviewed",
    "access.denied": "Access denied",
    "user.invite": "Invited a user",
    "user.reinvite": "Re-invited a user",
    "user.update": "Updated a user",
    "user.update_self": "Updated own profile",
    "user.dev_role_switch": "Switched own role (dev)",
    "user.notification_prefs.update": "Updated notification prefs",
    "user.notification_prefs.reset": "Reset notification prefs",
    "user.mfa.admin_reset": "Reset a user's 2FA",
    "user.mfa.self_reset": "Reset own 2FA",
    # Project Management
    "pm.activate": "Won bid — entered PM",
    "pm.retract": "Retracted PM entry (outcome corrected)",
    "pm.project_create": "Created PM project",
    "pm.details_update": "Edited PM details",
    "pm.stage_change": "Moved PM stage",
    "pm.complete": "Marked PM project complete",
    "co.create": "Added a change order",
    "co.update": "Updated a change order",
    "co.delete": "Deleted a change order",
    "sov.create": "Added a schedule-of-values line",
    "sov.update": "Updated a schedule-of-values line",
    "sov.delete": "Deleted a schedule-of-values line",
    "payapp.create": "Created a pay application",
    "payapp.update": "Updated a pay application",
    "payapp.delete": "Deleted a pay application",
    "milestone.create": "Added a milestone",
    "milestone.update": "Updated a milestone",
    "milestone.delete": "Deleted a milestone",
    "dailylog.create": "Added a daily log",
    "dailylog.update": "Updated a daily log",
    "dailylog.delete": "Deleted a daily log",
    "rfi.create": "Created an RFI",
    "rfi.update": "Updated an RFI",
    "rfi.close": "Closed an RFI",
    "rfi.delete": "Deleted an RFI",
    "manpower.create": "Added a manpower entry",
    "manpower.update": "Updated a manpower entry",
    "manpower.delete": "Deleted a manpower entry",
    "pm_doc.upload": "Uploaded a PM document",
    "pm_doc.download": "Downloaded a PM document",
    "pm_doc.delete": "Deleted a PM document",
}


def _activity_project_label(proj: dict | None) -> str | None:
    if not proj:
        return None
    number = (proj.get("number") or "").strip()
    name = (proj.get("name") or "").strip()
    return " — ".join(p for p in (number, name) if p) or None


def activity_log(
    range_: str,
    date_from: str | None,
    date_to: str | None,
    actor_id: str | None,
    action: str | None,
    project_id: str | None,
    limit: int,
    offset: int,
) -> dict:
    """One chronological feed of audit_log + stage_events within the window.

    Filterable by actor, action and project; paginated in Python over the merged
    list. Each source is capped (so a runaway window can't OOM) — `truncated` flags
    when a cap was hit so the UI can tell the user to narrow the range.
    """
    df, dt = resolve_range(range_, date_from, date_to)  # raises ValueError on bad input
    sb = get_supabase()
    df_iso, dt_iso = df.isoformat(), dt.isoformat()
    CAP = 1000

    # audit_log — skip entirely when filtering for the synthetic stage.advance.
    audit_rows: list[dict] = []
    if not action or action != "stage.advance":
        aq = (
            sb.table("audit_log")
            .select("id, actor_id, action, entity, entity_id, payload, created_at")
            .gte("created_at", df_iso)
            .lte("created_at", dt_iso)
        )
        if actor_id:
            aq = aq.eq("actor_id", actor_id)
        if action:
            aq = aq.eq("action", action)
        if project_id:
            # Most pipeline actions audit entity='project', entity_id=<project>.
            aq = aq.eq("entity", "project").eq("entity_id", project_id)
        audit_rows = aq.order("created_at", desc=True).limit(CAP).execute().data or []

    # stage_events — synthesized as stage.advance.
    events: list[dict] = []
    if not action or action == "stage.advance":
        eq = (
            sb.table("stage_events")
            .select("id, project_id, from_stage, to_stage, note, actor_id, entered_at")
            .gte("entered_at", df_iso)
            .lte("entered_at", dt_iso)
        )
        if actor_id:
            eq = eq.eq("actor_id", actor_id)
        if project_id:
            eq = eq.eq("project_id", project_id)
        events = eq.order("entered_at", desc=True).limit(CAP).execute().data or []

    # Resolve actor names/roles and project labels in batch.
    actor_ids = {r["actor_id"] for r in audit_rows if r.get("actor_id")}
    actor_ids |= {e["actor_id"] for e in events if e.get("actor_id")}
    actors: dict[str, dict] = {}
    if actor_ids:
        prof = (
            sb.table("profiles").select("id, full_name, role").in_("id", list(actor_ids)).execute()
        ).data or []
        actors = {p["id"]: p for p in prof}

    proj_ids = {e["project_id"] for e in events if e.get("project_id")}
    proj_ids |= {r["entity_id"] for r in audit_rows if r.get("entity") == "project" and r.get("entity_id")}
    projects: dict[str, dict] = {}
    if proj_ids:
        prows = (
            sb.table("projects").select("id, number, name").in_("id", list(proj_ids)).execute()
        ).data or []
        projects = {p["id"]: p for p in prows}

    def _row(rid, at, aid, act, entity, pid, detail) -> dict:
        prof = actors.get(aid) if aid else None
        return {
            "id": rid,
            "at": at,
            "actor_id": aid,
            "actor_name": (prof or {}).get("full_name"),
            "actor_role": (prof or {}).get("role"),
            "action": act,
            "action_label": ACTIVITY_ACTION_LABELS.get(act, act),
            "entity": entity,
            "project_id": pid,
            "project_label": _activity_project_label(projects.get(pid)) if pid else None,
            "detail": detail,
        }

    rows: list[dict] = []
    for r in audit_rows:
        pid = r["entity_id"] if r.get("entity") == "project" else None
        rows.append(
            _row(r["id"], r["created_at"], r.get("actor_id"), r["action"], r.get("entity"), pid, r.get("payload"))
        )
    for e in events:
        rows.append(
            _row(
                f"stage:{e['id']}",
                e["entered_at"],
                e.get("actor_id"),
                "stage.advance",
                "project",
                e.get("project_id"),
                {"from": e.get("from_stage"), "to": e.get("to_stage"), "note": e.get("note")},
            )
        )

    rows.sort(key=lambda x: _parse(x["at"]) or utcnow(), reverse=True)
    total = len(rows)
    truncated = len(audit_rows) >= CAP or len(events) >= CAP
    page = rows[offset : offset + limit]
    actions_present = sorted({r["action"] for r in rows})

    return {
        "rows": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "truncated": truncated,
        "available_actions": [
            {"value": a, "label": ACTIVITY_ACTION_LABELS.get(a, a)} for a in actions_present
        ],
    }

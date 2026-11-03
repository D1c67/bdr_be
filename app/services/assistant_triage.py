"""Phase 8 triage service (docs/ASSISTANT_PHASE8_PLAN.md 5.1 to 5.3, 5.7, 5.8).

The triage loop is separate from the Phase 7 router and makes no model call
itself: it claims one person's batch at a time through the 0196 RPCs (via
app.assistant.repo), checks the router's own eligibility test (no delegation,
owner question 15 (c)), builds a facts-only pack with app.assistant.triage,
calls the one model function `assistant_triage_model.classify`, and stores
every decision through the fenced finish, which also emits escalations to
the router. It never sends anything and never names the model service, the
turn loop, a sender or a delegation helper (tests/test_assistant_triage_pins.py).

One tick (`poll_once`), each step in its own try:
  1. gate: ASSISTANT_ENABLED, ASSISTANT_PROACTIVE_ENABLED and
     ASSISTANT_TRIAGE_ENABLED, else `env_off`; then the company row read
     once: `enabled`, `proactive_enabled` and `triage_enabled` must all be
     on, else `triage_skip_all(switched_off)` and stop;
  2. fuse: while the latch `triage_fuse_tripped_at` is set,
     `triage_skip_all(fuse)` and stop. Only an off and on flip of the
     company switch clears it (owner question 10 (a)); nothing here does;
  3. breaker: while the per-process breaker is open, no enqueue and no
     claim; once it is due a probe, one claim only;
  4. enqueue with the optional allowlist (None when it is empty);
  5. up to ASSISTANT_TRIAGE_BATCHES_PER_TICK claims, strictly one claim,
     model call and finish at a time, each claim passing the allowlist and
     the two fuse settings. A `{fuse}` or `{switched_off}` answer ends the
     tick; the first model error, timeout or busy gate opens the breaker
     (2, 4, 8, then 15 minutes between probes; a success closes it); a cap
     refusal ends the tick and pauses claims in this process for
     ASSISTANT_TRIAGE_CAP_PAUSE_SECONDS (the finish defers the batch to the
     next Pacific midnight, owner question 13 (b));
  6. once a Pacific day after 02:00, the run purge and the release of the
     company's own triage holds still active more than an hour after they
     expired (MC-1), each in its own try; a failed part is retried an hour
     later, never every tick. This step also runs while triage is switched
     off or the fuse is latched, so a crash never leaves a company hold open.

Time inside a tick: `now` is the tick's start (the tick script travels it),
and the real time spent since then is added to it for every claim, finish,
breaker trip and cap pause, so a busy or timeout failure that arrives after
minutes of waiting opens the breaker from the moment it failed, and a slow
tick never stamps a decision early.

Per batch (5.3): the router's eligibility (an active internal person with a
working assistant, `_Context.eligible` for the escalation kind, whose sub-app
is none); a person who fails it is settled `skipped ineligible` through
`triage_skip_user` with no model call. Then per item: a dismissed or missing
inbox row settles `dismissed`; a cap-deferred item settles `cap` once its row
is 24 hours old and is otherwise never judged by the window (MC-7); any other
item older than ASSISTANT_TRIAGE_WINDOW_HOURS, or a `bell.due` item whose
`internal_bid_at` has passed, settles `stale`. The run status and error kind
come only from the model module's closed exception set (5.4).

Correctness across both uvicorn workers lives in Postgres (5.8): the claim is
per person under an advisory lock, leases run on the real clock, and the
finish is fenced. The breaker and the cap pause are per process.

Public surface:
  poll_once(now=None, *, worker=None) -> dict   one tick, counts only
  polling_loop()                                the lifespan loop, every
                                                ASSISTANT_TRIAGE_POLL_SECONDS
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from app.assistant import proactive, repo, spend, triage
from app.core import supabase_client
from app.core.config import get_settings
from app.services import assistant_proactive, assistant_triage_model, due_digest

logger = logging.getLogger(__name__)

PT = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc

# One id per process, as assistant_proactive.WORKER_ID.
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

SKIP_ALL_LIMIT = 500
ENQUEUE_LIMIT = 500
CAP_AGE_OUT = timedelta(hours=24)            # P13: a cap-deferred row this old at its deferral midnight settles `cap`
CAP_STAGGER = timedelta(seconds=7200)        # A4: the retry helper's per-item offset after midnight (0196)
CAP_STAGGER_GRACE = timedelta(minutes=15)    # P8L-2-R1: one pause or lease of slack past the stagger
ESCALATED_LOOKBACK = timedelta(hours=2)      # the pack's escalated_project_last_2h
PART_MISSING_HORIZON = timedelta(hours=24)   # your_part_missing only for bids due inside 24 hours
DUE_KIND = "bell.due"                        # the record kind that goes stale when the bid is due
PURGE_AFTER = time(2, 0)
PURGE_LIMIT = 500
COMPANY_HOLD_LIMIT = 50
BREAKER_STEPS = (120, 240, 480, 900)         # 2, 4, 8, then 15 minutes between probes (5.7)
PURGE_RETRY = timedelta(hours=1)             # a failed purge part waits this long, never every tick
RELEASE_ERROR_KIND = "lease_expired"         # an infrastructure release, no attempt counted (5.3 step 2)
TODO_CHUNK = 200
# Closed-set failures raised before any model call (5.4): no model on the run row.
_NOT_CALLED = (assistant_triage_model.TriageUnpriced, assistant_triage_model.TriageSpendCheck)

# Batch outcomes (the counts poll_once returns).
OUT_DECIDED = "decided"
OUT_SKIPPED = "skipped_only"
OUT_INELIGIBLE = "ineligible"
OUT_RELEASED = "released"
OUT_FENCED = "fenced"


@dataclass
class _Outcome:
    kind: str                      # decided, skipped_only, ineligible, released, fenced, or an error kind
    error: type | None = None      # the closed-set class when the model call failed
    emitted: int = 0
    model_called: bool = False


class _ProcessState:
    """What this process remembers between ticks: the breaker, the cap
    pause, the day the purge last ran and one-time log flags. Shared by the
    threads of one process; correctness never depends on it (5.8)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.breaker_failures = 0
        self.breaker_open_until: datetime | None = None
        self.cap_pause_until: datetime | None = None
        self.purged_day: date | None = None
        self.purge_retry_at: datetime | None = None
        self.fuse_logged = False
        self.cap_logged_day: date | None = None
        self.allowlist_warned: frozenset[str] = frozenset()


_STATE = _ProcessState()


def reset_state() -> None:
    """Forget the breaker, the cap pause and the purge day (tests and the
    tick script start clean)."""
    with _STATE.lock:
        _STATE.reset()


# ── Small helpers ────────────────────────────────────────────────────────────


def _clock() -> datetime:
    """The real clock (the tests replace it with the fake database clock)."""
    return datetime.now(UTC)


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)


def _int(settings: Any, name: str, default: int) -> int:
    try:
        value = getattr(settings, name, None)
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _float(settings: Any, name: str, default: float) -> float:
    try:
        value = getattr(settings, name, None)
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _env_on(settings: Any) -> bool:
    return bool(getattr(settings, "assistant_enabled", False)
                and getattr(settings, "assistant_proactive_enabled", False)
                and getattr(settings, "assistant_triage_enabled", False))


def _switch_on(row: Mapping[str, Any] | None) -> bool:
    """The three company switches of 5.1 step 1; a missing row fails closed."""
    return bool(row and row.get("enabled") and row.get("proactive_enabled") and row.get("triage_enabled"))


def _step(out: dict[str, Any], name: str, fn, *args, **kwargs) -> Any:
    """Run one tick step in its own try (the router's _step shape); a failure
    is logged with its class only and recorded as an error name."""
    try:
        out[name] = fn(*args, **kwargs)
        return out[name]
    except Exception as exc:  # noqa: BLE001 - one step never stops the others
        logger.warning("assistant triage: step %s failed (%s)", name, exc.__class__.__name__, exc_info=True)
        out.setdefault("errors", []).append(name)
        return None


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (TypeError, ValueError):
        return False
    return True


def resolve_allowlist(settings: Any, profiles: Mapping[str, Mapping[str, Any]] | None) -> list[str] | None:
    """ASSISTANT_TRIAGE_USER_ALLOWLIST as profile ids for p_user_ids (13).
    Empty (the default) is None, which means everyone who has the assistant.
    Entries are profile ids or emails; an entry that matches no profile is
    dropped and logged once per process. A non-empty setting that matches
    nobody is an empty list, which claims nobody (never everyone)."""
    raw = str(getattr(settings, "assistant_triage_user_allowlist", "") or "")
    entries = [p.strip().lower() for p in raw.split(",") if p.strip()]
    if not entries:
        return None
    people = profiles or {}
    by_email = {str(p.get("email") or "").strip().lower(): str(pid) for pid, p in people.items()
                if str(p.get("email") or "").strip()}
    ids = {str(pid).lower(): str(pid) for pid in people}
    out: list[str] = []
    dropped: list[str] = []
    for entry in entries:
        match = ids.get(entry) if _is_uuid(entry) else by_email.get(entry)
        if match is None:
            dropped.append(entry)
        elif match not in out:
            out.append(match)
    if dropped:
        with _STATE.lock:
            fresh = frozenset(dropped) - _STATE.allowlist_warned
            _STATE.allowlist_warned |= frozenset(dropped)
        if fresh:
            logger.warning("assistant triage: %d allowlist entries match no profile and were dropped", len(fresh))
    return sorted(out)


# ── The breaker and the cap pause (5.1 steps 3 and 5, 5.7) ──────────────────


def _breaker(now: datetime) -> str:
    """closed, open, or probe (the wait passed: one claim is allowed)."""
    with _STATE.lock:
        if _STATE.breaker_failures == 0:
            return "closed"
        if _STATE.breaker_open_until is not None and now < _STATE.breaker_open_until:
            return "open"
        return "probe"


def _breaker_trip(now: datetime) -> datetime:
    with _STATE.lock:
        _STATE.breaker_failures += 1
        wait = BREAKER_STEPS[min(_STATE.breaker_failures, len(BREAKER_STEPS)) - 1]
        _STATE.breaker_open_until = now + timedelta(seconds=wait)
        first = _STATE.breaker_failures == 1
        until = _STATE.breaker_open_until
    if first:
        logger.warning("assistant triage: model unreachable, claims paused (breaker open)")
    return until


def _breaker_close() -> None:
    with _STATE.lock:
        was_open = _STATE.breaker_failures > 0
        _STATE.breaker_failures = 0
        _STATE.breaker_open_until = None
    if was_open:
        logger.info("assistant triage: model reachable again (breaker closed)")


def _cap_paused(now: datetime) -> bool:
    with _STATE.lock:
        return _STATE.cap_pause_until is not None and now < _STATE.cap_pause_until


def _cap_pause(now: datetime, settings: Any) -> None:
    seconds = _int(settings, "assistant_triage_cap_pause_seconds", 900)
    day = now.astimezone(PT).date()
    with _STATE.lock:
        _STATE.cap_pause_until = now + timedelta(seconds=seconds)
        first_today = _STATE.cap_logged_day != day
        _STATE.cap_logged_day = day
    if first_today:
        logger.warning("assistant triage: the spend cap refused a batch; rows wait for midnight")


def health() -> dict[str, Any]:
    """This process's breaker and cap pause, for the admin health block (8.1;
    read by routers/assistant_inbox.py, the only thing a route may take from
    this module): {breaker: {open, state, next_probe_at}, cap_paused:
    {paused, until}}. `open` is true while the breaker waits or probes
    ("paused: model unreachable"); `next_probe_at` is None once a probe is due."""
    now = _clock()
    state = _breaker(now)
    with _STATE.lock:
        until = _STATE.breaker_open_until
        cap = _STATE.cap_pause_until
    paused = cap is not None and cap > now
    return {
        "breaker": {"open": state != "closed", "state": state,
                    "next_probe_at": until.isoformat() if state == "open" and until is not None else None},
        "cap_paused": {"paused": paused, "until": cap.isoformat() if paused else None},
    }


# ── Facts (5.3) ──────────────────────────────────────────────────────────────


def _missing_categories(sb: Any, project_ids: list[str]) -> dict[str, list[str]]:
    """The router's own quote-missing query, shared (5.3): the public
    `missing_quote_categories(sb, project_ids)` WI-8b adds beside the private
    one, or the private one over a minimal context until it lands (it reads
    only `ctx.sb`)."""
    shared = getattr(assistant_proactive, "missing_quote_categories", None)
    if shared is not None:
        return shared(sb, project_ids)
    return assistant_proactive._missing_quote_categories(SimpleNamespace(sb=sb), project_ids)


def _your_part_missing(sb: Any, role: str, projects: Mapping[str, Mapping[str, Any]],
                       now: datetime) -> dict[str, bool | None]:
    """Per project whose internal_bid_at falls inside the next 24 hours, whether
    a part the person's role owns is missing (a `bid.due_not_ready`-shaped
    payload from due_digest.readiness_rows and the shared missing-categories
    query, judged by proactive.role_owns_missing_part). Amounts are read by
    readiness_rows only to compute booleans; nothing else leaves here."""
    due = []
    for pid, project in projects.items():
        at = _utc(project.get("internal_bid_at"))
        if at is not None and now < at <= now + PART_MISSING_HORIZON:
            due.append({**project, "id": pid})
    if not due:
        return {}
    checks = due_digest.readiness_rows(sb, due)
    missing = _missing_categories(sb, [str(p["id"]) for p in due])
    out: dict[str, bool | None] = {}
    for item in checks:
        pid = str(item["project"]["id"])
        flags = item.get("checks") or {}
        payload = {
            "missing_quote_category_ids": missing.get(pid, []) if not flags.get("has_quotes") else [],
            "labor_missing": not flags.get("has_labor"),
            "markups_missing": not flags.get("has_markups"),
        }
        out[pid] = proactive.role_owns_missing_part(role, payload)
    return out


def _open_todos(sb: Any, user_id: str, project_ids: list[str]) -> dict[str, int]:
    """{project id: count} of this person's open to-dos on each project."""
    out: dict[str, int] = {}
    for start in range(0, len(project_ids), TODO_CHUNK):
        chunk = project_ids[start:start + TODO_CHUNK]
        resp = (sb.table("todos").select("project_id").eq("user_id", user_id).eq("is_done", False)
                .in_("project_id", chunk).limit(1000).execute())
        for row in getattr(resp, "data", None) or []:
            if isinstance(row, dict) and row.get("project_id"):
                key = str(row["project_id"])
                out[key] = out.get(key, 0) + 1
    return out


def _gather_facts(ctx: Any, user_id: str, role: str, kept: list[dict], inbox: Mapping[str, Mapping[str, Any]],
                  projects: Mapping[str, Mapping[str, Any]], now: datetime) -> dict[str, Any]:
    """Everything build_pack needs for the kept items, all keyed by id and
    read through the repo or the router's own helpers (5.9)."""
    pids = sorted({str(i["project_id"]) for i in kept if i.get("project_id")})
    bells = sorted({str(i["source_notification_id"]) for i in kept if i.get("source_notification_id")})
    notes: dict[str, dict] = {}
    if bells:
        for bid, state in repo.notification_states(user_id, bells).items():
            notes.setdefault(bid, {}).update(read_at=state.get("read_at"), dismissed_at=state.get("dismissed_at"))
        for bid, sent in repo.notification_delivery_facts(user_id, bells).items():
            notes.setdefault(bid, {}).update(teams_sent=bool(sent.get("teams_sent")),
                                             email_sent=bool(sent.get("email_sent")))
    facts: dict[str, Any] = {
        "projects": {pid: projects[pid] for pid in pids if pid in projects},
        "notifications": notes,
        "holidays": ctx.holidays,
        "quiet_start": ctx.quiet_start,
        "quiet_end": ctx.quiet_end,
    }
    if pids:
        facts["your_part_missing"] = _your_part_missing(ctx.sb, role, facts["projects"], now)
        slot = proactive.previous_slot(now, ctx.holidays, slot_times=ctx.slot_times)
        facts["router_bid_due_projects"] = repo.triage_bid_due_projects(user_id, pids, since=slot)
        facts["open_todos"] = _open_todos(ctx.sb, user_id, pids)
        facts["pending_cards"] = repo.triage_pending_cards_by_project(user_id, pids)
        facts["escalated_projects_last_2h"] = repo.triage_escalated_projects(
            user_id, pids, since=now - ESCALATED_LOOKBACK)
    return facts


# ── One batch (5.3 to 5.5) ───────────────────────────────────────────────────


def _finish(run_id: str, worker: str, now: datetime, status: str, error_kind: str | None,
            entries: list[dict], usage: Mapping[str, Any] | None = None) -> dict:
    result = repo.triage_finish(None, run_id, worker, now, status, error_kind, entries, usage)
    if not result.get("fenced"):
        logger.warning("assistant triage: a finish was fenced out (lease lost or run settled elsewhere)")
    return result


def _cap_reference(deferred_at: datetime, now: datetime) -> datetime:
    """The moment a cap-deferred row's age is judged at (owner decision 13 (b),
    P8L-2): the latest Pacific midnight at or before `now` that is after the
    first cap refusal, or `now` when no midnight has passed since it. A retry
    claimed inside its 00:00 to 02:00 stagger (plus a small grace) is judged at
    that midnight; any later claim is judged at `now` (P8L-2-R1). A same-day
    retry after the stagger (an infrastructure backoff, the breaker, a worker
    outage) therefore ages out by its real age, so no row older than about
    26 hours 15 minutes is ever triaged; before this the reference stayed at
    midnight all day and the bound was about 47 hours. A claim a day late is
    judged by the same rule against the later midnight, so it ages out sooner,
    never later."""
    local = now.astimezone(PT)
    midnight = datetime(local.year, local.month, local.day, tzinfo=PT).astimezone(UTC)
    if midnight > deferred_at and now < midnight + CAP_STAGGER + CAP_STAGGER_GRACE:
        return midnight
    return now


def _item_skips(items: Sequence[Mapping[str, Any]], inbox: Mapping[str, Mapping[str, Any]],
                projects: Mapping[str, Mapping[str, Any]], now: datetime,
                window: timedelta) -> tuple[list[dict], list[dict]]:
    """5.3 step 3: (kept items, per-item skip entries for the finish)."""
    kept: list[dict] = []
    skips: list[dict] = []
    for item in items:
        row = inbox.get(str(item.get("inbox_id")))
        if row is None or row.get("dismissed_at"):
            skips.append({"id": item["id"], "skip_reason": "dismissed"})
            continue
        received = _utc(item.get("inbox_created_at")) or _utc(row.get("created_at"))
        age = now - received if received is not None else timedelta(0)
        deferred_at = _utc(item.get("cap_deferred_at"))
        if item.get("cap_deferred_at"):
            # Judged by the 24-hour age-out only, never by the window (MC-7), and at
            # the deferral midnight, not at the claim (P8L-2): the retry is staggered up
            # to 2 hours past midnight, so a row 23 hours old at midnight is still triaged.
            if received is not None and deferred_at is not None:
                age = _cap_reference(deferred_at, now) - received
            if age >= CAP_AGE_OUT:
                skips.append({"id": item["id"], "skip_reason": "cap"})
                continue
        elif age > window:
            skips.append({"id": item["id"], "skip_reason": "stale"})
            continue
        if item.get("event_type") == DUE_KIND and item.get("project_id"):
            due = _utc((projects.get(str(item["project_id"])) or {}).get("internal_bid_at"))
            if due is not None and due <= now:
                skips.append({"id": item["id"], "skip_reason": "stale"})
                continue
        kept.append({**dict(item), "inbox_created_at": received.isoformat() if received else None})
    return kept, skips


def run_batch(claim: Mapping[str, Any], now: datetime, *, worker: str, settings: Any, ctx: Any,
              allowlist: Sequence[str] | None, clock: Callable[[], datetime] | None = None) -> _Outcome:
    """One claimed batch: the checks of 5.3, the model call and the finish.
    Never raises for a model failure: the closed set maps to the run's error
    kind. Anything unexpected before the model call releases the run as an
    infrastructure failure (no attempt counted). `now` is the claim's time;
    `clock` (the tick's) gives the time after the model call for its finish."""
    run_id = str(claim["run_id"])
    user_id = str(claim["user_id"])
    items = [dict(i) for i in claim.get("items") or ()]

    if allowlist is not None and user_id not in allowlist:
        # The claim filters by the allowlist, so this is a bug: release, never settle.
        logger.error("assistant triage: a claimed person is outside this process's allowlist; released")
        _finish(run_id, worker, now, "failed", RELEASE_ERROR_KIND, [])
        return _Outcome(OUT_RELEASED)

    skips: list[dict] = []
    try:
        person = ctx.person(user_id)
        if person is None or not ctx.eligible(person, proactive.TRIAGE_ESCALATION, {}):
            # No delegation and no owner check (owner question 15 (c)).
            repo.triage_skip_user(user_id, "ineligible", now)
            return _Outcome(OUT_INELIGIBLE)
        role = str(person.get("role"))
        inbox = repo.triage_inbox_rows(user_id, [str(i["inbox_id"]) for i in items])
        projects: dict[str, dict] = {}
        for pid in sorted({str(i["project_id"]) for i in items if i.get("project_id")}):
            row = ctx.project(pid)
            if row is not None:
                projects[pid] = dict(row)
        window = timedelta(hours=_int(settings, "assistant_triage_window_hours", 24))
        kept, skips = _item_skips(items, inbox, projects, now, window)
        if not kept:
            result = _finish(run_id, worker, now, "decided", None, skips)
            return _Outcome(OUT_SKIPPED if result.get("fenced") else OUT_FENCED)
        facts = _gather_facts(ctx, user_id, role, kept, inbox, projects, now)
        rows = [{
            "event_type": i["event_type"], "created_at": i["inbox_created_at"],
            "cap_deferred_at": i.get("cap_deferred_at"), "project_id": i.get("project_id"),
            "source_notification_id": i.get("source_notification_id"),
            "payload": (inbox.get(str(i["inbox_id"])) or {}).get("payload") or {},
        } for i in kept]
    except Exception as exc:  # noqa: BLE001 - release the run, no attempt counted
        logger.warning("assistant triage: batch checks failed (%s); run released", exc.__class__.__name__,
                       exc_info=True)
        _finish(run_id, worker, now, "failed", RELEASE_ERROR_KIND, skips)
        return _Outcome(OUT_RELEASED)

    try:
        pack = triage.build_pack(rows, facts, role, now=now)
    except (triage.PackError, triage.PackNotClean) as exc:
        # A pack that is not clean is a bug: the run fails bad_answer, nothing is sent (5.3).
        logger.error("assistant triage: the fact pack was refused (%s)", exc.__class__.__name__)
        _finish(run_id, worker, now, "failed", assistant_triage_model.TriageBadAnswer.error_kind, skips)
        return _Outcome(assistant_triage_model.TriageBadAnswer.error_kind, assistant_triage_model.TriageBadAnswer)

    keys = [it["key"] for it in pack["items"]]
    by_key = dict(zip(keys, kept, strict=True))
    answer: Any = None
    called_usage: Mapping[str, Any] | None = None
    try:
        classified = assistant_triage_model.classify(run_id, pack, settings=settings)
    except assistant_triage_model.ALL as exc:
        error = type(exc)
        called_usage = getattr(exc, "usage", None)    # set when the call reached the model
    except Exception:  # noqa: BLE001 - classify raises only the closed set; anything else is a model error
        logger.warning("assistant triage: classify raised outside its closed set", exc_info=True)
        error = assistant_triage_model.TriageModelError
    else:
        error = None
        answer = classified.answer
        called_usage = classified.usage
    done = clock() if clock is not None else now      # the call may have waited minutes
    # The model, the company hold and the tokens and cost of the call (WI7-1);
    # the setting's model name alone when classify reported none.
    usage: dict = (dict(called_usage) if called_usage
                   else {"model": getattr(settings, "assistant_triage_model", None)})
    if error is not None:
        # A cap refusal, an unpriced route or a failed spend check never reached the model.
        called = error.item_class != "cap" and error not in _NOT_CALLED
        status = "skipped" if error.item_class == "cap" else "failed"
        result = _finish(run_id, worker, done, status, error.error_kind, skips, usage if called else None)
        return _Outcome(error.error_kind if result.get("fenced") else OUT_FENCED, error, model_called=called)

    checked = triage.validate(answer, keys, {k: by_key[k]["event_type"] for k in keys}, role)
    if checked.malformed:
        result = _finish(run_id, worker, done, "failed", assistant_triage_model.TriageBadAnswer.error_kind,
                         skips, usage)
        return _Outcome(assistant_triage_model.TriageBadAnswer.error_kind if result.get("fenced") else OUT_FENCED,
                        assistant_triage_model.TriageBadAnswer, model_called=True)
    if checked.unknown_keys or checked.duplicates or checked.invalid or checked.extra_fields:
        logger.info("assistant triage: answer format: %d unknown, %d duplicate, %d invalid, %d extra",
                    checked.unknown_keys, checked.duplicates, checked.invalid, checked.extra_fields)
    entries = list(skips)
    for key, decision in checked.decisions.items():
        entries.append({"id": by_key[key]["id"], "decision": decision["decision"], "reason": decision["reason"],
                        "action": decision["action"], "severity_hint": decision["severity"]})
    # Keys the answer left out are not named: the finish returns them to pending (one retry).
    result = _finish(run_id, worker, done, "decided", None, entries, usage)
    if not result.get("fenced"):
        return _Outcome(OUT_FENCED, model_called=True)
    return _Outcome(OUT_DECIDED, emitted=int(result.get("emitted") or 0), model_called=True)


# ── The daily purge (5.1 step 6) ─────────────────────────────────────────────


def run_purge(now: datetime, *, settings: Any) -> dict[str, Any] | None:
    """Once a Pacific day after 02:00: the run purge (0196 B4) and the
    release, through spend.settle, of the company's own triage holds still
    active more than an hour after they expired (MC-1). None when not due.
    The two parts run in separate tries, so a failing purge never blocks the
    hold release (or the other way round). The day is marked done only when
    both succeed; after a failure the step waits PURGE_RETRY before trying
    again, so a persistent failure logs once an hour, not every tick."""
    local = now.astimezone(PT)
    day = local.date()
    with _STATE.lock:
        due = (local.time() >= PURGE_AFTER and _STATE.purged_day != day
               and (_STATE.purge_retry_at is None or now >= _STATE.purge_retry_at))
    if not due:
        return None
    out: dict[str, Any] = {}
    failed: list[str] = []
    try:
        out["runs"] = repo.triage_purge(None, now, run_days=_int(settings, "assistant_triage_retention_days", 90),
                                        limit=PURGE_LIMIT)
    except Exception as exc:  # noqa: BLE001 - the hold release still runs
        logger.warning("assistant triage: the run purge failed (%s); retried in an hour", exc.__class__.__name__)
        failed.append("runs")
    released = 0
    try:
        holds = repo.triage_expired_company_holds(None, limit=COMPANY_HOLD_LIMIT)
    except Exception as exc:  # noqa: BLE001 - the run purge already ran
        logger.warning("assistant triage: reading expired company holds failed (%s); retried in an hour",
                       exc.__class__.__name__)
        failed.append("company_holds")
        holds = []
    for hold in holds:
        try:
            spend.settle(spend.COMPANY_SPEND_ID, hold.get("id"))
            released += 1
        except Exception as exc:  # noqa: BLE001 - one hold never stops the others
            logger.warning("assistant triage: releasing a company hold failed (%s)", exc.__class__.__name__)
    out["company_holds_released"] = released
    with _STATE.lock:
        if failed:
            _STATE.purge_retry_at = now + PURGE_RETRY
        else:
            _STATE.purged_day = day
            _STATE.purge_retry_at = None
    if failed:
        out["failed"] = failed
    return out


# ── The tick and the loop ────────────────────────────────────────────────────


def _context(now: datetime, settings: Any, row: Mapping[str, Any]) -> Any:
    """The router's per-tick context: its eligibility test, project read,
    holidays, quiet hours and slot times, shared rather than copied."""
    return assistant_proactive._Context(now, settings=settings, settings_row=row,
                                        sb=supabase_client.get_supabase())


def _claims(out: dict[str, Any], clock: Callable[[], datetime], worker: str, settings: Any, ctx: Any,
            allowlist: list[str] | None, limit: int) -> None:
    """Up to `limit` claims, one at a time. `clock` is the tick's time,
    read afresh for every claim, finish, breaker trip and cap pause."""
    lease = _int(settings, "assistant_triage_lease_seconds", 420)
    debounce = _int(settings, "assistant_triage_debounce_seconds", 300)
    batch_max = _int(settings, "assistant_triage_batch_max", 10)
    ratio = _float(settings, "assistant_triage_fuse_ratio", 0.5)
    per_hour = _int(settings, "assistant_triage_fuse_per_hour", 30)
    counts = out.setdefault("batches", {})
    for _ in range(max(0, limit)):
        at = clock()
        claim = repo.triage_claim(None, worker, at, lease, debounce, batch_max, ratio, per_hour,
                                  user_ids=allowlist)
        if not claim:
            return
        if claim.get("fuse"):
            out["fuse"] = True
            if claim.get("tripped_now"):
                with _STATE.lock:
                    _STATE.fuse_logged = True
                logger.warning("assistant triage: the company fuse tripped; triage paused for everyone "
                               "until an admin turns the company switch off and on")
            return
        if claim.get("switched_off"):
            out["switched_off"] = True
            return
        outcome = run_batch(claim, at, worker=worker, settings=settings, ctx=ctx, allowlist=allowlist,
                            clock=clock)
        counts[outcome.kind] = counts.get(outcome.kind, 0) + 1
        out["emitted"] = out.get("emitted", 0) + outcome.emitted
        error = outcome.error
        if error is None:
            if outcome.model_called:
                _breaker_close()
            continue
        if issubclass(error, assistant_triage_model.BREAKER):
            out["breaker_open_until"] = _breaker_trip(clock()).isoformat()   # from the failure, not the tick start
            return
        if error.item_class == "cap":
            _cap_pause(clock(), settings)
            out["cap_paused"] = True
            return
        if error.item_class == "infrastructure":
            # Unpriced or a failed spend check: every batch would fail the same way this tick.
            return
        # Answer-quality failures (bad_answer, truncated): the model answered, so it is reachable.
        _breaker_close()


def poll_once(now: datetime | None = None, *, worker: str | None = None) -> dict[str, Any]:
    """One triage tick (5.1). `now` defaults to the current time (the tick
    script travels it); `worker` defaults to WORKER_ID. Plain def: it touches
    the sync Supabase SDK. Returns counts only."""
    tick_real = _clock()
    now = _utc(now) if now is not None else tick_real

    def clock() -> datetime:
        # The tick's time plus the real time spent since it started.
        return now + (_clock() - tick_real)

    worker = worker or WORKER_ID
    settings = get_settings()
    if not _env_on(settings):
        return {"skipped": "env_off"}
    out: dict[str, Any] = {}
    row = _step(out, "settings", repo.settings_row, None)
    if "settings" in out.get("errors", ()):
        return out
    out.pop("settings", None)
    if not _switch_on(row):
        out["skipped"] = "switched_off"
        _step(out, "skip_all", repo.triage_skip_all, None, "switched_off", now, limit=SKIP_ALL_LIMIT)
        _step(out, "purge", run_purge, now, settings=settings)
        return out
    if (row or {}).get("triage_fuse_tripped_at"):
        out["skipped"] = "fuse"
        with _STATE.lock:
            first = not _STATE.fuse_logged
            _STATE.fuse_logged = True
        if first:
            logger.warning("assistant triage: the company fuse is latched; triage paused for everyone")
        _step(out, "skip_all", repo.triage_skip_all, None, "fuse", now, limit=SKIP_ALL_LIMIT)
        _step(out, "purge", run_purge, now, settings=settings)
        return out
    with _STATE.lock:
        _STATE.fuse_logged = False
    state = _breaker(now)
    if state == "open":
        out["skipped"] = "breaker"
        _step(out, "purge", run_purge, now, settings=settings)
        return out
    ctx = _step(out, "context", _context, now, settings, row)
    out.pop("context", None)
    if ctx is None:
        return out
    allowlist = None
    if str(getattr(settings, "assistant_triage_user_allowlist", "") or "").strip(" ,"):
        allowlist = _step(out, "allowlist", lambda: resolve_allowlist(settings, ctx.profiles()))
        out.pop("allowlist", None)
        if allowlist is None:
            return out    # an unreadable allowlist never widens to everyone
    window = _int(settings, "assistant_triage_window_hours", 24)
    _step(out, "enqueued", repo.triage_enqueue, None, now, window, limit=ENQUEUE_LIMIT, user_ids=allowlist)
    if _cap_paused(now):
        out["cap_paused"] = True
    else:
        limit = 1 if state == "probe" else _int(settings, "assistant_triage_batches_per_tick", 5)
        _step(out, "claims", _claims, out, clock, worker, settings, ctx, allowlist, limit)
        out.pop("claims", None)
    _step(out, "purge", run_purge, clock(), settings=settings)
    return out


async def polling_loop() -> None:
    """while True: await asyncio.to_thread(poll_once), log any exception,
    then sleep ASSISTANT_TRIAGE_POLL_SECONDS (the router's guarded shape).
    Returns at once when ASSISTANT_TRIAGE_LOOP_ENABLED is false (the routes
    are served without the loop, 16.5). Cancellation stops the sleep."""
    settings = get_settings()
    if not getattr(settings, "assistant_triage_loop_enabled", True):
        logger.info("assistant triage: loop disabled by ASSISTANT_TRIAGE_LOOP_ENABLED")
        return
    interval = _int(settings, "assistant_triage_poll_seconds", 60) or 60
    while True:
        try:
            await asyncio.to_thread(poll_once)
        except Exception:  # noqa: BLE001 - the loop must survive any tick failure
            logger.exception("Assistant triage tick failed")
        await asyncio.sleep(interval)


__all__ = ("WORKER_ID", "health", "poll_once", "polling_loop", "reset_state", "resolve_allowlist",
           "run_batch", "run_purge")

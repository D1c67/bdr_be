"""Phase 7 claim check on the dev database (docs/ASSISTANT_PHASE7_PLAN.md 11.3, 11.5 step 10).

    NOTIFICATION_EMAILS_ENABLED=false TEAMS_ALERTS_ENABLED=false \\
      .venv/bin/python scripts/check_proactive_claims.py --user-ids <id>[,<id>] [--now <ISO>] [--iterations 20]

Two separate processes (multiprocessing, spawn, one Supabase client each) call
poll_once(now, worker=<distinct id>, tails=False) --iterations times each, in
lockstep, against rows this script seeds for one or two allowlisted users:
per user two todo.due events (and their to-dos), which become digest items,
and one due immediate high inbox row, which rings once and gets a Teams
ledger row. It then asserts, from the database:

  - one inbox row per (user, dedupe_key), exactly the expected rows;
  - one digest ledger row and one digest bell per (user, date, slot), and one
    router bell per delivered immediate row;
  - one assistant_external_sends row per (target, channel), each settled
    skipped channel_off (the children run with ASSISTANT_PROACTIVE_EXTERNAL_ENABLED=true
    and both global channel switches off, so this proves claims, never a send);
  - every seeded event processed once (attempts 1);
  - the digest slot finished once, the digest claimed once across all polls,
    and assistant_router_cursors untouched (tails=False).

--now defaults to the latest workday 07:05 PT at or before the real now and
must lie in an open digest slot (a workday, 07:00 to 10:00 or 13:00 to 16:00
PT) within 30 days before and 8 days after the real now (every poll runs the
retention purge at that clock). The users must have no digest for that slot,
no pending router rows, no open to-dos due that day and no todo.due
subscription row that changes the code default (digest); the slot must have no
assistant_proactive_slots row yet; and no assistant_events row may be pending
or claimed under an expired lease at --now, so the workers claim only what
this run creates.

Dev only: refuses unless SUPABASE_URL is the dev project bpidntbyvoooqvaispup,
and refuses while NOTIFICATION_EMAILS_ENABLED or TEAMS_ALERTS_ENABLED is on
in the caller's env. The proactive loop on the dev server must be off (11.5
step 9), or it competes for the same rows. Every row created is removed by id
in a finally block (events, inbox rows, digests, slots, sends, the
assistant.inbox and assistant.digest bells and the seeded to-dos). Exit 0 when
every check passes, 1 on a failed check, 2 on a refusal. No model call, no
real send.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import re
import secrets
import sys
import threading
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.assistant_proactive_tick import (  # noqa: E402
    PT,
    UTC,
    Refused,
    parse_when,
    require_dev_ref,
    require_test_email,
    require_travel_window,
)

ITERATIONS = 20
TODOS_PER_USER = 2
BELL_TYPES = ("assistant.inbox", "assistant.digest")
HIGH_TITLE = "Phase 7 claim check item (test)"
TODO_TITLE = "Phase 7 claim check (test)"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
DELETE_CHUNK = 100
PAGE = 1000
# The env every child process runs under: the router and its external step on
# so ledger rows are written, both global channel switches off so every send
# settles channel_off, the RFP test bench off so no send is held, and no
# live-test recipient override.
CHILD_ENV = {
    "ASSISTANT_ENABLED": "true",
    "ASSISTANT_ALLOW_OPENAI_IN_SELF_HOSTED": "true",
    "ASSISTANT_PROACTIVE_ENABLED": "true",
    "ASSISTANT_PROACTIVE_EXTERNAL_ENABLED": "true",
    "NOTIFICATION_EMAILS_ENABLED": "false",
    "TEAMS_ALERTS_ENABLED": "false",
    "RFP_TESTING_ENABLED": "false",
    "ASSISTANT_PROACTIVE_TEST_EMAIL_TO": "",
}


# ── Guards and arguments ─────────────────────────────────────────────────────


def _real_now() -> datetime:
    """The wall clock (tests pin it)."""
    return datetime.now(UTC)


def require_channels_off(settings: Any) -> None:
    on = [name for name, flag in (("NOTIFICATION_EMAILS_ENABLED", getattr(settings, "notification_emails_enabled", False)),
                                  ("TEAMS_ALERTS_ENABLED", getattr(settings, "teams_alerts_enabled", False))) if flag]
    if on:
        raise Refused(f"{' and '.join(on)} must be false on the command line. This script proves claims with "
                      "every send settling channel_off and must never be able to send.")


def check_guards(settings: Any) -> None:
    require_dev_ref(settings)
    require_test_email(settings)
    require_channels_off(settings)


def parse_user_ids(value: str) -> list[str]:
    ids = [p.strip().lower() for p in (value or "").split(",") if p.strip()]
    if not 1 <= len(ids) <= 2 or len(set(ids)) != len(ids) or not all(UUID_RE.match(i) for i in ids):
        raise ValueError("--user-ids takes one or two distinct user ids (uuids), comma separated")
    return ids


def _user_ids(value: str) -> list[str]:
    try:
        return parse_user_ids(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _when(value: str) -> datetime:
    try:
        return parse_when(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_proactive_claims.py", description="Two-process claim check of the proactive router (dev only).")
    parser.add_argument("--user-ids", required=True, type=_user_ids, help="one or two allowlisted user ids")
    parser.add_argument("--now", type=_when, help="tick time inside an open digest slot; naive means Pacific")
    parser.add_argument("--iterations", type=int, default=ITERATIONS, help="polls per process (default 20)")
    return parser


# ── Slot arithmetic ──────────────────────────────────────────────────────────


def slot_setup(settings: Any, holidays: list[Any] | None) -> dict[str, Any]:
    from app.assistant import proactive
    from app.services import assistant_proactive as svc

    return {"slot_times": svc._slot_times(settings), "holidays": list(holidays or []),
            "catchup": int(getattr(settings, "assistant_proactive_digest_catchup_hours", 3) or 3), "proactive": proactive}


def open_slot(now: datetime, setup: dict[str, Any]) -> tuple[str, datetime, date] | None:
    """(slot, slot instant in UTC, Pacific date) of the digest slot open at
    `now`, else None (the router's own rule, assistant_proactive._open_slots)."""
    proactive = setup["proactive"]
    day = now.astimezone(PT).date()
    if not proactive.is_workday(day, setup["holidays"]):
        return None
    for slot in proactive.SLOT_ORDER:
        at = proactive.slot_instant(day, slot, slot_times=setup["slot_times"]).astimezone(UTC)
        if at <= now < at + timedelta(hours=setup["catchup"]):
            return slot, at, day
    return None


def default_now(real_now: datetime, setup: dict[str, Any]) -> datetime:
    """07:05 PT of the latest workday whose slot has already opened."""
    proactive = setup["proactive"]
    day = real_now.astimezone(PT).date()
    for _ in range(14):
        if proactive.is_workday(day, setup["holidays"]):
            at = proactive.slot_instant(day, "am", slot_times=setup["slot_times"]).astimezone(UTC) + timedelta(minutes=5)
            if at <= real_now:
                return at
        day -= timedelta(days=1)
    raise Refused("No recent workday found for --now. Pass --now.")


# ── Database reads and the run record ────────────────────────────────────────


def _rows(resp: Any) -> list[dict]:
    return [r for r in (getattr(resp, "data", None) or []) if isinstance(r, dict)]


def _select(client: Any, table: str, users: list[str], types: tuple[str, ...] | None = None) -> list[dict]:
    """Every row of `table` for these users (optionally of these notification
    types), paged past PostgREST's row cap."""
    out: list[dict] = []
    lo = 0
    while True:
        query = client.table(table).select("*").in_("user_id", users)
        if types:
            query = query.in_("type", list(types))
        rows = _rows(query.order("id").range(lo, lo + PAGE - 1).execute())
        out.extend(rows)
        if len(rows) < PAGE:
            return out
        lo += PAGE


@dataclass
class Run:
    run_id: str
    users: list[str]
    now: datetime
    day: date
    slot: str
    slot_at: datetime
    workers: list[str]
    todo_ids: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    inbox_ids: list[str] = field(default_factory=list)
    high_keys: dict[str, str] = field(default_factory=dict)       # user -> dedupe_key of the immediate row
    high_todos: dict[str, str] = field(default_factory=dict)      # user -> its to-do id
    before: dict[str, Any] = field(default_factory=dict)

    def record(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "workers": self.workers, "todos": self.todo_ids, "events": self.event_ids,
                "inbox": self.inbox_ids}


def _utc_at(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        at = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (at if at.tzinfo else at.replace(tzinfo=UTC)).astimezone(UTC)


def _unprocessed_events(client: Any) -> list[dict]:
    """Every assistant_events row not yet processed (any user), paged."""
    out: list[dict] = []
    lo = 0
    while True:
        rows = _rows(client.table("assistant_events").select("*").is_("processed_at", "null").order("id")
                     .range(lo, lo + PAGE - 1).execute())
        out.extend(rows)
        if len(rows) < PAGE:
            return out
        lo += PAGE


def snapshot(client: Any, users: list[str], day: date) -> dict[str, Any]:
    """The ids and keys that exist before the run, so everything else the
    router creates for these users is this run's. Unprocessed events are the
    only existing events a worker could touch, so their ids are kept out of
    the cleanup."""
    return {
        "events": {str(r["id"]) for r in _unprocessed_events(client)},
        "inbox": {str(r["id"]) for r in _select(client, "assistant_inbox", users)},
        "digests": {str(r["id"]) for r in _select(client, "assistant_digests", users)},
        "sends": {str(r["id"]) for r in _select(client, "assistant_external_sends", users)},
        "bells": {str(r["id"]) for r in _select(client, "notifications", users, BELL_TYPES)},
        "slots": {(str(r["step"]), str(r["slot_date"])[:10])
                  for r in _rows(client.table("assistant_proactive_slots").select("*").eq("slot_date", day.isoformat())
                                 .execute())},
        "cursors": _cursors(client),
    }


def _cursors(client: Any) -> list[tuple]:
    rows = _rows(client.table("assistant_router_cursors").select("*").execute())
    return sorted((str(r["name"]), str(r.get("cursor_at")), str(r.get("cursor_id")), str(r.get("updated_at")))
                  for r in rows)


def _routing(sub: dict[str, Any]) -> tuple:
    """(enabled, severity, delivery) of an effective todo.due subscription."""
    from app.assistant import proactive

    return sub["enabled"], sub["severity"], proactive.delivery_for(proactive.TODO_DUE, sub["severity"])


def preflight(client: Any, users: list[str], day: date, slot: str, now: datetime) -> list[str]:
    """Read-only checks that the run can be told apart from what is there."""
    from app.assistant import proactive
    from app.core.roles import INTERNAL_ROLES, Role
    from app.services import assistant_proactive as svc

    problems: list[str] = []
    settings_rows = _rows(client.table("assistant_settings").select("*").eq("id", 1).limit(1).execute())
    if not (settings_rows and settings_rows[0].get("enabled") and settings_rows[0].get("proactive_enabled")):
        problems.append("the company switch is off (assistant_settings.enabled and proactive_enabled)")
    people = {str(r["id"]): r for r in _rows(client.table("profiles").select("*").in_("id", users).execute())}
    internal = {r.value for r in INTERNAL_ROLES if r != Role.ESTIMATOR}
    for user in users:
        person = people.get(user)
        if person is None or not person.get("is_active") or person.get("role") not in internal:
            problems.append(f"user {user} is not an active internal profile")
    for row in _select(client, "assistant_digests", users):
        if str(row.get("digest_date"))[:10] == day.isoformat() and row.get("slot") == slot:
            problems.append(f"user {row['user_id']} already has the {day.isoformat()} {slot} digest")
    for row in _select(client, "assistant_inbox", users):
        if (row.get("delivery") in ("immediate", "digest") and row.get("delivered_at") is None
                and row.get("dismissed_at") is None):
            problems.append(f"user {row['user_id']} has a pending router inbox row")
    for row in _select(client, "todos", users):
        if row.get("is_done"):
            continue
        try:
            due = date.fromisoformat(str(row.get("due_date"))[:10])
        except ValueError:
            continue
        if day - timedelta(days=7) <= due <= day:
            problems.append(f"user {row['user_id']} has an open to-do due {due.isoformat()}")
    # evaluate() expects the code default for todo.due (digest): a user row,
    # an admin role row or a feedback downgrade that changes it fails a
    # correct run, so the operator clears it or runs after the step 12 restore.
    subs = _rows(client.table("assistant_subscriptions").select("*").eq("event_type", proactive.TODO_DUE).execute())
    for user in users:
        person = people.get(user)
        if person is None:
            continue
        role = str(person.get("role") or "")
        user_row = next((r for r in subs if str(r.get("user_id")) == user and not r.get("role")), None)
        role_row = next((r for r in subs if r.get("role") == role and not r.get("user_id")), None)
        base = proactive.effective_subscription(proactive.TODO_DUE, role)
        got = proactive.effective_subscription(proactive.TODO_DUE, role, user_row, role_row)
        if _routing(got) != _routing(base):
            named = [f"{kind} row {r.get('id')} (source {r.get('source')})"
                     for kind, r in (("user", user_row), ("role", role_row)) if r is not None]
            problems.append(f"user {user} has todo.due {proactive.mode_for_severity(got['severity'], got['enabled'])}, "
                            f"not the code default, from assistant_subscriptions {', '.join(named)}")
    step = f"digest_{slot}"
    if _rows(client.table("assistant_proactive_slots").select("*").eq("step", step)
             .eq("slot_date", day.isoformat()).execute()):
        problems.append(f"assistant_proactive_slots already has ({step}, {day.isoformat()}); pass a --now on another "
                        "workday or clear that row as part of the test's recorded cleanup")
    lease_floor = now - timedelta(seconds=svc.EVENT_LEASE_SECONDS)
    busy = [r for r in _unprocessed_events(client)
            if r.get("status") == "pending"
            or (r.get("status") == "claimed" and (_utc_at(r.get("claimed_at")) or now) < lease_floor)]
    if busy:
        shown = ", ".join(f"{r.get('id')} ({r.get('status')})" for r in busy[:5])
        more = f" and {len(busy) - 5} more" if len(busy) > 5 else ""
        problems.append(f"{len(busy)} assistant_events row(s) are pending or claimed under an expired lease at --now, "
                        f"so the workers would claim them: {shown}{more}")
    return problems


# ── Seeding ──────────────────────────────────────────────────────────────────


def seed(client: Any, run: Run, log: Callable[[str], None] = print) -> None:
    """Per user: two to-dos due on the run's day with a pending todo.due event
    each (digest items), and one to-do due tomorrow behind a due immediate
    high inbox row. Every id is recorded before the insert that can create it."""
    from app.assistant import proactive, repo

    for user in run.users:
        for _ in range(TODOS_PER_USER):
            todo = str(uuid.uuid4())
            run.todo_ids.append(todo)
            client.table("todos").insert(
                {"id": todo, "user_id": user, "title": TODO_TITLE, "due_date": run.day.isoformat(), "is_done": False}
            ).execute()
            payload = proactive.clean_payload(
                proactive.TODO_DUE, {"todo_id": todo, "due": run.day.isoformat(), "overdue": False})
            key = proactive.event_dedupe_key(proactive.TODO_DUE, payload)
            inserted = repo.insert_events(None, [{
                "event_type": proactive.TODO_DUE, "dedupe_key": key, "project_id": None, "subject_user_id": user,
                "payload": payload, "occurred_at": run.slot_at, "source": "emit", "status": "pending",
            }])
            run.event_ids.extend(str(r["id"]) for r in inserted)
        high = str(uuid.uuid4())
        run.todo_ids.append(high)
        run.high_todos[user] = high
        client.table("todos").insert(
            {"id": high, "user_id": user, "title": TODO_TITLE, "due_date": (run.day + timedelta(days=1)).isoformat(),
             "is_done": False}
        ).execute()
        key = f"p7:claims:{run.run_id}:{user}"
        run.high_keys[user] = key
        due = run.now - timedelta(minutes=1)
        made = repo.insert_inbox_row(user, {
            "dedupe_key": key, "event_id": None, "severity": "high", "title": HIGH_TITLE,
            "why": "A seeded row for the claim check.", "project_id": None, "event_type": proactive.TODO_DUE,
            "payload": proactive.clean_payload(proactive.TODO_DUE, {
                "todo_id": high, "due": (run.day + timedelta(days=1)).isoformat(), "overdue": False}),
            "delivery": "immediate", "precedence": None, "deliver_after": due, "created_at": due,
        }, now=due)
        if made and made.get("id"):
            run.inbox_ids.append(str(made["id"]))
    log("SEEDED " + json.dumps(run.record(), sort_keys=True))


# ── The two workers ──────────────────────────────────────────────────────────


def child_env(users: list[str], environ: dict[str, str] | None = None) -> dict[str, str]:
    """The env a worker process runs under (CHILD_ENV plus the allowlist and a
    token secret that is never printed)."""
    env = dict(CHILD_ENV)
    env["ASSISTANT_PROACTIVE_USER_ALLOWLIST"] = ",".join(users)
    have = (environ if environ is not None else os.environ).get("ASSISTANT_TOKEN_SECRET", "")
    env["ASSISTANT_TOKEN_SECRET"] = have if len(have) >= 32 else secrets.token_urlsafe(48)
    return env


def worker_main(worker: str, now_iso: str, iterations: int, users: list[str], barrier: Any, queue: Any) -> None:
    """One worker process: set the env, re-check the guards on the settings it
    will really run with, then poll in lockstep with the other worker."""
    out: dict[str, Any] = {"worker": worker, "polls": [], "error": None}
    try:
        os.environ.update(child_env(users))
        from app.core import supabase_client
        from app.core.config import get_settings

        get_settings.cache_clear()
        supabase_client.get_supabase.cache_clear()
        settings = get_settings()
        check_guards(settings)
        from app.services import assistant_proactive

        now = parse_when(now_iso)
        for _ in range(iterations):
            try:
                barrier.wait(60)
            except threading.BrokenBarrierError:
                pass
            out["polls"].append(assistant_proactive.poll_once(now, worker=worker, tails=False))
    except Refused as exc:
        out["error"] = f"Refused: {exc}"
    except BaseException as exc:  # noqa: BLE001 - reported to the parent; the text may hold values, so class only
        out["error"] = exc.__class__.__name__
    queue.put(out)


def spawn_runner(workers: list[str], now: datetime, iterations: int, users: list[str]) -> list[dict]:
    """Run one process per worker id under the spawn start method."""
    mp = multiprocessing.get_context("spawn")
    barrier, queue = mp.Barrier(len(workers)), mp.Queue()
    procs = [mp.Process(target=worker_main, args=(w, now.isoformat(), iterations, users, barrier, queue))
             for w in workers]
    for proc in procs:
        proc.start()
    results: list[dict] = []
    try:
        for _ in procs:
            results.append(queue.get(timeout=max(120, iterations * 60)))
    except Exception:  # noqa: BLE001 - a worker that never reported is a failed check below
        pass
    finally:
        for proc in procs:
            proc.join(timeout=30)
            if proc.is_alive():
                proc.terminate()
    return results


# ── Assertions ───────────────────────────────────────────────────────────────


def _run_event(row: dict, run: Run) -> bool:
    """An event the run's router created: a sweeper or approval-scan row
    (source sweeper, occurred_at the slot instant or the run's now) that did
    not exist unprocessed before the run."""
    if str(row["id"]) in run.before.get("events", set()) or row.get("source") != "sweeper":
        return False
    return _utc_at(row.get("occurred_at")) in (_utc_at(run.slot_at), _utc_at(run.now))


def collect_created(client: Any, run: Run) -> dict[str, list]:
    """Everything the router wrote for these users since the snapshot, plus
    the slots claimed under this run's worker ids and the events it seeded or
    created: those claimed under its worker ids, and those a retry handed back
    (claimed_by null, still unprocessed)."""
    before = run.before
    users = run.users
    workers = run.workers
    inbox = [r for r in _select(client, "assistant_inbox", users) if str(r["id"]) not in before["inbox"]]
    digests = [r for r in _select(client, "assistant_digests", users) if str(r["id"]) not in before["digests"]]
    sends = [r for r in _select(client, "assistant_external_sends", users) if str(r["id"]) not in before["sends"]]
    bells = [r for r in _select(client, "notifications", users, BELL_TYPES) if str(r["id"]) not in before["bells"]]
    slots = [r for r in _rows(client.table("assistant_proactive_slots").select("*")
                              .eq("slot_date", run.day.isoformat()).execute())
             if (str(r["step"]), str(r["slot_date"])[:10]) not in before["slots"] and r.get("claimed_by") in workers]
    events = {str(r["id"]): r for r in _rows(client.table("assistant_events").select("*").in_("id", run.event_ids)
                                            .execute())} if run.event_ids else {}
    for r in _rows(client.table("assistant_events").select("*").in_("claimed_by", workers).execute()):
        if _run_event(r, run):
            events.setdefault(str(r["id"]), r)
    for r in _unprocessed_events(client):
        if r.get("claimed_by") is None and _run_event(r, run):
            events.setdefault(str(r["id"]), r)
    return {"inbox": inbox, "digests": digests, "sends": sends, "bells": bells, "slots": slots,
            "events": list(events.values())}


def _dup(rows: list[dict], *cols: str) -> list[tuple]:
    counts = Counter(tuple(str(r.get(c)) for c in cols) for r in rows)
    return [k for k, n in counts.items() if n > 1]


def evaluate(client: Any, run: Run, results: list[dict]) -> dict[str, list[str]]:
    """Check name -> failure messages (empty means the check passed)."""
    made = collect_created(client, run)
    users = run.users
    checks: dict[str, list[str]] = {}

    seeded = {r["id"]: r for r in made["events"] if str(r["id"]) in set(run.event_ids)}
    bad = [f"event {eid} is {e.get('status')} after {e.get('attempts')} attempt(s)" for eid, e in seeded.items()
           if not (e.get("status") == "processed" and e.get("attempts") == 1 and e.get("processed_at"))]
    if len(seeded) != len(run.event_ids):
        bad.append(f"{len(run.event_ids) - len(seeded)} seeded event(s) vanished")
    checks["every seeded event finished once"] = bad

    inbox, digests, bells, sends = made["inbox"], made["digests"], made["bells"], made["sends"]
    bad = [f"inbox rows repeat (user, key) {k}" for k in _dup(inbox, "user_id", "dedupe_key")]
    for user in users:
        mine = [r for r in inbox if str(r["user_id"]) == user]
        high = [r for r in mine if r.get("dedupe_key") == run.high_keys[user]]
        listed = [r for r in mine if r.get("delivery") == "digest"]
        if len(high) != 1 or high[0].get("delivered_at") is None or not high[0].get("notification_id"):
            bad.append(f"user {user}: the immediate row was not delivered exactly once")
        if len(listed) != TODOS_PER_USER or any(not r.get("digest_id") for r in listed):
            bad.append(f"user {user}: expected {TODOS_PER_USER} digest rows attached to a digest, found {len(listed)}")
        if len(mine) != 1 + TODOS_PER_USER:
            bad.append(f"user {user}: expected {1 + TODOS_PER_USER} new inbox rows, found {len(mine)}")
    checks["one inbox row per (user, key)"] = bad

    bad = [f"digest ledger repeats (user, date, slot) {k}" for k in _dup(digests, "user_id", "digest_date", "slot")]
    for user in users:
        mine = [d for d in digests if str(d["user_id"]) == user]
        if len(mine) != 1:
            bad.append(f"user {user}: expected one digest, found {len(mine)}")
            continue
        d = mine[0]
        if str(d.get("digest_date"))[:10] != run.day.isoformat() or d.get("slot") != run.slot:
            bad.append(f"user {user}: the digest is for the wrong date or slot")
        if d.get("item_count") != TODOS_PER_USER:
            bad.append(f"user {user}: the digest lists {d.get('item_count')} item(s), expected {TODOS_PER_USER}")
        if d.get("claimed_by") not in run.workers:
            bad.append(f"user {user}: the digest was claimed by an unknown worker")
    checks["one digest ledger row per (user, date, slot)"] = bad

    bad = [f"bell ids repeat {k}" for k in _dup(bells, "id")]
    bell_ids = {str(b["id"]) for b in bells}
    for user in users:
        mine = [b for b in bells if str(b["user_id"]) == user]
        kinds = Counter(b.get("type") for b in mine)
        if kinds.get("assistant.digest", 0) != 1 or kinds.get("assistant.inbox", 0) != 1:
            bad.append(f"user {user}: expected one digest bell and one inbox bell, found {dict(kinds)}")
    for d in digests:
        if str(d.get("notification_id")) not in bell_ids:
            bad.append("a digest does not point at a bell this run created")
    for r in inbox:
        if r.get("dedupe_key") in run.high_keys.values() and str(r.get("notification_id")) not in bell_ids:
            bad.append("an immediate row does not point at a bell this run created")
    checks["one bell per (user, date, slot) and per immediate row"] = bad

    bad = [f"send ledger repeats (target, channel) {k}" for k in _dup(sends, "target_kind", "target_id", "channel")]
    teams_off = {str(r["user_id"]) for r in _rows(client.table("teams_alert_users").select("user_id, enabled")
                                                  .in_("user_id", users).execute()) if r.get("enabled") is False}
    expected = {("digest", str(d["id"]), "email") for d in digests}
    for r in inbox:
        if r.get("dedupe_key") in run.high_keys.values() and str(r["user_id"]) not in teams_off:
            expected.add(("inbox", str(r["id"]), "teams"))
    got = {(str(s["target_kind"]), str(s["target_id"]), str(s["channel"])) for s in sends}
    if got != expected:
        bad.append(f"send ledger rows differ from the expected targets: {len(got)} found, {len(expected)} expected")
    for s in sends:
        if not (s.get("status") == "skipped" and s.get("error_kind") == "channel_off" and s.get("sent_at") is None
                and s.get("email_log_id") is None):
            bad.append(f"a send settled {s.get('status')}/{s.get('error_kind')}, not skipped/channel_off")
    checks["one send ledger row per (target, channel), none sent"] = bad

    step = f"digest_{run.slot}"
    mine = [s for s in made["slots"] if s.get("step") == step]
    bad = []
    if len(mine) != 1 or not mine[0].get("finished_at"):
        bad.append(f"the {step} slot was not created and finished exactly once")
    checks["the digest slot finished once"] = bad

    claimed = sum(int((p.get("digests") or {}).get("digests") or 0) for r in results for p in r.get("polls", []))
    bad = [] if claimed == len(users) else [f"digests claimed {claimed} time(s) across all polls, expected {len(users)}"]
    checks["each digest claimed once across all polls"] = bad

    bad = [f"worker {r.get('worker')} failed: {r['error']}" for r in results if r.get("error")]
    if len(results) != len(run.workers):
        bad.append(f"{len(run.workers) - len(results)} worker(s) never reported")
    for r in results:
        if any(p.get("skipped") for p in r.get("polls", [])):
            bad.append(f"worker {r.get('worker')} skipped a poll: {[p['skipped'] for p in r['polls'] if p.get('skipped')][0]}")
    checks["both workers polled"] = bad

    after = _cursors(client)
    checks["tails=False left the cursors alone"] = [] if after == run.before["cursors"] else ["a cursor row changed"]
    return checks


# ── Cleanup ──────────────────────────────────────────────────────────────────


def _delete_ids(client: Any, table: str, ids: list[str], column: str = "id") -> int:
    done = 0
    for start in range(0, len(ids), DELETE_CHUNK):
        chunk = ids[start:start + DELETE_CHUNK]
        client.table(table).delete().in_(column, chunk).execute()
        done += len(chunk)
    return done


def _delete_slots(client: Any, slots: list[dict]) -> int:
    for s in slots:
        client.table("assistant_proactive_slots").delete().eq("step", s["step"]).eq(
            "slot_date", str(s["slot_date"])[:10]).execute()
    return len(slots)


def cleanup(client: Any, run: Run) -> dict[str, Any]:
    """Delete this run's rows by id, children first. Each table is its own
    try so one failure never strands the rest; the result lists the counts and
    any table that failed."""
    out: dict[str, Any] = {"deleted": {}, "failed": []}
    try:
        made = collect_created(client, run)
    except Exception as exc:  # noqa: BLE001
        out["failed"].append(f"collect:{exc.__class__.__name__}")
        made = {"inbox": [], "digests": [], "sends": [], "bells": [], "slots": [], "events": []}
    plan: list[tuple[str, Callable[[], int]]] = [
        ("assistant_external_sends", lambda: _delete_ids(client, "assistant_external_sends",
                                                         [str(r["id"]) for r in made["sends"]])),
        ("notifications", lambda: _delete_ids(client, "notifications", [str(r["id"]) for r in made["bells"]])),
        ("assistant_inbox", lambda: _delete_ids(
            client, "assistant_inbox", sorted({str(r["id"]) for r in made["inbox"]} | set(run.inbox_ids)))),
        ("assistant_digests", lambda: _delete_ids(client, "assistant_digests", [str(r["id"]) for r in made["digests"]])),
        ("assistant_events", lambda: _delete_ids(
            client, "assistant_events", sorted({str(r["id"]) for r in made["events"]} | set(run.event_ids)))),
        ("assistant_proactive_slots", lambda: _delete_slots(client, made["slots"])),
        ("todos", lambda: _delete_ids(client, "todos", run.todo_ids)),
    ]
    for table, fn in plan:
        try:
            out["deleted"][table] = fn()
        except Exception as exc:  # noqa: BLE001
            out["failed"].append(f"{table}:{exc.__class__.__name__}")
    return out


# ── The check ────────────────────────────────────────────────────────────────

Runner = Callable[[list[str], datetime, int, list[str]], list[dict]]


def run_check(client: Any, settings: Any, users: list[str], *, runner: Runner, now: datetime | None = None,
              iterations: int = ITERATIONS, real_now: datetime | None = None,
              log: Callable[[str], None] = print) -> tuple[dict[str, list[str]], dict[str, Any]]:
    """Preflight, seed, run the two workers, assert, clean up in a finally.
    Raises Refused before anything is written. Returns (checks, summary)."""
    rows = _rows(client.table("assistant_settings").select("*").eq("id", 1).limit(1).execute())
    setup = slot_setup(settings, (rows[0].get("holidays") if rows else None))
    clock = real_now or _real_now()
    when = now or default_now(clock, setup)
    require_travel_window(when, clock)
    found = open_slot(when, setup)
    if found is None:
        raise Refused(f"--now {when.astimezone(PT).isoformat()} is not inside an open digest slot "
                      "(a workday, 07:00 to 10:00 or 13:00 to 16:00 Pacific).")
    slot, slot_at, day = found
    problems = preflight(client, users, day, slot, when)
    if problems:
        raise Refused("preflight failed: " + "; ".join(problems))
    run_id = uuid.uuid4().hex[:8]
    run = Run(run_id=run_id, users=users, now=when, day=day, slot=slot, slot_at=slot_at,
              workers=[f"claims-{run_id}-a", f"claims-{run_id}-b"])
    run.before = snapshot(client, users, day)
    summary: dict[str, Any] = {"run_id": run_id, "now": when.isoformat(), "now_pacific": when.astimezone(PT).isoformat(),
                               "slot": slot, "users": users, "workers": run.workers, "iterations": iterations}
    checks: dict[str, list[str]] = {}
    try:
        seed(client, run, log)
        results = runner(run.workers, when, iterations, users)
        summary["polls"] = sum(len(r.get("polls", [])) for r in results)
        summary["step_errors"] = sorted({e for r in results for p in r.get("polls", []) for e in p.get("errors", [])})
        checks = evaluate(client, run, results)
    finally:
        summary["cleanup"] = cleanup(client, run)
    try:
        left = collect_created(client, run)
        leftover = {k: len(v) for k, v in left.items() if v}
    except Exception as exc:  # noqa: BLE001
        leftover = {"unreadable": exc.__class__.__name__}
    summary["leftover"] = leftover
    if leftover or summary["cleanup"]["failed"]:
        checks["cleanup removed every row"] = [f"leftover {leftover}, failed {summary['cleanup']['failed']}"]
    return checks, summary


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from app.core import supabase_client
    from app.core.config import get_settings

    try:
        try:
            settings = get_settings()
        except Exception as exc:  # noqa: BLE001 - never print setting values
            raise Refused(f"The settings did not load ({exc.__class__.__name__}). Check the command line env.") from None
        check_guards(settings)
        client = supabase_client.get_supabase()
        checks, summary = run_check(client, settings, args.user_ids, runner=spawn_runner, now=args.now,
                                    iterations=args.iterations)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    failed = False
    for name, problems in checks.items():
        if problems:
            failed = True
            print(f"FAIL: {name}", flush=True)
            for problem in problems:
                print(f"  - {problem}", flush=True)
        else:
            print(f"PASS: {name}", flush=True)
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

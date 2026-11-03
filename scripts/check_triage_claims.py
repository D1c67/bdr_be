"""Phase 8 triage claim check on the dev database (docs/ASSISTANT_PHASE8_PLAN.md 16.3; WI-7).

    .venv/bin/python scripts/check_triage_claims.py --user-ids <id>,<id>,<id> [--skip-spend]

Two simulated workers (threads, each request on its own pooled connection,
so Postgres sees concurrent transactions) call the 0196 RPCs
assistant_triage_claim and assistant_triage_finish through app/assistant/repo.py
against rows this script seeds for three active internal users, and the
script then asserts from the database:

  claims      30 pending items for 3 people (10 each, batch 4): every item is
              claimed once and decided once, no run ever holds two people's
              items, one person's runs never overlap in time (each run's span
              runs from its claim returning to its finish being sent, on this
              script's monotonic clock, held open briefly so a second claim
              of the same person would land inside it), and every row
              escalated gets exactly one router event, even when two finishes
              race on the same run (the second is fenced out);
  allowlists  two workers with different allowlists never claim or skip each
              other's people;
  travel      with items still pending for the person, a claim with p_now 8
              days ahead never claims them while the other worker's run is
              live and never retires that run (leases run on the real clock);
              the run still finishes and the rest are then claimed normally;
  race        two claims racing on one person with 15 pending items and
              p_batch_max 10 yield exactly one run of 10 items, never a
              second run and never a 0-item insert (MC-5);
  fuse        after one worker's finish crosses the hourly wire, its next
              claim latches the fuse and the other worker's next claim also
              answers {fuse: true} (P8R-2);
  spend       two workers reserving company triage holds in parallel against
              a company cap set just above one hold admit exactly one. Needs
              0196 part B (the triage kind and the company identity); pass
              --skip-spend while part B is not applied.

How it stays apart from everything else on dev: it refuses unless SUPABASE_URL
is the dev project bpidntbyvoooqvaispup; while any assistant_triage_runs row
is claimed under a live lease (another triage worker: the dev servers must run
with ASSISTANT_TRIAGE_ENABLED off, and the WI-16 tick script must not run);
while any triage item is pending anywhere (a fresh switch-on epoch would sweep
it); and unless each user is an active internal profile. During the run it sets
assistant_settings.proactive_enabled off (so a running Phase 7 router finishes
the escalation events `switched_off` and delivers nothing) and
triage_enabled on with a fresh epoch per case; it restores the company row's
enabled, proactive_enabled, triage_enabled, triage_enabled_at,
triage_fuse_tripped_at and global_daily_cap_usd in a finally block, and deletes
every inbox row (its triage items cascade), triage run, router event and
company hold it created, by id. No model call, no send. Exit 0 when every check
passes, 1 on a failed check, 2 on a refusal. Last line:
TEST_RESULT PASS|FAIL pass=N fail=M.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.assistant_proactive_tick import Refused, require_dev_ref  # noqa: E402

UTC = timezone.utc
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
KIND = "bell.drawing_changed"
TITLE = "Phase 8 triage claim check (test)"
LEASE = 420
TRAVEL_AHEAD = timedelta(days=8)
HOLD_OPEN_SECONDS = 0.05      # a claimed run stays open this long before its finish (the overlap window)
HOLD_AMOUNT = Decimal("0.01")
SETTINGS_KEYS = ("enabled", "proactive_enabled", "triage_enabled", "triage_enabled_at", "triage_fuse_tripped_at",
                 "global_daily_cap_usd")
DELETE_CHUNK = 100


def _real_now() -> datetime:
    return datetime.now(UTC)


def _rows(resp: Any) -> list[dict]:
    return [r for r in (getattr(resp, "data", None) or []) if isinstance(r, dict)]


def _ts(value: Any) -> datetime | None:
    if value is None:
        return None
    at = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (at if at.tzinfo else at.replace(tzinfo=UTC)).astimezone(UTC)


def parse_user_ids(value: str) -> list[str]:
    ids = [p.strip().lower() for p in (value or "").split(",") if p.strip()]
    if len(ids) != 3 or len(set(ids)) != 3 or not all(UUID_RE.match(i) for i in ids):
        raise ValueError("--user-ids takes exactly three distinct user ids (uuids), comma separated")
    return ids


def _user_ids(value: str) -> list[str]:
    try:
        return parse_user_ids(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="check_triage_claims.py",
                                     description="Two-worker claim check of the triage RPCs (dev only).")
    parser.add_argument("--user-ids", required=True, type=_user_ids, help="three active internal user ids")
    parser.add_argument("--skip-spend", action="store_true",
                        help="skip the company cap race (needed while 0196 part B is not applied)")
    return parser


# ── The run record ───────────────────────────────────────────────────────────


@dataclass
class Run:
    users: list[str]
    started: datetime
    original: dict[str, Any] = field(default_factory=dict)
    inbox_ids: list[str] = field(default_factory=list)
    holds: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add_inbox(self, ids: list[str]) -> None:
        with self.lock:
            self.inbox_ids.extend(ids)


# ── Guards ───────────────────────────────────────────────────────────────────


def preflight(client: Any, users: list[str], now: datetime) -> list[str]:
    from app.core.roles import INTERNAL_ROLES, Role

    problems: list[str] = []
    rows = _rows(client.table("assistant_settings").select("*").eq("id", 1).limit(1).execute())
    if not rows:
        problems.append("the assistant_settings row is missing")
    elif "triage_enabled" not in rows[0] or "triage_fuse_tripped_at" not in rows[0]:
        problems.append("0196 part A is not applied (assistant_settings has no triage columns)")
    people = {str(r["id"]): r for r in _rows(client.table("profiles").select("id, role, is_active")
                                             .in_("id", users).execute())}
    internal = {r.value for r in INTERNAL_ROLES if r != Role.ESTIMATOR}
    for user in users:
        person = people.get(user)
        if person is None or not person.get("is_active") or person.get("role") not in internal:
            problems.append(f"user {user} is not an active internal profile")
    live = [r for r in _rows(client.table("assistant_triage_runs").select("id, claimed_by, lease_until")
                             .eq("status", "claimed").limit(50).execute())
            if (_ts(r.get("lease_until")) or now) > now]
    if live:
        problems.append(f"{len(live)} triage run(s) are claimed under a live lease: another triage worker is "
                        "running (the dev servers must run with ASSISTANT_TRIAGE_ENABLED off)")
    pending = _rows(client.table("assistant_triage_items").select("id").eq("status", "pending").limit(1).execute())
    if pending:
        problems.append("triage items are pending on dev; a fresh switch-on epoch would sweep them")
    return problems


# ── Settings and seeding ─────────────────────────────────────────────────────


def fresh_epoch(client: Any, run: Run, *, cap: Any = None) -> datetime:
    """Switch triage on with a new epoch from the database clock and the
    latch clear, proactive_enabled off (no escalation is ever delivered).
    The set_updated_at trigger stamps updated_at with now(); that value is
    then written as the epoch, the switch route's two-step (repo.set_triage_enabled)."""
    patch: dict[str, Any] = {"enabled": True, "proactive_enabled": False, "triage_enabled": True,
                             "triage_fuse_tripped_at": None, "triage_enabled_at": _real_now().isoformat()}
    if cap is not None:
        patch["global_daily_cap_usd"] = float(cap)
    rows = _rows(client.table("assistant_settings").update(patch).eq("id", 1).execute())
    stamp = rows[0]["updated_at"] if rows and rows[0].get("updated_at") else _real_now().isoformat()
    client.table("assistant_settings").update({"triage_enabled_at": stamp}).eq("id", 1).execute()
    return _ts(stamp) or _real_now()


def seed(client: Any, run: Run, user: str, count: int, *, now: datetime) -> list[str]:
    """`count` bell-sourced record rows for `user`, enqueued for that user
    only. The rows carry created_at = `now` (the case's epoch, from the
    database clock), so the enqueue window (epoch <= created_at <= p_now)
    takes them whatever the machine clock says. Every id is recorded before
    the insert."""
    from app.assistant import repo

    ids = [str(uuid.uuid4()) for _ in range(count)]
    run.add_inbox(ids)
    client.table("assistant_inbox").insert([
        {"id": i, "user_id": user, "dedupe_key": f"p7:{KIND}:claimcheck:{i}", "severity": "normal", "title": TITLE,
         "payload": {}, "delivery": "record", "event_type": KIND, "created_at": now.isoformat()}
        for i in ids
    ]).execute()
    repo.triage_enqueue(None, now, 24, limit=1000, user_ids=[user])
    return ids


def items_for(client: Any, inbox_ids: list[str]) -> list[dict]:
    out: list[dict] = []
    for start in range(0, len(inbox_ids), DELETE_CHUNK):
        chunk = inbox_ids[start:start + DELETE_CHUNK]
        out.extend(_rows(client.table("assistant_triage_items").select("*").in_("inbox_id", chunk).execute()))
    return out


def runs_for(client: Any, users: list[str], since: datetime) -> list[dict]:
    return _rows(client.table("assistant_triage_runs").select("*").in_("user_id", users)
                 .gte("created_at", since.isoformat()).limit(1000).execute())


def events_for(client: Any, inbox_ids: list[str]) -> list[dict]:
    keys = [f"p8:esc:{i}" for i in inbox_ids]
    out: list[dict] = []
    for start in range(0, len(keys), DELETE_CHUNK):
        out.extend(_rows(client.table("assistant_events").select("id, dedupe_key, subject_user_id")
                         .in_("dedupe_key", keys[start:start + DELETE_CHUNK]).execute()))
    return out


# ── Workers ──────────────────────────────────────────────────────────────────


def _claim(worker: str, now: datetime, *, users: list[str] | None, batch: int = 4, ratio: float = 0,
           per_hour: int = 0) -> dict | None:
    from app.assistant import repo

    return repo.triage_claim(None, worker, now, LEASE, 0, batch, ratio, per_hour, user_ids=users)


def _decide(claim: dict, decision: str = "escalate") -> list[dict]:
    if decision == "escalate":
        return [{"id": i["id"], "decision": "escalate", "reason": "waiting_on_you", "action": None,
                 "severity_hint": "normal"} for i in claim["items"]]
    return [{"id": i["id"], "decision": "none", "reason": "info_only", "action": None, "severity_hint": None}
            for i in claim["items"]]


def _finish(claim: dict, worker: str, now: datetime, decision: str = "escalate") -> dict:
    from app.assistant import repo

    return repo.triage_finish(None, claim["run_id"], worker, now, "decided", None, _decide(claim, decision),
                              {"model": "claimcheck"})


def _threads(targets: list[Callable[[], None]]) -> list[BaseException]:
    errors: list[BaseException] = []

    def wrap(fn: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 - reported as a failed check
                errors.append(exc)
        return run

    threads = [threading.Thread(target=wrap(fn)) for fn in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)
    return errors


# ── Cases ────────────────────────────────────────────────────────────────────


def case_claims(client: Any, run: Run) -> list[str]:
    now = fresh_epoch(client, run)
    inbox: list[str] = []
    for user in run.users:
        inbox += seed(client, run, user, 10, now=now)
    raced: list[dict] = []
    # Per person: (claim returned, finish sent) on one monotonic clock. Each
    # span lies inside the run's real open time, so two spans that intersect
    # prove two live runs for one person. Never the RPC's finished_at: that is
    # p_now (the case's epoch), earlier than every real-clock claimed_at.
    spans: dict[str, list[tuple[float, float]]] = {}

    def worker(name: str) -> None:
        first = True
        for _ in range(40):
            claim = _claim(name, now, users=run.users)
            if not claim or not claim.get("run_id"):
                return
            opened = time.monotonic()
            time.sleep(HOLD_OPEN_SECONDS)
            with run.lock:
                spans.setdefault(str(claim["user_id"]), []).append((opened, time.monotonic()))
            if first:
                # Two finishes race on the same run: the fence lets exactly one write.
                results: list[dict] = []
                errs = _threads([lambda c=claim: results.append(_finish(c, name, now)) for _ in range(2)])
                if errs:
                    raise errs[0]
                raced.append({"fenced": sorted(bool(r.get("fenced")) for r in results)})
                first = False
            else:
                _finish(claim, name, now)

    problems = [f"worker error {e.__class__.__name__}" for e in _threads([lambda: worker("w-a"), lambda: worker("w-b")])]
    items = items_for(client, inbox)
    if len(items) != 30:
        problems.append(f"expected 30 items, found {len(items)}")
    statuses = Counter(i.get("status") for i in items)
    if statuses != Counter({"decided": 30}):
        problems.append(f"items not all decided once: {dict(statuses)}")
    by_run: dict[str, set[str]] = {}
    for item in items:
        by_run.setdefault(str(item.get("run_id")), set()).add(str(item["user_id"]))
    if any(len(people) != 1 for people in by_run.values()):
        problems.append("a run holds two people's items")
    for user in run.users:
        mine = sorted(spans.get(user, []))
        if not mine:
            problems.append(f"user {user} was never claimed")
        if any(mine[i + 1][0] < mine[i][1] for i in range(len(mine) - 1)):
            problems.append(f"user {user} was in two runs at once")
    events = events_for(client, inbox)
    keys = Counter(e["dedupe_key"] for e in events)
    if len(keys) != 30 or any(n != 1 for n in keys.values()):
        problems.append(f"expected one event per row (30), found {len(events)} events on {len(keys)} rows")
    owners = {str(i["inbox_id"]): str(i["user_id"]) for i in items}
    if any(str(e.get("subject_user_id")) != owners.get(e["dedupe_key"].split(":")[-1]) for e in events):
        problems.append("an event is addressed to someone other than the row's owner")
    if len(raced) != 2 or any(r["fenced"] != [False, True] for r in raced):
        problems.append(f"racing finishes were not fenced to one writer: {raced}")
    return problems


def case_allowlists(client: Any, run: Run) -> list[str]:
    now = fresh_epoch(client, run)
    a, b = run.users[0], run.users[1]
    inbox = seed(client, run, a, 5, now=now) + seed(client, run, b, 5, now=now)
    seen: dict[str, set[str]] = {"w-a": set(), "w-b": set()}

    def worker(name: str, allow: list[str]) -> None:
        for _ in range(20):
            claim = _claim(name, now, users=allow, batch=2)
            if not claim or not claim.get("run_id"):
                return
            seen[name].add(claim["user_id"])
            _finish(claim, name, now, "none")

    problems = [f"worker error {e.__class__.__name__}"
                for e in _threads([lambda: worker("w-a", [a]), lambda: worker("w-b", [b])])]
    if seen["w-a"] - {a} or seen["w-b"] - {b}:
        problems.append(f"a worker claimed outside its allowlist: {seen}")
    statuses = Counter((i["user_id"], i.get("status"), i.get("skip_reason")) for i in items_for(client, inbox))
    if statuses != Counter({(a, "decided", None): 5, (b, "decided", None): 5}):
        problems.append(f"items not decided by their own worker: {dict(statuses)}")
    return problems


def case_travel(client: Any, run: Run) -> list[str]:
    now = fresh_epoch(client, run)
    user = run.users[2]
    seed(client, run, user, 3, now=now)
    problems: list[str] = []
    # Batch 1 leaves two items pending, so the claim ahead has candidates and
    # only the live-run exclusion can keep it from claiming the person.
    claim = _claim("w-a", now, users=[user], batch=1)
    if not claim or not claim.get("run_id"):
        return ["the first claim returned no run"]
    ahead = _claim("w-b", now + TRAVEL_AHEAD, users=[user], batch=10)
    if ahead and ahead.get("run_id"):
        problems.append("the claim 8 days ahead claimed the person who has a live run")
        _finish(ahead, "w-b", now + TRAVEL_AHEAD, "none")
    row = _rows(client.table("assistant_triage_runs").select("status").eq("id", claim["run_id"]).execute())
    if not row or row[0].get("status") != "claimed":
        problems.append(f"the live run was retired by a claim 8 days ahead: {row}")
    result = _finish(claim, "w-a", now, "none")
    if not result.get("fenced"):
        problems.append("the live run's own finish was fenced out")
    rest = _claim("w-a", now, users=[user], batch=10)
    if not rest or not rest.get("run_id"):
        if not (ahead and ahead.get("run_id")):
            problems.append("the pending items were not claimable once the live run finished")
    else:
        if len(rest.get("items") or []) != 2:
            problems.append(f"expected the 2 items left pending, got {len(rest.get('items') or [])}")
        _finish(rest, "w-a", now, "none")
    return problems


def case_race(client: Any, run: Run) -> list[str]:
    now = fresh_epoch(client, run)
    user = run.users[0]
    seed(client, run, user, 15, now=now)
    barrier = threading.Barrier(2)
    got: list[tuple[str, dict | None]] = []

    def worker(name: str) -> None:
        barrier.wait(timeout=30)
        got.append((name, _claim(name, now, users=[user], batch=10)))

    problems = [f"worker error {e.__class__.__name__}" for e in _threads([lambda: worker("w-a"), lambda: worker("w-b")])]
    runs = [(name, g) for name, g in got if g and g.get("run_id")]
    if len(runs) != 1:
        problems.append(f"expected exactly one run, got {len(runs)}")
    elif len(runs[0][1]["items"]) != 10:
        problems.append(f"the run holds {len(runs[0][1]['items'])} items, not 10")
    for name, claim in runs:
        _finish(claim, name, now, "none")
    live = _rows(client.table("assistant_triage_runs").select("id, item_count").eq("user_id", user)
                 .gte("created_at", run.started.isoformat()).eq("status", "claimed").execute())
    if live:
        problems.append(f"{len(live)} run(s) left claimed after the race")
    zero = _rows(client.table("assistant_triage_runs").select("id").eq("user_id", user).eq("item_count", 0).execute())
    if zero:
        problems.append("a 0-item run was inserted")
    return problems


def case_fuse(client: Any, run: Run) -> list[str]:
    now = fresh_epoch(client, run)
    a, b = run.users[0], run.users[1]
    seed(client, run, a, 3, now=now)
    seed(client, run, b, 3, now=now)
    problems: list[str] = []
    claim = _claim("w-a", now, users=[a], batch=3, per_hour=2)
    if not claim or not claim.get("run_id"):
        return ["the first claim returned no run"]
    _finish(claim, "w-a", now)                       # 3 escalations > 2 in the hour
    answers: dict[str, Any] = {}

    def worker(name: str, allow: list[str]) -> None:
        answers[name] = _claim(name, now, users=allow, batch=3, per_hour=2)

    problems += [f"worker error {e.__class__.__name__}"
                 for e in _threads([lambda: worker("w-a", [a]), lambda: worker("w-b", [b])])]
    if not all(isinstance(v, dict) and v.get("fuse") for v in answers.values()) or len(answers) != 2:
        problems.append(f"both workers' next claims should answer fuse: {answers}")
    row = _rows(client.table("assistant_settings").select("triage_fuse_tripped_at").eq("id", 1).execute())
    if not row or not row[0].get("triage_fuse_tripped_at"):
        problems.append("the latch was not set")
    after = _claim("w-b", now + timedelta(minutes=61), users=[b], batch=3, per_hour=2)
    if not (isinstance(after, dict) and after.get("fuse")):
        problems.append("the latch cleared by itself 61 minutes later")
    return problems


def case_spend(client: Any, run: Run, settings: Any) -> list[str]:
    from app.assistant import repo, spend

    now = fresh_epoch(client, run)
    data = repo.triage_counts(None, now, now)
    sp = data.get("spend") or {}
    if "global_spent" not in sp or "global_held" not in sp:
        return ["the counts RPC returned no spend block"]
    used = Decimal(str(sp["global_spent"])) + Decimal(str(sp["global_held"]))
    cap = (used + HOLD_AMOUNT * Decimal("1.5")).quantize(Decimal("0.0001"))
    fresh_epoch(client, run, cap=cap)
    admitted: list[str] = []
    refused: list[str] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait(timeout=30)
        try:
            hold = spend.reserve(spend.COMPANY_SPEND_ID, kind=spend.KIND_TRIAGE, amount=HOLD_AMOUNT, ttl=60,
                                 run_id=str(uuid.uuid4()), settings=settings)
        except spend.SpendRefused:
            refused.append("global")
            return
        with run.lock:
            run.holds.append(hold)
        admitted.append(hold)

    problems = [f"worker error {e.__class__.__name__}" for e in _threads([worker, worker])]
    if len(admitted) != 1 or len(refused) != 1:
        problems.append(f"expected exactly one hold admitted, got {len(admitted)} admitted and {len(refused)} refused")
    for hold in admitted:
        spend.release(spend.COMPANY_SPEND_ID, hold)
    return problems


# ── Cleanup and main ─────────────────────────────────────────────────────────


def cleanup(client: Any, run: Run, log: Callable[[str], None] = print) -> None:
    """Delete what this run created, by id, and restore the company row."""
    errors: list[str] = []
    try:
        runs = [r["id"] for r in runs_for(client, run.users, run.started)]
        events = [e["id"] for e in events_for(client, run.inbox_ids)]
        for table, ids in (("assistant_events", events), ("assistant_inbox", run.inbox_ids),
                           ("assistant_triage_runs", runs), ("assistant_spend_reservations", run.holds)):
            for start in range(0, len(ids), DELETE_CHUNK):
                client.table(table).delete().in_("id", ids[start:start + DELETE_CHUNK]).execute()
    except Exception as exc:  # noqa: BLE001 - restore the settings row whatever happened
        errors.append(f"row cleanup failed ({exc.__class__.__name__})")
    if run.original:
        client.table("assistant_settings").update(run.original).eq("id", 1).execute()
    log(json.dumps({"cleanup": {"inbox": len(run.inbox_ids), "holds": len(run.holds), "errors": errors}}))


def run_check(client: Any, settings: Any, users: list[str], *, skip_spend: bool) -> dict[str, list[str]]:
    now = _real_now()
    problems = preflight(client, users, now)
    if problems:
        raise Refused("; ".join(problems))
    row = _rows(client.table("assistant_settings").select("*").eq("id", 1).limit(1).execute())[0]
    run = Run(users=users, started=now - timedelta(seconds=5), original={k: row.get(k) for k in SETTINGS_KEYS})
    checks: dict[str, list[str]] = {}
    try:
        checks["claims"] = case_claims(client, run)
        checks["allowlists"] = case_allowlists(client, run)
        checks["travel"] = case_travel(client, run)
        checks["race"] = case_race(client, run)
        checks["fuse"] = case_fuse(client, run)
        if not skip_spend:
            checks["spend"] = case_spend(client, run, settings)
    finally:
        cleanup(client, run)
    return checks


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from app.core import supabase_client
    from app.core.config import get_settings

    try:
        try:
            settings = get_settings()
        except Exception as exc:  # noqa: BLE001 - never print setting values
            raise Refused(f"The settings did not load ({exc.__class__.__name__}).") from None
        require_dev_ref(settings)
        client = supabase_client.get_supabase()
        checks = run_check(client, settings, args.user_ids, skip_spend=args.skip_spend)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    passed = sum(1 for p in checks.values() if not p)
    failed = len(checks) - passed
    for name, problems in checks.items():
        print(f"{'PASS' if not problems else 'FAIL'}: {name}", flush=True)
        for problem in problems:
            print(f"  - {problem}", flush=True)
    print(f"TEST_RESULT {'PASS' if not failed else 'FAIL'} pass={passed} fail={failed}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The phase 7 repo functions (docs/ASSISTANT_PHASE7_PLAN.md, WI-3) and the
FakeDB emulation of migration 0190 they run against.

Invariants pinned here:

  convention  every function after the phase 7 marker takes user_id first;
              a function that spans every user is a system sweep: only None
              is accepted (a person's id raises before the database is
              touched); the four scoped sweeps answer for one person when
              given one; RPC wrappers pass p_now and exactly the arguments
              the SQL declares; PostgREST upserts target only full unique
              constraints (checked against the SQL), never the partial
              indexes of assistant_subscriptions; SETTINGS_COLUMNS reads
              proactive_enabled; no em dash, no bare deliver( call;
  fences      event claim (lease, p_ids, lease_exhausted retire, attempts at
              claim), event finish fence and backoff, inbox deliver exactly
              once, digest deliver one ledger row per user and slot, the
              per-user lock shared by deliver, digest and fold, fold and
              un-fold, send claim and settle (every outcome, the deferred
              branch, the stale claim retired as uncertain), the slot lease,
              the cursor fence, two workers over one FakeDB;
  rows        feedback (mute, mute_project, the third not_useful), snooze,
              dismiss, link bell (rank, 120 s, either order), the counts-only
              report with its floor, the purge with legal hold.
"""

from __future__ import annotations

import ast
import inspect
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.assistant import repo
from app.core import supabase_client
from tests import assistant_fakes as fk
from tests.assistant_fakes import (
    BELL,
    BELL2,
    DIGEST,
    INBOX,
    INBOX2,
    P7_OTHER,
    P7_T0,
    PROJECT,
    SEND,
    USER,
    FakeDB,
    UniqueViolation,
    bell_row,
    cursor_row,
    digest_row,
    event_row,
    inbox_row,
    send_row,
    slot_row,
    subscription_row,
)

SQL = (Path(__file__).resolve().parents[1] / "supabase/migrations/0190_assistant_phase7.sql").read_text(
    encoding="utf-8")
REPO_SOURCE = Path(repo.__file__).read_text(encoding="utf-8")
MARKER = "# ── Phase 7: the proactive router"
P7_SOURCE = REPO_SOURCE[REPO_SOURCE.index(MARKER):]
P7_START_LINE = REPO_SOURCE[: REPO_SOURCE.index(MARKER)].count("\n") + 1
EM_DASH = chr(0x2014)


def at(**delta) -> datetime:
    return P7_T0 + timedelta(**delta)


def iso(**delta) -> str:
    return at(**delta).isoformat()


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB(now=P7_T0)
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: fake)
    return fake


def seeded(monkeypatch, **tables) -> FakeDB:
    fake = FakeDB(now=P7_T0, **tables)
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: fake)
    return fake


def rows(fake: FakeDB, table: str) -> list[dict]:
    return fake.tables.get(table, [])


def one(fake: FakeDB, table: str, **eq) -> dict:
    return next(r for r in rows(fake, table) if all(str(r.get(k)) == str(v) for k, v in eq.items()))


# ── Conventions ──────────────────────────────────────────────────────────────


def _phase7_functions() -> dict[str, object]:
    tree = ast.parse(REPO_SOURCE)
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.lineno >= P7_START_LINE]
    return {name: getattr(repo, name) for name in names}


PUBLIC_API = {
    "set_proactive_enabled", "insert_events", "events_by_dedupe_keys", "events_claim", "event_finish",
    "insert_inbox_row", "inbox_row", "inbox_row_by_key", "inbox_rows_by_keys", "inbox_due_immediate",
    "inbox_folded_pending",
    "inbox_pending_digest_users", "inbox_pending_digest", "inbox_digest_items", "inbox_list",
    "inbox_unread_rows", "notification_states", "inbox_supersede_due_not_ready", "inbox_retitle", "inbox_deliver",
    "digest_deliver", "inbox_mark_read", "inbox_feedback", "inbox_snooze", "inbox_dismiss", "inbox_link_bell",
    "inbox_fold", "user_subscriptions", "role_subscriptions", "subscription_set", "role_subscription_set",
    "subscription_reset", "role_subscription_reset", "project_mutes", "project_mute_add", "project_mute_remove",
    "project_muted_user_ids", "digest", "digest_for_slot", "digests_recent", "digest_auto_downgrades", "router_cursors",
    "advance_router_cursor", "reseed_router_cursors", "proactive_slot", "claim_proactive_slot",
    "record_proactive_slot", "finish_proactive_slot", "external_send_insert", "external_send",
    "external_send_for_target", "external_sends_claim", "external_send_settle", "offboard_dismiss_undelivered",
    "offboard_skip_pending_sends", "offboard_delete_subscriptions", "offboard_delete_project_mutes",
    "proactive_purge", "inbox_feedback_report",
}


def test_the_public_api_is_pinned():
    public = {n for n in _phase7_functions() if not n.startswith("_")}
    assert public == PUBLIC_API, (public ^ PUBLIC_API)


def test_every_phase7_function_takes_user_id_first():
    checked = 0
    for name, fn in _phase7_functions().items():
        if name.startswith("_p7_"):
            continue
        params = list(inspect.signature(fn).parameters)
        assert params and params[0] == "user_id", name
        checked += 1
    assert checked >= 50


def test_the_scoped_and_system_sweeps_are_marked():
    """A user_id typed `str | None` is a system sweep and says so."""
    optional = [n for n, fn in _phase7_functions().items()
                if not n.startswith("_p7_") and "None" in str(next(iter(inspect.signature(fn).parameters.values())).annotation)]
    assert len(optional) >= 20
    for name in optional:
        assert "system sweep" in (getattr(repo, name).__doc__ or "").lower(), name


ITEM = "9c9c9c9c-9c9c-49c9-89c9-9c9c9c9c9c91"

SYSTEM_ONLY = {
    "insert_events": lambda u: repo.insert_events(u, [{"event_type": "todo.due", "dedupe_key": "p7:todo.due:x"}]),
    "events_by_dedupe_keys": lambda u: repo.events_by_dedupe_keys(u, ["p7:x"]),
    "events_claim": lambda u: repo.events_claim(u, worker="w", limit=5),
    "event_finish": lambda u: repo.event_finish(u, ITEM, worker="w", status="processed"),
    "router_cursors": lambda u: repo.router_cursors(u),
    "advance_router_cursor": lambda u: repo.advance_router_cursor(
        u, "notifications", expect_at=iso(), expect_id=None, new_at=iso(), updated_by="w"),
    "reseed_router_cursors": lambda u: repo.reseed_router_cursors(u, to_at=at(), updated_by="w"),
    "proactive_slot": lambda u: repo.proactive_slot(u, "digest_am", date(2026, 10, 30)),
    "claim_proactive_slot": lambda u: repo.claim_proactive_slot(u, "digest_am", date(2026, 10, 30), "w", at()),
    "record_proactive_slot": lambda u: repo.record_proactive_slot(
        u, "digest_am", date(2026, 10, 30), worker="w", result={}),
    "finish_proactive_slot": lambda u: repo.finish_proactive_slot(u, "digest_am", date(2026, 10, 30), worker="w"),
    "external_sends_claim": lambda u: repo.external_sends_claim(u, worker="w"),
    "external_send_settle": lambda u: repo.external_send_settle(u, ITEM, worker="w", outcome="sent"),
    "proactive_purge": lambda u: repo.proactive_purge(u, inbox_days=90, event_days=90, digest_days=365, send_days=90),
    "inbox_feedback_report": lambda u: repo.inbox_feedback_report(u, at(days=-30)),
}


@pytest.mark.parametrize("name", sorted(SYSTEM_ONLY))
def test_system_sweeps_accept_only_none(db, name):
    with pytest.raises(ValueError, match="system sweep"):
        SYSTEM_ONLY[name](USER)
    assert db.calls == [] and db.rpc_calls == []
    SYSTEM_ONLY[name](None)  # None is accepted
    assert db.calls or db.rpc_calls


def test_every_system_only_function_is_in_the_table():
    """A function whose source calls _p7_system_only is covered above."""
    guarded = {n for n, fn in _phase7_functions().items()
               if not n.startswith("_p7_") and "_p7_system_only(" in inspect.getsource(fn)}
    assert guarded == set(SYSTEM_ONLY)


def test_the_scoped_sweeps_answer_for_one_person_or_everyone(db):
    db.tables.update({
        "assistant_inbox": [
            inbox_row(),
            inbox_row(id=INBOX2, user_id=P7_OTHER, dedupe_key="p7:todo.due:t2:2026-10-30"),
        ],
        "assistant_project_mutes": [
            {"user_id": USER, "project_id": PROJECT, "created_at": iso()},
            {"user_id": P7_OTHER, "project_id": PROJECT, "created_at": iso()},
        ],
    })
    assert {r["user_id"] for r in repo.inbox_due_immediate(None, now=at())} == {USER, P7_OTHER}
    assert [r["user_id"] for r in repo.inbox_due_immediate(USER, now=at())] == [USER]
    assert sorted(repo.project_muted_user_ids(None, PROJECT)) == sorted([USER, P7_OTHER])
    assert repo.project_muted_user_ids(P7_OTHER, PROJECT) == [P7_OTHER]
    folded = [inbox_row(delivery="digest", precedence="quiet_hours"),
              inbox_row(id=INBOX2, user_id=P7_OTHER, dedupe_key="p7:b", delivery="digest", precedence="quiet_hours")]
    db.tables["assistant_inbox"] = folded
    assert len(repo.inbox_folded_pending(None)) == 2
    assert [r["user_id"] for r in repo.inbox_folded_pending(USER)] == [USER]
    assert sorted(repo.inbox_pending_digest_users(None, slot_at=at())) == sorted([USER, P7_OTHER])
    assert repo.inbox_pending_digest_users(USER, slot_at=at()) == [USER]


def _sql_functions() -> dict[str, list[tuple[str, bool]]]:
    out: dict[str, list[tuple[str, bool]]] = {}
    for match in re.finditer(r"create or replace function public\.(\w+)\(\s*(.*?)\)\s*returns", SQL, re.S):
        args = [a.strip() for a in match.group(2).split(",\n") if a.strip()]
        flat = []
        for chunk in re.split(r",\s*(?=p_)", " ".join(match.group(2).split())):
            if chunk.strip():
                flat.append((chunk.split()[0], " default " in chunk))
        out.setdefault(match.group(1), flat or [(a.split()[0], " default " in a) for a in args])
    return out


RPC_CALLS = {
    repo.EVENTS_CLAIM_RPC: lambda: repo.events_claim(None, worker="w", limit=5, ids=[ITEM]),
    repo.EVENT_FINISH_RPC: lambda: repo.event_finish(None, ITEM, worker="w", status="retry"),
    repo.INBOX_DELIVER_RPC: lambda: repo.inbox_deliver(USER, INBOX, bell_message="m", teams=True,
                                                       teams_not_before=at(hours=1)),
    repo.DIGEST_DELIVER_RPC: lambda: repo.digest_deliver(
        USER, digest_date=date(2026, 10, 30), slot="am", slot_at=at(), worker="w", email=True),
    repo.INBOX_MARK_READ_RPC: lambda: repo.inbox_mark_read(USER, [INBOX]),
    repo.INBOX_FEEDBACK_RPC: lambda: repo.inbox_feedback(USER, INBOX, "useful"),
    repo.INBOX_SNOOZE_RPC: lambda: repo.inbox_snooze(USER, INBOX, at(hours=6), now=at()),
    repo.SUBSCRIPTION_SET_RPC: lambda: repo.subscription_set(USER, "todo.due", enabled=False, severity="normal"),
    repo.SUBSCRIPTION_RESET_RPC: lambda: repo.subscription_reset(USER, "todo.due"),
    repo.INBOX_DISMISS_RPC: lambda: repo.inbox_dismiss(USER, [INBOX], "user"),
    repo.FEEDBACK_REPORT_RPC: lambda: repo.inbox_feedback_report(None, at(days=-30)),
    repo.LINK_BELL_RPC: lambda: repo.inbox_link_bell(
        USER, "p7:bell:email:abc", notification_id=BELL, bell_at=at(), event_type="bell.quote_received", rank=1,
        title="A quote arrived", why=None, payload={"notification_id": BELL}, project_id=None),
    repo.SENDS_CLAIM_RPC: lambda: repo.external_sends_claim(None, worker="w"),
    repo.SEND_SETTLE_RPC: lambda: repo.external_send_settle(None, SEND, worker="w", outcome="sent"),
    repo.INBOX_FOLD_RPC: lambda: repo.inbox_fold(USER, INBOX, am_date=date(2026, 10, 30), am_slot_at=at(),
                                                 fold_until=at(hours=3)),
    repo.PROACTIVE_PURGE_RPC: lambda: repo.proactive_purge(
        None, inbox_days=90, event_days=90, digest_days=365, send_days=90),
}


def test_every_0190_router_rpc_has_a_wrapper():
    sql_names = {n for n in _sql_functions() if n.startswith("assistant_") and n != "assistant_inbox_classify"}
    assert sql_names == set(RPC_CALLS), sql_names ^ set(RPC_CALLS)
    assert len(sql_names) == 16


@pytest.mark.parametrize("rpc", sorted(RPC_CALLS))
def test_rpc_wrappers_pass_p_now_and_the_declared_arguments(db, rpc):
    RPC_CALLS[rpc]()
    name, params = db.rpc_calls[-1]
    declared = dict(_sql_functions()[rpc])
    assert name == rpc
    assert set(params) <= set(declared), set(params) - set(declared)
    assert {p for p, has_default in declared.items() if not has_default} <= set(params)
    if "p_now" in declared:
        assert "p_now" in params
        assert _parse(params["p_now"]).tzinfo is not None
    else:
        assert rpc in (repo.SUBSCRIPTION_SET_RPC, repo.SUBSCRIPTION_RESET_RPC)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def test_p_now_is_the_callers_time_in_utc_and_naive_times_are_refused(db):
    pacific = datetime(2026, 10, 30, 7, 0, tzinfo=timezone(timedelta(hours=-7)))
    repo.inbox_mark_read(USER, None, now=pacific)
    assert db.rpc_calls[-1][1]["p_now"] == "2026-10-30T14:00:00+00:00"
    assert db.rpc_calls[-1][1]["p_ids"] is None
    with pytest.raises(ValueError, match="timezone"):
        repo.inbox_mark_read(USER, None, now=datetime(2026, 10, 30, 7, 0))


def test_upserts_target_only_full_unique_constraints():
    targets = set(re.findall(r"on_conflict=([A-Z_]+)", P7_SOURCE))
    assert targets == {"EVENTS_CONFLICT", "INBOX_CONFLICT", "PROJECT_MUTES_CONFLICT", "SLOTS_CONFLICT",
                       "SENDS_CONFLICT"}
    assert repo.EVENTS_CONFLICT == "dedupe_key"
    assert repo.INBOX_CONFLICT == "user_id,dedupe_key"
    assert repo.PROJECT_MUTES_CONFLICT == "user_id,project_id"
    assert repo.SLOTS_CONFLICT == "step,slot_date"
    assert repo.SENDS_CONFLICT == "target_kind,target_id,channel"
    flat = " ".join(SQL.split()).lower()
    core = " ".join((Path(repo.__file__).resolve().parents[2] / "supabase/migrations/0170_assistant_core.sql")
                    .read_text(encoding="utf-8").split()).lower()
    assert "dedupe_key text not null unique" in core
    assert "constraint assistant_inbox_dedupe_uq unique (user_id, dedupe_key)" in core
    assert "primary key (user_id, project_id)" in flat
    assert "primary key (step, slot_date)" in flat
    assert "constraint assistant_external_sends_target_uq unique (target_kind, target_id, channel)" in flat
    # The partial indexes of assistant_subscriptions are never an upsert target.
    for name, fn in _phase7_functions().items():
        source = inspect.getsource(fn)
        if "SUBSCRIPTIONS_TABLE" in source or "DIGESTS_TABLE" in source or "ROUTER_CURSORS_TABLE" in source:
            assert ".upsert(" not in source, name


def test_settings_columns_read_the_company_switch():
    assert "proactive_enabled" in repo.SETTINGS_COLUMNS.replace(" ", "").split(",")


def test_set_proactive_enabled_writes_the_switch(db):
    db.tables["assistant_settings"] = [fk.settings_row()]
    out = repo.set_proactive_enabled(USER, True)
    assert out["proactive_enabled"] is True
    assert one(db, "assistant_settings", id=1)["updated_by"] == USER
    db.tables["assistant_settings"] = []
    with pytest.raises(RuntimeError):
        repo.set_proactive_enabled(USER, False)


def test_phase7_source_is_clean():
    assert EM_DASH not in P7_SOURCE
    assert EM_DASH not in Path(fk.__file__).read_text(encoding="utf-8")
    assert not re.search(r"(?<![\w.])deliver\(", P7_SOURCE)
    for banned in ("notifications.deliver", "notify_role", "notify_user", "notification_email", "teams_alerts",
                   "graph_email", "get_supabase", "complete_messages", "app.services.llm"):
        assert banned not in P7_SOURCE, banned


def test_only_the_wrappers_name_the_phase7_rpcs():
    """A new RPC name belongs to a constant; no call site spells it twice."""
    for const in [c for c in dir(repo) if c.endswith("_RPC") and c.startswith(("EVENT", "INBOX", "DIGEST", "SUBSCR",
                                                                               "FEEDBACK", "LINK", "SENDS", "SEND_",
                                                                               "PROACTIVE"))]:
        value = getattr(repo, const)
        assert P7_SOURCE.count(f'"{value}"') == 1, const


# ── Events ───────────────────────────────────────────────────────────────────


def test_insert_events_ignores_duplicates_and_drops_unknown_keys(db):
    row = {"event_type": "todo.due", "dedupe_key": "p7:todo.due:t1:2026-10-30", "subject_user_id": USER,
           "payload": {"todo_id": "t1"}, "occurred_at": at(), "source": "sweeper", "junk": 1, "id": "forged"}
    first = repo.insert_events(None, [row])
    assert len(first) == 1 and first[0]["id"] != "forged" and "junk" not in first[0]
    assert first[0]["status"] == "pending" and first[0]["attempts"] == 0
    assert repo.insert_events(None, [row, {**row, "dedupe_key": "p7:todo.due:t2:2026-10-30"}]) \
        and len(rows(db, "assistant_events")) == 2
    assert repo.insert_events(None, [row]) == []
    with pytest.raises(ValueError):
        repo.insert_events(None, [{"event_type": "todo.due"}])


def test_insert_events_sends_one_key_set_per_row(db):
    """PostgREST writes NULL for a key one row of a batch lacks, so a batch
    must carry the same keys in every row and fill the NOT NULL columns."""
    sent: list[list[dict]] = []

    class Spy:
        def __init__(self, inner):
            self.inner = inner

        def upsert(self, payload, **kw):
            sent.append(payload)
            return self.inner.upsert(payload, **kw)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    original = db.table
    db.table = lambda name: Spy(original(name)) if name == "assistant_events" else original(name)
    bare = {"event_type": "todo.due", "dedupe_key": "p7:todo.due:t1:2026-10-30"}
    full = {"event_type": "todo.due", "dedupe_key": "p7:todo.due:t2:2026-10-30", "payload": {"todo_id": "t2"},
            "occurred_at": at(), "source": "sweeper", "status": "claimed", "claimed_by": "w", "claimed_at": at(),
            "attempts": 1}
    out = repo.insert_events(None, [bare, full])
    assert len(out) == 2
    assert len({frozenset(item) for item in sent[0]}) == 1
    first = sent[0][0]
    assert first["payload"] == {} and first["status"] == "pending" and first["attempts"] == 0
    assert first["occurred_at"] is not None


def test_a_slot_holder_inserts_its_events_already_claimed_and_the_general_claim_skips_them(db):
    held = {"event_type": "todo.due", "dedupe_key": "p7:todo.due:t1:2026-10-30", "source": "sweeper",
            "status": "claimed", "claimed_by": "holder", "claimed_at": at(), "attempts": 1, "occurred_at": at()}
    pending = {"event_type": "todo.due", "dedupe_key": "p7:todo.due:t2:2026-10-30", "source": "sweeper",
               "occurred_at": at()}
    repo.insert_events(None, [held, pending])
    got = repo.events_claim(None, worker="other", limit=10, now=at(seconds=5))
    assert [e["dedupe_key"] for e in got] == [pending["dedupe_key"]]
    ids = [r["id"] for r in repo.events_by_dedupe_keys(None, [held["dedupe_key"]])]
    assert repo.events_claim(None, worker="other", limit=10, now=at(seconds=5), ids=ids) == []
    stale = repo.events_claim(None, worker="other", limit=10, now=at(seconds=700), ids=ids)
    assert [e["claimed_by"] for e in stale] == ["other"] and stale[0]["attempts"] == 2
    assert repo.events_claim(None, worker="other", limit=10, now=at(), ids=[]) == []


def test_event_claim_lease_attempts_and_the_lease_exhausted_retire(db):
    db.tables["assistant_events"] = [
        event_row(id=ITEM, dedupe_key="p7:poison", status="claimed", claimed_by="dead", claimed_at=iso(), attempts=5),
        event_row(dedupe_key="p7:fresh"),
    ]
    got = repo.events_claim(None, worker="w1", limit=50, now=at(seconds=700))
    assert [e["dedupe_key"] for e in got] == ["p7:fresh"] and got[0]["attempts"] == 1
    poison = one(db, "assistant_events", id=ITEM)
    assert poison["status"] == "failed" and poison["last_error"] == "lease_exhausted"
    assert poison["processed_at"] is not None
    # Not stale yet: another worker cannot take a live claim.
    assert repo.events_claim(None, worker="w2", limit=50, now=at(seconds=100)) == []
    # Stale: reclaimed, attempts rise at claim.
    again = repo.events_claim(None, worker="w2", limit=50, now=at(seconds=2000))
    assert again[0]["claimed_by"] == "w2" and again[0]["attempts"] == 2


def test_event_claim_honours_backoff_limit_and_order(db):
    db.tables["assistant_events"] = [
        event_row(id=f"0000000{i}-0000-4000-8000-000000000000", dedupe_key=f"p7:e{i}",
                  occurred_at=iso(minutes=i)) for i in range(5)
    ] + [event_row(id=ITEM, dedupe_key="p7:later", next_attempt_at=iso(minutes=30))]
    got = repo.events_claim(None, worker="w", limit=3, now=at(minutes=10))
    assert [e["dedupe_key"] for e in got] == ["p7:e0", "p7:e1", "p7:e2"]
    assert repo.events_claim(None, worker="w", limit=50, now=at(minutes=10))[-1]["dedupe_key"] != "p7:later"
    db.locked_events.add(ITEM)
    one(db, "assistant_events", id=ITEM)["next_attempt_at"] = None
    assert repo.events_claim(None, worker="w", limit=50, now=at(minutes=11)) == []


def test_two_workers_never_claim_the_same_event(db):
    db.tables["assistant_events"] = [event_row(id=f"0000000{i}-0000-4000-8000-000000000000",
                                               dedupe_key=f"p7:e{i}", occurred_at=iso(seconds=i)) for i in range(8)]
    with ThreadPoolExecutor(2) as pool:
        a, b = pool.map(lambda w: repo.events_claim(None, worker=w, limit=5, now=at(minutes=1)), ["wa", "wb"])
    ids = [e["id"] for e in a + b]
    assert len(ids) == len(set(ids)) == 8


def test_event_finish_is_fenced_on_the_claim_and_retries_with_backoff(db):
    db.tables["assistant_events"] = [event_row()]
    repo.events_claim(None, worker="w1", limit=1, now=at())
    assert repo.event_finish(None, EVENT_ID, worker="w2", status="processed", now=at()) is False
    assert repo.event_finish(None, EVENT_ID, worker="w1", status="retry", error="boom", retry_seconds=60,
                             now=at(seconds=1)) is True
    e = one(db, "assistant_events", id=EVENT_ID)
    assert e["status"] == "pending" and e["claimed_by"] is None and e["last_error"] == "boom"
    assert e["next_attempt_at"] == iso(seconds=61)
    assert repo.events_claim(None, worker="w1", limit=1, now=at(seconds=30)) == []
    assert repo.events_claim(None, worker="w1", limit=1, now=at(seconds=62))[0]["attempts"] == 2
    assert repo.event_finish(None, EVENT_ID, worker="w1", status="processed", routed={"pushed": 2},
                             now=at(seconds=63)) is True
    e = one(db, "assistant_events", id=EVENT_ID)
    assert e["status"] == "processed" and e["routed"] == {"pushed": 2} and e["processed_at"] == iso(seconds=63)
    assert repo.event_finish(None, EVENT_ID, worker="w1", status="processed") is False  # no longer claimed
    with pytest.raises(ValueError):
        repo.event_finish(None, EVENT_ID, worker="w1", status="done")


def test_event_retry_dead_letters_at_five_attempts(db):
    db.tables["assistant_events"] = [event_row(status="claimed", claimed_by="w1", claimed_at=iso(), attempts=5)]
    assert repo.event_finish(None, EVENT_ID, worker="w1", status="retry", error="x", now=at()) is True
    e = one(db, "assistant_events", id=EVENT_ID)
    assert e["status"] == "failed" and e["processed_at"] == iso() and e["claimed_by"] == "w1"


EVENT_ID = fk.EVENT


# ── Inbox rows ───────────────────────────────────────────────────────────────


def _router_row(**over) -> dict:
    base = {"dedupe_key": "p7:todo.due:t1:2026-10-30", "event_type": "todo.due", "severity": "high",
            "title": "A to-do is due today", "why": "You own it.", "delivery": "immediate",
            "deliver_after": at(), "event_id": EVENT_ID}
    base.update(over)
    return base


def test_insert_inbox_row_is_per_person_and_ignores_a_duplicate(db):
    made = repo.insert_inbox_row(USER, _router_row(), now=at())
    assert made["user_id"] == USER and made["created_at"] == iso() and made["delivery"] == "immediate"
    assert repo.insert_inbox_row(USER, _router_row(), now=at()) is None
    other = repo.insert_inbox_row(P7_OTHER, _router_row(), now=at())
    assert other["user_id"] == P7_OTHER and other["id"] != made["id"]
    assert repo.inbox_row(USER, made["id"])["dedupe_key"] == made["dedupe_key"]
    assert repo.inbox_row(P7_OTHER, made["id"]) is None
    assert repo.inbox_row_by_key(P7_OTHER, made["dedupe_key"])["id"] == other["id"]


def test_inbox_rows_by_keys_is_per_person_batched_and_skips_missing_keys(db, monkeypatch):
    mine = [repo.insert_inbox_row(USER, _router_row(dedupe_key=f"p7:todo.due:t{n}:2026-10-30"), now=at())
            for n in range(3)]
    other = repo.insert_inbox_row(P7_OTHER, _router_row(dedupe_key="p7:todo.due:t0:2026-10-30"), now=at())
    reads: list[str] = []
    real = db.table
    monkeypatch.setattr(db, "table", lambda name: reads.append(name) or real(name))
    keys = [r["dedupe_key"] for r in mine] + ["p7:todo.due:missing:2026-10-30", mine[0]["dedupe_key"], ""]
    found = repo.inbox_rows_by_keys(USER, keys)
    assert reads == ["assistant_inbox"]                   # one read for the whole page
    assert {k: r["id"] for k, r in found.items()} == {r["dedupe_key"]: r["id"] for r in mine}
    theirs = repo.inbox_rows_by_keys(P7_OTHER, keys)
    assert {k: r["id"] for k, r in theirs.items()} == {other["dedupe_key"]: other["id"]}
    reads.clear()
    assert repo.inbox_rows_by_keys(USER, []) == {} and reads == []


def test_inbox_rows_by_keys_reads_one_chunk_per_hundred_keys(db, monkeypatch):
    reads: list[str] = []
    real = db.table
    monkeypatch.setattr(db, "table", lambda name: reads.append(name) or real(name))
    repo.inbox_rows_by_keys(USER, [f"p7:x:{n}" for n in range(150)])
    assert reads == ["assistant_inbox", "assistant_inbox"]


def test_the_inbox_list_links_escalations_with_one_batched_read(db, monkeypatch):
    """WI9-3: _triage_for reads the escalation rows of a page of escalated
    items in one call, never one inbox_row_by_key per item."""
    from types import SimpleNamespace

    from app.assistant import proactive
    from app.routers import assistant_inbox as inbox_router

    ids = [f"9d9d9d9d-9d9d-49d9-89d9-{n:012d}" for n in range(50)]
    found = {i: {"inbox_id": i, "status": "decided", "decision": "escalate"} for i in ids}
    found["9d9d9d9d-9d9d-49d9-89d9-999999999999"] = {"status": "decided", "decision": "none"}
    monkeypatch.setattr(repo, "triage_items_for_inbox", lambda user_id, inbox_ids: dict(found))
    monkeypatch.setattr(repo, "inbox_row_by_key", lambda *a, **k: pytest.fail("one read per item"))
    batches: list[list[str]] = []

    def by_keys(user_id, keys):
        assert user_id == USER
        batches.append(list(keys))
        key = proactive.event_dedupe_key(proactive.TRIAGE_ESCALATION, {"inbox_id": ids[7]})
        return {key: {"id": INBOX2, "dedupe_key": key}}

    monkeypatch.setattr(repo, "inbox_rows_by_keys", by_keys)
    rows = [{"id": i, "delivery": "record"} for i in found]
    got, links = inbox_router._triage_for(SimpleNamespace(id=USER), rows)
    assert len(batches) == 1 and len(batches[0]) == 50
    assert got == found and links == {ids[7]: INBOX2}

    def boom(user_id, keys):
        raise RuntimeError("inbox read failed")

    monkeypatch.setattr(repo, "inbox_rows_by_keys", boom)
    got, links = inbox_router._triage_for(SimpleNamespace(id=USER), rows)
    assert got == found and links == {}                    # the links are optional


@pytest.mark.parametrize("override", [
    {"dedupe_key": "calendar:x"}, {"delivery": "direct"}, {"delivery": "immediate", "deliver_after": None},
    {"delivery": "digest", "deliver_after": None}, {"title": ""},
])
def test_insert_inbox_row_refuses_what_the_router_does_not_own(db, override):
    with pytest.raises(ValueError):
        repo.insert_inbox_row(USER, _router_row(**override))
    assert rows(db, "assistant_inbox") == []


def test_a_record_row_needs_no_deliver_after(db):
    made = repo.insert_inbox_row(USER, {
        "dedupe_key": "p7:bell:" + BELL, "event_type": "bell.quote_received", "severity": "normal",
        "title": "A quote arrived", "delivery": "record", "source_notification_id": BELL, "delivered_at": at()},
        now=at())
    assert made["deliver_after"] is None and made["source_notification_id"] == BELL


def test_a_record_row_is_delivered_by_definition(db):
    made = repo.insert_inbox_row(USER, {
        "dedupe_key": "p7:bell:" + BELL, "event_type": "bell.quote_received", "severity": "normal",
        "title": "A quote arrived", "delivery": "record", "source_notification_id": BELL}, now=at(minutes=2))
    assert made["delivered_at"] == iso(minutes=2)
    assert [r["id"] for r in repo.inbox_list(USER)] == [made["id"]]
    kept = repo.insert_inbox_row(USER, {
        "dedupe_key": "p7:bell:" + BELL2, "event_type": "bell.quote_received", "severity": "normal",
        "title": "A quote arrived", "delivery": "record", "source_notification_id": BELL2,
        "delivered_at": at(minutes=1)}, now=at(minutes=2))
    assert kept["delivered_at"] == iso(minutes=1)


def test_the_release_step_finds_due_rows_a_crashed_worker_left(db):
    repo.insert_inbox_row(USER, _router_row(), now=at())
    repo.insert_inbox_row(USER, _router_row(dedupe_key="p7:later", deliver_after=at(hours=11)), now=at())
    repo.insert_inbox_row(USER, _router_row(dedupe_key="p7:digest", delivery="digest"), now=at())
    due = repo.inbox_due_immediate(None, now=at(minutes=1))
    assert [r["dedupe_key"] for r in due] == ["p7:todo.due:t1:2026-10-30"]
    assert len(repo.inbox_due_immediate(USER, now=at(minutes=1), dedupe_key="p7:todo.due:t1:2026-10-30")) == 1
    assert repo.inbox_due_immediate(USER, now=at(minutes=1), dedupe_key="p7:nope") == []
    assert [r["dedupe_key"] for r in repo.inbox_due_immediate(None, now=at(hours=12))][-1] == "p7:later"


def test_inbox_deliver_rings_one_bell_and_one_teams_row_exactly_once(db):
    db.tables["assistant_inbox"] = [inbox_row()]
    out = repo.inbox_deliver(USER, INBOX, bell_message="A to-do is due today.", bell_metadata={"event_type": "todo.due"},
                             teams=True, now=at(minutes=1))
    assert out["delivered"] is True and out["notification_id"] and out["teams_send_id"]
    bell = one(db, "notifications", id=out["notification_id"])
    assert bell["type"] == "assistant.inbox" and bell["user_id"] == USER and bell["created_at"] == iso(minutes=1)
    assert bell["metadata"] == {"event_type": "todo.due", "inbox_id": INBOX, "actor_kind": "system"}
    row = one(db, "assistant_inbox", id=INBOX)
    assert row["delivered_at"] == iso(minutes=1) and row["notification_id"] == out["notification_id"]
    send = one(db, "assistant_external_sends", id=out["teams_send_id"])
    assert (send["target_kind"], send["channel"], send["status"]) == ("inbox", "teams", "pending")
    assert send["not_before"] == iso(minutes=1) and send["created_at"] == iso(minutes=1)
    # A lost fence is normal: nothing more is written.
    assert repo.inbox_deliver(USER, INBOX, bell_message="again", teams=True, now=at(minutes=2)) == {"delivered": False}
    assert len(rows(db, "notifications")) == 1 and len(rows(db, "assistant_external_sends")) == 1


def test_inbox_deliver_holds_a_future_row_and_defers_teams_to_the_hold_end(db):
    db.tables["assistant_inbox"] = [inbox_row(deliver_after=iso(hours=1))]
    assert repo.inbox_deliver(USER, INBOX, bell_message="m", now=at())["delivered"] is False
    out = repo.inbox_deliver(USER, INBOX, bell_message="m", teams=True, teams_not_before=at(hours=12),
                             now=at(hours=2))
    assert one(db, "assistant_external_sends", id=out["teams_send_id"])["not_before"] == iso(hours=12)
    assert repo.inbox_deliver(USER, INBOX, bell_message="m", teams=True, now=at(hours=3)) == {"delivered": False}


def test_inbox_deliver_without_a_bell_or_teams_and_for_the_wrong_person(db):
    db.tables["assistant_inbox"] = [inbox_row()]
    assert repo.inbox_deliver(P7_OTHER, INBOX, bell_message="m", now=at()) == {"delivered": False}
    out = repo.inbox_deliver(USER, INBOX, bell_message=None, now=at())
    assert out == {"delivered": True, "notification_id": None, "teams_send_id": None}
    assert rows(db, "notifications") == [] and rows(db, "assistant_external_sends") == []


def test_two_workers_deliver_a_row_exactly_once(db):
    db.tables["assistant_inbox"] = [inbox_row()]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: repo.inbox_deliver(USER, INBOX, bell_message="m", teams=True, now=at()),
                                range(2)))
    assert sorted(r["delivered"] for r in results) == [False, True]
    assert len(rows(db, "notifications")) == 1 and len(rows(db, "assistant_external_sends")) == 1


def test_a_crash_after_the_insert_delivers_once_on_a_later_tick(db):
    row = repo.insert_inbox_row(USER, _router_row(), now=at())  # worker A dies here
    for _ in range(2):  # both workers run the release step
        for due in repo.inbox_due_immediate(None, now=at(seconds=40)):
            repo.inbox_deliver(due["user_id"], due["id"], bell_message="m", teams=True, now=at(seconds=40))
    assert row["delivered_at"] is None
    assert len(rows(db, "notifications")) == 1 and len(rows(db, "assistant_external_sends")) == 1


def _digest_args(**over):
    base = dict(digest_date=date(2026, 10, 30), slot="am", slot_at=at(), worker="wa", email=True, now=at(minutes=1))
    base.update(over)
    return base


def test_digest_deliver_attaches_due_rows_rings_one_bell_and_one_email_row(db):
    db.tables["assistant_inbox"] = [
        inbox_row(id=INBOX, delivery="digest", dedupe_key="p7:a"),
        inbox_row(id=INBOX2, delivery="digest", dedupe_key="p7:b", deliver_after=iso(hours=6)),  # not due yet
        inbox_row(id="b3b3b3b3-b3b3-4b3b-8b3b-b3b3b3b3b3b3", delivery="digest", dedupe_key="p7:c",
                  dismissed_at=iso(), dismissed_reason="user"),
        inbox_row(id="b4b4b4b4-b4b4-4b4b-8b4b-b4b4b4b4b4b4", delivery="digest", dedupe_key="p7:d", user_id=P7_OTHER),
    ]
    out = repo.digest_deliver(USER, **_digest_args())
    assert out["claimed"] is True and out["item_count"] == 1 and out["notification_id"] and out["email_send_id"]
    assert one(db, "assistant_inbox", id=INBOX)["digest_id"] == out["digest_id"]
    assert one(db, "assistant_inbox", id=INBOX2)["digest_id"] is None
    assert one(db, "assistant_inbox", dedupe_key="p7:d")["digest_id"] is None
    bell = one(db, "notifications", id=out["notification_id"])
    assert bell["message"] == "Your 7:00 AM digest: 1 item." and bell["metadata"]["digest_id"] == out["digest_id"]
    send = one(db, "assistant_external_sends", id=out["email_send_id"])
    assert (send["target_kind"], send["channel"], send["target_id"]) == ("digest", "email", out["digest_id"])
    ledger = repo.digest_for_slot(USER, date(2026, 10, 30), "am")
    assert ledger["item_count"] == 1 and ledger["since_at"] == iso(days=-3, minutes=1)
    assert repo.digest(USER, ledger["id"])["id"] == ledger["id"] and repo.digest(P7_OTHER, ledger["id"]) is None
    assert [r["id"] for r in repo.inbox_digest_items(USER, ledger["id"])] == [INBOX]
    # The ledger is the claim: a second call, from either worker, does nothing.
    assert repo.digest_deliver(USER, **_digest_args(worker="wb", now=at(minutes=2))) == {"claimed": False}
    assert len(rows(db, "assistant_digests")) == 1 and len(rows(db, "notifications")) == 1


def test_an_empty_slot_writes_nothing_and_a_digest_without_email_adds_no_send_row(db):
    assert repo.digest_deliver(USER, **_digest_args()) == {"claimed": False}
    assert rows(db, "assistant_digests") == [] and rows(db, "notifications") == []
    db.tables["assistant_inbox"] = [inbox_row(delivery="digest")]
    out = repo.digest_deliver(USER, **_digest_args(email=False, slot="pm", digest_date=date(2026, 10, 30)))
    assert out["claimed"] and out["email_send_id"] is None and rows(db, "assistant_external_sends") == []
    assert one(db, "notifications", id=out["notification_id"])["message"] == "Your 1:00 PM digest: 1 item."
    with pytest.raises(Exception, match="bad arguments"):
        repo.digest_deliver(USER, **_digest_args(slot="noon"))


def test_digest_counts_record_rows_whose_source_bell_is_unread_and_undismissed(db):
    db.tables["notifications"] = [
        bell_row(id=BELL), bell_row(id=BELL2, read_at=iso(hours=-1)),
        bell_row(id="a3a3a3a3-a3a3-4a3a-8a3a-a3a3a3a3a3a3", dismissed_at=iso(hours=-1)),
    ]
    rec = dict(delivery="record", deliver_after=None, created_at=iso(hours=-2), delivered_at=iso(hours=-2),
               severity="normal")
    db.tables["assistant_inbox"] = [
        inbox_row(delivery="digest"),
        inbox_row(id=INBOX2, dedupe_key="p7:bell:1", source_notification_id=BELL, **rec),
        inbox_row(id="b5b5b5b5-b5b5-4b5b-8b5b-b5b5b5b5b5b5", dedupe_key="p7:bell:2", source_notification_id=BELL2, **rec),
        inbox_row(id="b6b6b6b6-b6b6-4b6b-8b6b-b6b6b6b6b6b6", dedupe_key="p7:bell:3",
                  source_notification_id="a3a3a3a3-a3a3-4a3a-8a3a-a3a3a3a3a3a3", **rec),
    ]
    assert repo.digest_deliver(USER, **_digest_args())["record_count"] == 1


def test_the_since_window_is_the_later_of_the_last_rung_digest_and_three_days(db):
    db.tables["assistant_digests"] = [
        digest_row(id="d2d2d2d2-d2d2-4d2d-8d2d-d2d2d2d2d2d2", digest_date="2026-10-29", slot="pm",
                   created_at=iso(days=-1), notification_id=BELL),
        digest_row(id="d3d3d3d3-d3d3-4d3d-8d3d-d3d3d3d3d3d3", digest_date="2026-10-29", slot="am",
                   created_at=iso(hours=-3), notification_id=None),  # an empty slot did not ring
    ]
    db.tables["assistant_inbox"] = [inbox_row(delivery="digest")]
    out = repo.digest_deliver(USER, **_digest_args())
    assert repo.digest(USER, out["digest_id"])["since_at"] == iso(days=-1)


def test_two_workers_deliver_one_digest_per_person_and_slot(db):
    db.tables["assistant_inbox"] = [inbox_row(delivery="digest")]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda w: repo.digest_deliver(USER, **_digest_args(worker=w)), ["wa", "wb"]))
    assert sorted(r["claimed"] for r in results) == [False, True]
    assert len(rows(db, "assistant_digests")) == 1 and len(rows(db, "notifications")) == 1
    assert len(rows(db, "assistant_external_sends")) == 1


def test_deliver_digest_and_fold_share_one_per_user_lock(db):
    db.tables["assistant_inbox"] = [inbox_row(), inbox_row(id=INBOX2, dedupe_key="p7:b", delivery="digest")]
    repo.inbox_deliver(USER, INBOX, bell_message="m", now=at())
    repo.digest_deliver(USER, **_digest_args())
    repo.inbox_fold(USER, INBOX2)
    key = f"assistant_inbox_deliver:{USER}"
    assert db.advisory_locks == [key, key, key]
    repo.inbox_deliver(P7_OTHER, INBOX, bell_message="m", now=at())
    assert db.advisory_locks[-1] == f"assistant_inbox_deliver:{P7_OTHER}"


@pytest.mark.parametrize("call", [
    lambda: repo.inbox_deliver(USER, INBOX, bell_message="m", now=at()),
    lambda: repo.digest_deliver(USER, **_digest_args()),
    lambda: repo.inbox_fold(USER, INBOX, now=at()),
], ids=["deliver", "digest", "fold"])
def test_each_waits_for_the_per_user_lock(db, call):
    db.tables["assistant_inbox"] = [inbox_row(delivery="digest")]
    done = threading.Event()
    lock = db.user_lock(USER)
    lock.acquire()
    try:
        worker = threading.Thread(target=lambda: (call(), done.set()))
        worker.start()
        assert not done.wait(0.3)
    finally:
        lock.release()
    assert done.wait(5)
    worker.join()


def _held(**over):
    """A high row held by quiet hours: created at 02:00 PT, ringing at 06:00 PT."""
    base = dict(created_at=iso(hours=-5), deliver_after=iso(hours=-1))
    base.update(over)
    return inbox_row(**base)


def test_fold_moves_a_held_row_into_the_am_digest_only_while_no_am_ledger_row_exists(db):
    db.tables["assistant_inbox"] = [_held()]
    out = repo.inbox_fold(USER, INBOX, am_date=date(2026, 10, 30), am_slot_at=at(hours=1), fold_until=at(hours=3),
                          now=at())
    assert out == "folded"
    row = one(db, "assistant_inbox", id=INBOX)
    assert (row["delivery"], row["precedence"], row["deliver_after"]) == ("digest", "quiet_hours", iso(hours=1))
    assert repo.inbox_folded_pending(None)[0]["id"] == INBOX
    # Not yet time: wait.
    assert repo.inbox_fold(USER, INBOX, am_date=date(2026, 10, 30), am_slot_at=at(hours=1),
                           fold_until=at(hours=3), now=at()) == "wait"
    # The window closed with no AM digest: back to immediate, delivered at once.
    assert repo.inbox_fold(USER, INBOX, am_date=date(2026, 10, 30), am_slot_at=at(hours=1),
                           fold_until=at(hours=3), now=at(hours=4)) == "deliver"
    row = one(db, "assistant_inbox", id=INBOX)
    assert (row["delivery"], row["deliver_after"]) == ("immediate", iso(hours=4))
    assert repo.inbox_folded_pending(None) == []


def test_a_held_row_is_delivered_not_folded_once_the_am_digest_went_out(db):
    db.tables["assistant_inbox"] = [_held()]
    db.tables["assistant_digests"] = [digest_row()]
    assert repo.inbox_fold(USER, INBOX, am_date=date(2026, 10, 30), am_slot_at=at(hours=1),
                           fold_until=at(hours=3), now=at()) == "deliver"
    assert one(db, "assistant_inbox", id=INBOX)["delivery"] == "immediate"
    # A folded row whose AM digest went out without it returns to immediate.
    db.tables["assistant_inbox"] = [_held(delivery="digest", precedence="quiet_hours", deliver_after=iso(hours=1))]
    assert repo.inbox_fold(USER, INBOX, am_date=date(2026, 10, 30), am_slot_at=at(hours=1),
                           fold_until=at(hours=3), now=at()) == "deliver"


@pytest.mark.parametrize("prior", ["R17", "R4b", None])
def test_fold_keeps_the_prior_precedence_and_the_unfold_restores_it(db, prior):
    """0191: the fold parks the row's precedence in the payload; while folded
    the row still reads quiet_hours (the un-fold selector and the digest's
    High line); the un-fold puts the rule back and removes the key."""
    db.tables["assistant_inbox"] = [_held(precedence=prior, payload={"todo_id": "t1"})]
    assert repo.inbox_fold(USER, INBOX, am_date=date(2026, 10, 30), am_slot_at=at(hours=1),
                           fold_until=at(hours=3), now=at()) == "folded"
    row = one(db, "assistant_inbox", id=INBOX)
    assert row["precedence"] == "quiet_hours"
    assert row["payload"] == {"todo_id": "t1", "folded_from_precedence": prior}
    assert [r["id"] for r in repo.inbox_folded_pending(None)] == [INBOX]
    assert repo.inbox_fold(USER, INBOX, am_date=date(2026, 10, 30), am_slot_at=at(hours=1),
                           fold_until=at(hours=3), now=at(hours=4)) == "deliver"
    row = one(db, "assistant_inbox", id=INBOX)
    assert (row["delivery"], row["precedence"]) == ("immediate", prior)
    assert row["payload"] == {"todo_id": "t1"}
    assert repo.inbox_folded_pending(None) == []


def test_a_row_folded_before_0191_keeps_quiet_hours_on_unfold(db):
    db.tables["assistant_inbox"] = [_held(delivery="digest", precedence="quiet_hours", deliver_after=iso(hours=1),
                                          payload={"todo_id": "t1"})]
    assert repo.inbox_fold(USER, INBOX, am_date=date(2026, 10, 30), am_slot_at=at(hours=1),
                           fold_until=at(hours=3), now=at(hours=4)) == "deliver"
    row = one(db, "assistant_inbox", id=INBOX)
    assert (row["delivery"], row["precedence"], row["payload"]) == ("immediate", "quiet_hours", {"todo_id": "t1"})


@pytest.mark.parametrize("override", [
    {"created_at": iso(), "deliver_after": iso()},        # a crash leftover is never held
    {"severity": "urgent"},                               # urgent never folds
    {"event_type": "assistant.approval_expiring"},        # nor an approval warning
    {"severity": "normal"},
    {"dismissed_at": iso(), "dismissed_reason": "user"},
])
def test_only_a_held_high_row_folds(db, override):
    base = dict(created_at=iso(hours=-5), deliver_after=iso(hours=-1))
    base.update(override)
    db.tables["assistant_inbox"] = [inbox_row(**base)]
    out = repo.inbox_fold(USER, INBOX, am_date=date(2026, 10, 30), am_slot_at=at(hours=1), fold_until=at(hours=3),
                          now=at())
    assert out in ("deliver", "none") and one(db, "assistant_inbox", id=INBOX)["delivery"] == "immediate"
    assert repo.inbox_fold(USER, "00000000-0000-4000-8000-00000000dead", now=at()) == "none"


# ── Feedback, read, snooze, dismiss ──────────────────────────────────────────


def _delivered(**over):
    base = dict(delivered_at=iso(minutes=-5), notification_id=BELL)
    base.update(over)
    return inbox_row(**base)


def test_mark_read_clears_the_router_bell_and_never_a_record_rows_source_bell(db):
    db.tables["notifications"] = [bell_row(id=BELL), bell_row(id=BELL2)]
    db.tables["assistant_inbox"] = [
        _delivered(),
        inbox_row(id=INBOX2, dedupe_key="p7:bell:x", delivery="record", deliver_after=None, delivered_at=iso(),
                  source_notification_id=BELL2),
    ]
    assert repo.inbox_mark_read(USER, None, now=at()) == 1
    assert one(db, "assistant_inbox", id=INBOX)["read_at"] == iso()
    assert one(db, "notifications", id=BELL)["read_at"] == iso()
    assert one(db, "notifications", id=BELL2)["read_at"] is None
    assert one(db, "assistant_inbox", id=INBOX2)["read_at"] is None
    assert repo.inbox_mark_read(USER, [INBOX]) == 0
    assert repo.inbox_mark_read(P7_OTHER, None, now=at()) == 0


def test_feedback_useful_and_not_useful_store_the_value(db):
    db.tables["assistant_inbox"] = [_delivered()]
    out = repo.inbox_feedback(USER, INBOX, "useful", now=at())
    assert out == {"ok": True, "feedback": "useful", "event_type": "todo.due", "auto_downgraded": False}
    assert one(db, "assistant_inbox", id=INBOX)["feedback_at"] == iso()
    assert repo.inbox_feedback(USER, INBOX, "great")["code"] == "invalid_feedback"
    assert repo.inbox_feedback(P7_OTHER, INBOX, "useful")["code"] == "not_found"


def test_mute_writes_the_user_override_and_dismisses_pending_rows_of_the_kind(db):
    db.tables["assistant_inbox"] = [
        _delivered(),
        inbox_row(id=INBOX2, dedupe_key="p7:other", delivery="digest"),                       # pending: dismissed
        inbox_row(id="b7b7b7b7-b7b7-4b7b-8b7b-b7b7b7b7b7b7", dedupe_key="p7:x", event_type="bid.due_not_ready"),
    ]
    out = repo.inbox_feedback(USER, INBOX, "mute", now=at())
    assert out["ok"] is True
    sub = one(db, "assistant_subscriptions", user_id=USER, event_type="todo.due")
    assert (sub["enabled"], sub["source"], sub["role"], sub["severity"]) == (False, "user", None, "normal")
    pending = one(db, "assistant_inbox", id=INBOX2)
    assert (pending["dismissed_reason"], pending["dismissed_at"]) == ("muted", iso())
    assert one(db, "assistant_inbox", dedupe_key="p7:x")["dismissed_at"] is None
    assert one(db, "assistant_inbox", id=INBOX)["dismissed_at"] is None  # the delivered row stays
    # Muting again updates the one row (the partial unique index).
    db.tables["assistant_subscriptions"][0]["enabled"] = True
    repo.inbox_feedback(USER, INBOX, "mute", now=at(minutes=1))
    assert len(rows(db, "assistant_subscriptions")) == 1 and rows(db, "assistant_subscriptions")[0]["enabled"] is False


@pytest.mark.parametrize("override", [
    {"delivery": "direct", "deliver_after": None, "event_type": "assistant.calendar"},
    {"event_type": "assistant.digest"},
])
def test_mute_refuses_direct_rows_and_assistant_kinds_but_allows_approval_warnings(db, override):
    db.tables["assistant_inbox"] = [_delivered(**override)]
    assert repo.inbox_feedback(USER, INBOX, "mute")["code"] == "not_mutable"
    assert rows(db, "assistant_subscriptions") == []
    db.tables["assistant_inbox"] = [_delivered(event_type="assistant.approval_expiring")]
    assert repo.inbox_feedback(USER, INBOX, "mute")["ok"] is True


def test_mute_project_adds_the_mute_and_dismisses_the_projects_pending_rows(db):
    db.tables["assistant_inbox"] = [
        _delivered(project_id=PROJECT),
        inbox_row(id=INBOX2, dedupe_key="p7:p", delivery="digest", project_id=PROJECT),
        inbox_row(id="b8b8b8b8-b8b8-4b8b-8b8b-b8b8b8b8b8b8", dedupe_key="p7:q", project_id=fk.PROJECT2),
    ]
    assert repo.inbox_feedback(USER, INBOX, "mute_project", now=at())["ok"] is True
    assert [m["project_id"] for m in repo.project_mutes(USER)] == [PROJECT]
    assert repo.project_mutes(P7_OTHER) == []
    assert one(db, "assistant_inbox", id=INBOX2)["dismissed_reason"] == "muted"
    assert one(db, "assistant_inbox", dedupe_key="p7:q")["dismissed_at"] is None
    repo.inbox_feedback(USER, INBOX, "mute_project", now=at(minutes=1))
    assert len(repo.project_mutes(USER)) == 1
    db.tables["assistant_inbox"] = [_delivered(project_id=None)]
    assert repo.inbox_feedback(USER, INBOX, "mute_project")["code"] == "not_mutable"


def test_the_third_not_useful_mark_in_fourteen_days_moves_the_kind_to_inbox_only_once(db):
    ids = [f"c{i}c{i}c{i}c{i}-c{i}c{i}-4c{i}c-8c{i}c-c{i}c{i}c{i}c{i}c{i}c{i}" for i in range(4)]
    db.tables["assistant_inbox"] = [_delivered(id=i, dedupe_key=f"p7:t{n}") for n, i in enumerate(ids)]
    assert [repo.inbox_feedback(USER, ids[n], "not_useful", now=at(days=n))["auto_downgraded"] for n in range(3)] \
        == [False, False, True]
    sub = one(db, "assistant_subscriptions", user_id=USER)
    assert (sub["enabled"], sub["severity"], sub["source"]) == (True, "low", "feedback_auto")
    assert repo.inbox_feedback(USER, ids[3], "not_useful", now=at(days=3))["auto_downgraded"] is False
    assert len(rows(db, "assistant_subscriptions")) == 1


def test_not_useful_marks_older_than_fourteen_days_do_not_count(db):
    ids = [f"c{i}c{i}c{i}c{i}-c{i}c{i}-4c{i}c-8c{i}c-c{i}c{i}c{i}c{i}c{i}c{i}" for i in range(3)]
    db.tables["assistant_inbox"] = [_delivered(id=i, dedupe_key=f"p7:t{n}") for n, i in enumerate(ids)]
    for n in range(3):
        out = repo.inbox_feedback(USER, ids[n], "not_useful", now=at(days=n * 8))
    assert out["auto_downgraded"] is False and rows(db, "assistant_subscriptions") == []


def test_snooze_returns_the_row_unread_clears_its_bell_and_stops_at_three(db):
    db.tables["notifications"] = [bell_row(id=BELL)]
    db.tables["assistant_inbox"] = [_delivered(read_at=iso(minutes=-1))]
    out = repo.inbox_snooze(USER, INBOX, at(hours=6), now=at())
    assert out["ok"] is True and out["snooze_count"] == 1
    row = one(db, "assistant_inbox", id=INBOX)
    assert (row["delivery"], row["delivered_at"], row["read_at"], row["notification_id"], row["precedence"],
            row["deliver_after"]) == ("digest", None, None, None, "snoozed", iso(hours=6))
    assert one(db, "notifications", id=BELL)["read_at"] == iso()
    assert repo.inbox_snooze(USER, INBOX, at(hours=7), now=at(hours=1))["code"] == "not_mutable"  # not delivered
    for n in range(2):
        row.update(delivered_at=iso(), delivery="immediate")
        assert repo.inbox_snooze(USER, INBOX, at(days=n + 1), now=at(minutes=n + 1))["ok"] is True
    row.update(delivered_at=iso(), delivery="immediate")
    assert repo.inbox_snooze(USER, INBOX, at(days=9), now=at(hours=1))["code"] == "snooze_limit"
    assert repo.inbox_snooze(P7_OTHER, INBOX, at(days=9), now=at(hours=1))["code"] == "not_found"
    with pytest.raises(Exception, match="bad slot"):
        repo.inbox_snooze(USER, INBOX, at(minutes=-1), now=at())


def test_dismiss_marks_the_router_bell_read_and_leaves_a_record_rows_source_bell(db):
    db.tables["notifications"] = [bell_row(id=BELL), bell_row(id=BELL2)]
    db.tables["assistant_inbox"] = [
        _delivered(),
        inbox_row(id=INBOX2, dedupe_key="p7:bell:y", delivery="record", deliver_after=None, delivered_at=iso(),
                  source_notification_id=BELL2),
    ]
    assert repo.inbox_dismiss(USER, [INBOX, INBOX2], "user", now=at()) == 2
    assert one(db, "notifications", id=BELL)["read_at"] == iso()
    assert one(db, "notifications", id=BELL2)["read_at"] is None
    assert one(db, "assistant_inbox", id=INBOX2)["dismissed_reason"] == "user"
    assert repo.inbox_dismiss(USER, [INBOX], "user") == 0
    assert repo.inbox_dismiss(P7_OTHER, [INBOX], "user") == 0
    assert repo.inbox_dismiss(USER, [], "user") == 0
    with pytest.raises(ValueError):
        repo.inbox_dismiss(USER, [INBOX], "bored")


def test_supersede_dismisses_only_older_pending_due_not_ready_rows_of_the_same_project(db):
    mine = dict(event_type="bid.due_not_ready", project_id=PROJECT)
    db.tables["assistant_inbox"] = [
        inbox_row(dedupe_key="p7:old", delivery="digest", **mine),
        inbox_row(id=INBOX2, dedupe_key="p7:new", delivery="digest", **mine),
        inbox_row(id="b9b9b9b9-b9b9-4b9b-8b9b-b9b9b9b9b9b9", dedupe_key="p7:rung", delivered_at=iso(), **mine),
        inbox_row(id="bababa01-baba-4aba-8aba-bababababa01", dedupe_key="p7:p2", event_type="bid.due_not_ready",
                  project_id=fk.PROJECT2),
        inbox_row(id="bababa02-baba-4aba-8aba-bababababa02", dedupe_key="p7:u2", user_id=P7_OTHER, **mine),
    ]
    assert repo.inbox_supersede_due_not_ready(USER, PROJECT, keep_dedupe_key="p7:new", now=at()) == 1
    states = {r["dedupe_key"]: r["dismissed_reason"] for r in rows(db, "assistant_inbox")}
    assert states == {"p7:old": "superseded", "p7:new": None, "p7:rung": None, "p7:p2": None, "p7:u2": None}


# ── Link bell (A11.11) ───────────────────────────────────────────────────────

KEY = "p7:bell:email:abc"


def _link(db_, bell, rank, when, title="T", **kw):
    return repo.inbox_link_bell(
        USER, KEY, notification_id=bell, bell_at=when, event_type="bell.quote_received" if rank < 2 else "bell.late_quote",
        rank=rank, title=title, why=None, payload={"notification_id": bell}, project_id=kw.get("project_id"),
        now=when)


def test_link_bell_creates_one_record_row_then_links_a_lower_rank_bell(db):
    first = _link(db, BELL, 2, at(), "Late quote")
    assert first["linked"] is False
    row = one(db, "assistant_inbox", id=first["inbox_id"])
    assert (row["delivery"], row["source_notification_id"], row["delivered_at"]) == ("record", BELL, iso())
    assert row["payload"]["rank"] == 2 and row["payload"]["linked_notification_ids"] == []
    out = _link(db, BELL2, 1, at(seconds=5), "Quote")
    assert out == {"inbox_id": first["inbox_id"], "linked": True, "source": False}
    row = one(db, "assistant_inbox", id=first["inbox_id"])
    assert row["title"] == "Late quote" and row["payload"]["linked_notification_ids"] == [BELL2]
    assert _link(db, BELL2, 1, at(seconds=5))["noop"] is True
    assert _link(db, BELL, 2, at())["noop"] is True
    assert len(rows(db, "assistant_inbox")) == 1


def test_a_higher_rank_bell_becomes_the_source_and_the_old_source_is_linked(db):
    first = _link(db, BELL, 1, at(), "Quote")
    out = _link(db, BELL2, 2, at(seconds=30), "Late quote")
    assert out["source"] is True
    row = one(db, "assistant_inbox", id=first["inbox_id"])
    assert (row["title"], row["source_notification_id"], row["event_type"]) == ("Late quote", BELL2, "bell.late_quote")
    assert row["payload"]["linked_notification_ids"] == [BELL] and row["payload"]["rank"] == 2
    assert row["created_at"] == iso()  # not later than 120 s: the row keeps its time


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_equal_rank_bells_from_one_ingest_resolve_to_the_earliest_whatever_the_order(db, order):
    bells = [(BELL, at(seconds=10)), (BELL2, at(seconds=40))]
    for n in order:
        _link(db, bells[n][0], 1, bells[n][1])
    row = rows(db, "assistant_inbox")[0]
    assert row["source_notification_id"] == BELL and row["payload"]["linked_notification_ids"] == [BELL2]


def test_an_equal_rank_bell_more_than_120_seconds_later_becomes_the_source_and_moves_created_at(db):
    first = _link(db, BELL, 1, at())
    db.tables["assistant_inbox"][0].update(dismissed_at=iso(), dismissed_reason="user")
    out = _link(db, BELL2, 1, at(seconds=300), "Quote again")
    assert out["source"] is True
    row = one(db, "assistant_inbox", id=first["inbox_id"])
    assert row["created_at"] == iso(seconds=300) and row["source_notification_id"] == BELL2
    assert (row["dismissed_at"], row["dismissed_reason"]) == (None, None)  # a user dismissal re-opens


def test_link_bell_refuses_bad_arguments_and_a_row_that_is_not_a_record(db):
    with pytest.raises(Exception, match="bad arguments"):
        repo.inbox_link_bell(USER, "p7:todo.due:x", notification_id=BELL, bell_at=at(), event_type="bell.x", rank=1,
                             title="T", why=None, payload=None, project_id=None)
    db.tables["assistant_inbox"] = [inbox_row(dedupe_key=KEY)]
    assert _link(db, BELL, 1, at())["code"] == "not_record"


@pytest.mark.parametrize("given,expected", [
    ("high", "high"), ("low", "low"), ("urgent", "urgent"), ("normal", "normal"),
    (None, "normal"), ("severe", "normal"), (3, "normal"),
])
def test_link_bell_takes_its_severity_from_the_payload(db, given, expected):
    """0191: the record row's severity comes from p_payload->>'severity' (an
    allowed value, else normal) and the key never lands in the stored payload."""
    payload = {"notification_id": BELL}
    if given is not None:
        payload["severity"] = given
    out = repo.inbox_link_bell(USER, KEY, notification_id=BELL, bell_at=at(), event_type="bell.late_quote", rank=2,
                               title="Late quote", why=None, payload=payload, project_id=None, now=at())
    row = one(db, "assistant_inbox", id=out["inbox_id"])
    assert row["severity"] == expected and "severity" not in row["payload"]


def test_link_bell_severity_follows_the_source(db):
    def link(bell, rank, when, severity):
        return repo.inbox_link_bell(USER, KEY, notification_id=bell, bell_at=when, event_type="bell.x", rank=rank,
                                    title="T", why=None, payload={"notification_id": bell, "severity": severity},
                                    project_id=None, now=when)

    first = link(BELL, 1, at(), "low")
    assert one(db, "assistant_inbox", id=first["inbox_id"])["severity"] == "low"
    assert link(BELL2, 2, at(seconds=30), "high")["source"] is True
    row = one(db, "assistant_inbox", id=first["inbox_id"])
    assert row["severity"] == "high" and "severity" not in row["payload"]
    # A linked (non-source) bell leaves the row's severity alone.
    assert link("00000000-0000-4000-8000-0000000000aa", 1, at(seconds=40), "urgent")["source"] is False
    assert one(db, "assistant_inbox", id=first["inbox_id"])["severity"] == "high"


@pytest.mark.parametrize("given,expected", [
    ("R13", "R13"), ("R4b", "R4b"), (None, None), ("quiet_hours", None), ("r13", None), ("R13 ", None),
    ("R" + "1" * 20, None), (13, None),
])
def test_link_bell_takes_its_precedence_rule_from_the_payload(db, given, expected):
    """S6-1 (0191): the record row's precedence comes from
    p_payload->>'precedence' (^R[0-9]+[a-z]?$, at most 16 characters, else
    null) and the key never lands in the stored payload."""
    payload = {"notification_id": BELL, "severity": "low"}
    if given is not None:
        payload["precedence"] = given
    out = repo.inbox_link_bell(USER, KEY, notification_id=BELL, bell_at=at(), event_type="bell.late_quote", rank=2,
                               title="Late quote", why=None, payload=payload, project_id=None, now=at())
    row = one(db, "assistant_inbox", id=out["inbox_id"])
    assert row["precedence"] == expected and "precedence" not in row["payload"]


def test_link_bell_precedence_follows_the_source(db):
    def link(bell, rank, when, rule):
        return repo.inbox_link_bell(USER, KEY, notification_id=bell, bell_at=when, event_type="bell.x", rank=rank,
                                    title="T", why=None, payload={"notification_id": bell, "precedence": rule},
                                    project_id=None, now=when)

    first = link(BELL, 1, at(), "R13")
    assert one(db, "assistant_inbox", id=first["inbox_id"])["precedence"] == "R13"
    assert link(BELL2, 2, at(seconds=30), "R12")["source"] is True
    row = one(db, "assistant_inbox", id=first["inbox_id"])
    assert row["precedence"] == "R12" and "precedence" not in row["payload"]
    # A linked (non-source) bell leaves the row's precedence alone.
    assert link("00000000-0000-4000-8000-0000000000aa", 1, at(seconds=40), "R26")["source"] is False
    row = one(db, "assistant_inbox", id=first["inbox_id"])
    assert row["precedence"] == "R12" and "precedence" not in row["payload"]


def test_linked_notification_ids_stop_at_twenty(db):
    _link(db, "00000000-0000-4000-8000-000000000000", 5, at())
    for n in range(1, 25):
        _link(db, f"00000000-0000-4000-8000-0000000000{n:02d}", 1, at(seconds=n))
    linked = rows(db, "assistant_inbox")[0]["payload"]["linked_notification_ids"]
    assert len(linked) == 20


# ── Reads for the routes ─────────────────────────────────────────────────────


def test_inbox_list_segments_and_scope(db):
    db.tables["assistant_inbox"] = [
        _delivered(created_at=iso(minutes=-3)),
        inbox_row(id=INBOX2, dedupe_key="p7:d", delivery="digest", delivered_at=iso(), digest_id=DIGEST,
                  read_at=iso(), created_at=iso(minutes=-2)),
        inbox_row(id="bbbb0001-bbbb-4bbb-8bbb-bbbbbbbbbbb1", dedupe_key="p7:undelivered"),
        inbox_row(id="bbbb0002-bbbb-4bbb-8bbb-bbbbbbbbbbb2", dedupe_key="p7:gone", delivered_at=iso(),
                  dismissed_at=iso(), dismissed_reason="user"),
        inbox_row(id="bbbb0003-bbbb-4bbb-8bbb-bbbbbbbbbbb3", dedupe_key="p7:theirs", user_id=P7_OTHER,
                  delivered_at=iso()),
    ]
    assert [r["id"] for r in repo.inbox_list(USER)] == [INBOX2, INBOX]
    assert [r["id"] for r in repo.inbox_list(USER, segment="unread")] == [INBOX]
    assert [r["id"] for r in repo.inbox_list(USER, segment="digests")] == [INBOX2]
    assert len(repo.inbox_list(USER, limit=1)) == 1
    with pytest.raises(ValueError):
        repo.inbox_list(USER, segment="bogus")
    assert [r["id"] for r in repo.inbox_unread_rows(USER)] == [INBOX]
    assert [r["id"] for r in repo.inbox_pending_digest(USER, slot_at=at())] == []


def test_inbox_list_pages_with_the_keyset_cursor(db):
    ids = [f"cccc{n:04d}-cccc-4ccc-8ccc-cccccccccccc" for n in range(5)]
    db.tables["assistant_inbox"] = [
        _delivered(id=ids[n], dedupe_key=f"p7:page:{n}", created_at=iso(minutes=-n)) for n in range(5)
    ] + [_delivered(id="cccc0009-cccc-4ccc-8ccc-cccccccccccc", dedupe_key="p7:page:tie", created_at=iso(minutes=-2))]
    first = repo.inbox_list(USER, limit=2)
    assert [r["id"] for r in first] == [ids[0], ids[1]]
    second = repo.inbox_list(USER, limit=2, before=(first[-1]["created_at"], first[-1]["id"]))
    assert [r["id"] for r in second] == ["cccc0009-cccc-4ccc-8ccc-cccccccccccc", ids[2]]
    third = repo.inbox_list(USER, limit=5, before=(second[-1]["created_at"], second[-1]["id"]))
    assert [r["id"] for r in third] == [ids[3], ids[4]]
    assert repo.inbox_list(USER, limit=5, before=(third[-1]["created_at"], third[-1]["id"])) == []


def test_notification_states_return_only_this_persons_bells(db):
    db.tables["notifications"] = [bell_row(id=BELL, read_at=iso()), bell_row(id=BELL2, user_id=P7_OTHER)]
    states = repo.notification_states(USER, [BELL, BELL2, "not-a-uuid"])
    assert list(states) == [BELL] and states[BELL]["read_at"] == iso()


# ── Subscriptions and project mutes ──────────────────────────────────────────


def test_subscription_set_user_and_role_rows_and_the_urgent_rules(db):
    out = repo.subscription_set(USER, "todo.due", enabled=True, severity="high")
    assert out["ok"] and out["subscription"]["user_id"] == USER and out["subscription"]["source"] == "user"
    assert repo.subscription_set(USER, "todo.due", enabled=False, severity="low", source="feedback_auto")["ok"]
    assert len(repo.user_subscriptions(USER)) == 1 and repo.user_subscriptions(USER)[0]["source"] == "feedback_auto"
    assert repo.subscription_set(USER, "todo.due", enabled=True, severity="urgent")["code"] == "invalid_mode"
    assert repo.subscription_set(USER, "Bad Kind", enabled=True, severity="low")["code"] == "unknown_kind"
    assert repo.subscription_set(USER, "todo.due", enabled=True, severity="low", source="robot")["code"] == "bad_source"
    role = repo.role_subscription_set(None, "executive", "assistant.approval_expiring", enabled=True,
                                      severity="urgent", actor=USER)
    assert role["ok"] and role["subscription"]["role"] == "executive" and role["subscription"]["user_id"] is None
    assert role["subscription"]["source"] == "admin" and role["subscription"]["updated_by"] == USER
    assert repo.role_subscription_set(None, "executive", "bell.quote_received", enabled=True, severity="urgent",
                                      actor=USER)["code"] == "invalid_mode"
    assert repo.role_subscription_set(None, "estimator", "todo.due", enabled=True, severity="low",
                                      actor=USER)["code"] == "bad_role"
    assert repo.role_subscription_set(None, "executive", "todo.due", enabled=False, severity="normal",
                                      actor=USER)["ok"]
    assert [(s["role"], s["event_type"]) for s in repo.role_subscriptions(None)] == [
        ("executive", "assistant.approval_expiring"), ("executive", "todo.due")]
    assert [s["event_type"] for s in repo.role_subscriptions(None, role="executive", event_type="todo.due")] == \
        ["todo.due"]
    assert repo.user_subscriptions(P7_OTHER) == []


def test_role_defaults_accept_the_acting_person_for_request_handlers(db):
    """The admin routes pass the caller's id; the router sweep passes None.
    Either way the row is a role row and the id never reaches it."""
    out = repo.role_subscription_set(USER, "executive", "todo.due", enabled=True, severity="high", actor=USER)
    assert out["ok"] and out["subscription"]["user_id"] is None and out["subscription"]["role"] == "executive"
    assert [s["event_type"] for s in repo.role_subscriptions(USER, role="executive")] == ["todo.due"]
    assert [s["event_type"] for s in repo.role_subscriptions(None, role="executive")] == ["todo.due"]
    assert repo.role_subscription_reset(USER, "executive", "todo.due", actor=USER) is True
    assert repo.role_subscriptions(USER) == []


def test_subscription_reset_removes_one_row_and_reports_whether_it_did(db):
    db.tables["assistant_subscriptions"] = [
        subscription_row(),
        subscription_row(user_id=P7_OTHER),
        subscription_row(user_id=None, role="executive", source="admin"),
    ]
    assert repo.subscription_reset(USER, "todo.due") is True
    assert repo.subscription_reset(USER, "todo.due") is False
    assert len(rows(db, "assistant_subscriptions")) == 2
    assert repo.role_subscription_reset(None, "executive", "todo.due", actor=USER) is True
    assert [s["user_id"] for s in rows(db, "assistant_subscriptions")] == [P7_OTHER]


def test_the_fake_enforces_both_partial_unique_indexes_and_the_role_check(db):
    db._insert("assistant_subscriptions", {"user_id": USER, "event_type": "todo.due", "source": "user"})
    with pytest.raises(UniqueViolation, match="assistant_subscriptions_user_uq"):
        db._insert("assistant_subscriptions", {"user_id": USER, "event_type": "todo.due", "source": "user"})
    db._insert("assistant_subscriptions", {"role": "executive", "event_type": "todo.due"})
    with pytest.raises(UniqueViolation, match="assistant_subscriptions_role_uq"):
        db._insert("assistant_subscriptions", {"role": "executive", "event_type": "todo.due"})
    for bad in ("estimator", "pe", "estimating_engineer"):
        with pytest.raises(fk.CheckViolation):
            db._insert("assistant_subscriptions", {"role": bad, "event_type": "todo.due"})


def test_project_mutes_add_remove_and_ignore_duplicates(db):
    assert repo.project_mute_add(USER, PROJECT, now=at()) is True
    assert repo.project_mute_add(USER, PROJECT, now=at()) is False
    assert repo.project_mute_add(P7_OTHER, PROJECT, now=at()) is True
    assert repo.project_mute_remove(USER, PROJECT) is True
    assert repo.project_mute_remove(USER, PROJECT) is False
    assert [m["user_id"] for m in repo.project_mutes(P7_OTHER)] == [P7_OTHER]


def test_digests_recent_is_newest_first_and_per_person(db):
    db.tables["assistant_digests"] = [
        digest_row(), digest_row(id="d4d4d4d4-d4d4-4d4d-8d4d-d4d4d4d4d4d4", slot="pm", created_at=iso(hours=6)),
        digest_row(id="d5d5d5d5-d5d5-4d5d-8d5d-d5d5d5d5d5d5", user_id=P7_OTHER),
    ]
    got = repo.digests_recent(USER)
    assert [d["slot"] for d in got] == ["pm", "am"]


def test_digest_auto_downgrades_reads_the_window_since_the_previous_rung_digest(db):
    """S7-1 (9.1): the feedback_auto rows after the previous digest that rang
    its bell and up to this digest, this person only, oldest first; an
    unrung digest (no items) does not close the window."""
    prev = digest_row(id="d1d1d1d1-d1d1-4d1d-8d1d-d1d1d1d1d1d1", created_at=iso(hours=-24),
                      notification_id=BELL)
    unrung = digest_row(id="d2d2d2d2-d2d2-4d2d-8d2d-d2d2d2d2d2d2", slot="pm", created_at=iso(hours=-18))
    this = digest_row(id="d3d3d3d3-d3d3-4d3d-8d3d-d3d3d3d3d3d3", created_at=iso(), notification_id=BELL2)
    other = digest_row(id="d4d4d4d4-d4d4-4d4d-8d4d-d4d4d4d4d4d4", user_id=P7_OTHER, created_at=iso(hours=-2),
                       notification_id=BELL)
    db.tables["assistant_digests"] = [prev, unrung, this, other]
    db.tables["assistant_subscriptions"] = [
        subscription_row(event_type="todo.due", source="feedback_auto", created_at=iso(hours=-20)),
        subscription_row(event_type="bid.due_not_ready", source="feedback_auto", created_at=iso(hours=-21)),
        subscription_row(event_type="assistant.approval_expiring", source="feedback_auto",
                         created_at=iso(hours=-30)),                                  # before prev
        subscription_row(event_type="bell.quote_received", source="user", created_at=iso(hours=-3)),
        subscription_row(event_type="todo.due", user_id=P7_OTHER, source="feedback_auto", created_at=iso(hours=-3)),
        subscription_row(event_type="todo.due", user_id=None, role="executive", source="admin",
                         created_at=iso(hours=-3)),
    ]
    got = repo.digest_auto_downgrades(USER, repo.digest(USER, this["id"]))
    assert [r["event_type"] for r in got] == ["bid.due_not_ready", "todo.due"]
    # The first digest of all reaches back the retention window; the earlier digest's own window is disjoint.
    assert [r["event_type"] for r in repo.digest_auto_downgrades(USER, prev)] == ["assistant.approval_expiring"]
    later = digest_row(id="d5d5d5d5-d5d5-4d5d-8d5d-d5d5d5d5d5d5", created_at=iso(hours=6), notification_id=BELL)
    db.tables["assistant_digests"].append(later)
    assert repo.digest_auto_downgrades(USER, later) == []
    assert repo.digest_auto_downgrades(USER, {"created_at": None}) == []


def test_digest_auto_downgrades_does_not_repeat_a_purged_digests_lines(db):
    """With no earlier rung digest left (the purge removed it), the window
    opens at created_at minus the digest retention days, so a line the
    purged digest already announced is not announced again."""
    old = digest_row(id="d1d1d1d1-d1d1-4d1d-8d1d-d1d1d1d1d1d1", created_at=iso(days=-400), notification_id=BELL)
    this = digest_row(id="d3d3d3d3-d3d3-4d3d-8d3d-d3d3d3d3d3d3", created_at=iso(), notification_id=BELL2)
    db.tables["assistant_digests"] = [old, this]
    db.tables["assistant_subscriptions"] = [
        subscription_row(event_type="todo.due", source="feedback_auto", created_at=iso(days=-401)),  # old announced it
        subscription_row(event_type="bid.due_not_ready", source="feedback_auto", created_at=iso(days=-30)),
    ]
    # Before the purge the old digest bounds the window.
    assert [r["event_type"] for r in repo.digest_auto_downgrades(USER, this)] == ["bid.due_not_ready"]
    repo.proactive_purge(None, inbox_days=90, event_days=90, digest_days=365, send_days=90, now=at())
    assert [d["id"] for d in rows(db, "assistant_digests")] == [this["id"]]
    # After it, the same lines (two workers read the same bound).
    for _ in range(2):
        got = repo.digest_auto_downgrades(USER, this, retention_days=365)
        assert [r["event_type"] for r in got] == ["bid.due_not_ready"]
    # The default reads the setting (365); the shortest retention narrows the window.
    assert [r["event_type"] for r in repo.digest_auto_downgrades(USER, this)] == ["bid.due_not_ready"]
    assert repo.digest_auto_downgrades(USER, this, retention_days=7) == []


# ── Cursors and slots ────────────────────────────────────────────────────────


def test_cursor_advance_is_fenced_on_the_old_value(db):
    db.tables["assistant_router_cursors"] = [cursor_row(), cursor_row(name="audit_log")]
    old = repo.router_cursors(None)[0]
    assert old["name"] == "audit_log"  # ordered by name
    cur = next(c for c in repo.router_cursors(None) if c["name"] == "notifications")
    assert repo.advance_router_cursor(None, "notifications", expect_at=cur["cursor_at"], expect_id=None,
                                      new_at=at(minutes=5), new_id=ITEM, updated_by="wa", now=at(minutes=6)) is True
    moved = next(c for c in repo.router_cursors(None) if c["name"] == "notifications")
    assert (moved["cursor_at"], moved["cursor_id"], moved["updated_by"]) == (iso(minutes=5), ITEM, "wa")
    # The other worker, holding the old value, loses.
    assert repo.advance_router_cursor(None, "notifications", expect_at=cur["cursor_at"], expect_id=None,
                                      new_at=at(minutes=9), updated_by="wb") is False
    assert repo.advance_router_cursor(None, "notifications", expect_at=iso(minutes=5), expect_id=ITEM,
                                      new_at=at(minutes=9), updated_by="wb") is True
    with pytest.raises(ValueError):
        repo.advance_router_cursor(None, "quotes", expect_at=iso(), expect_id=None, new_at=iso(), updated_by="w")
    assert repo.reseed_router_cursors(None, to_at=at(hours=1), updated_by="boot", now=at(hours=1)) == 2
    assert {c["cursor_at"] for c in repo.router_cursors(None)} == {iso(hours=1)}
    assert repo.reseed_router_cursors(None, to_at=at(), updated_by="boot", names=["audit_log"]) == 1
    with pytest.raises(ValueError):
        repo.reseed_router_cursors(None, to_at=at(), updated_by="boot", names=["quotes"])


def test_a_slot_is_claimed_once_and_reclaimed_after_the_lease(db):
    day = date(2026, 10, 30)
    held = repo.claim_proactive_slot(None, "digest_am", day, "wa", at())
    assert held["claimed_by"] == "wa" and held["result"] == {"attempts": 1} and held["finished_at"] is None
    assert repo.claim_proactive_slot(None, "digest_am", day, "wb", at(minutes=5)) is None
    assert repo.claim_proactive_slot(None, "digest_pm", day, "wb", at(minutes=5))["claimed_by"] == "wb"
    # Past 15 minutes the dead holder's slot is reclaimed and result.attempts is bumped.
    assert repo.record_proactive_slot(None, "digest_am", day, worker="wa", result={"failures": 1, "error": "KeyError"})
    taken = repo.claim_proactive_slot(None, "digest_am", day, "wb", at(minutes=16))
    assert taken["claimed_by"] == "wb" and taken["claimed_at"] == iso(minutes=16)
    assert taken["result"] == {"attempts": 2, "failures": 1, "error": "KeyError"}
    assert repo.claim_proactive_slot(None, "digest_am", day, "wa", at(minutes=17)) is None
    with pytest.raises(ValueError):
        repo.claim_proactive_slot(None, "digest_noon", day, "wa", at())


def test_a_finished_slot_is_never_reclaimed_and_only_the_holder_finishes_it(db):
    day = date(2026, 10, 30)
    repo.claim_proactive_slot(None, "purge", day, "wa", at())
    assert repo.finish_proactive_slot(None, "purge", day, worker="wb", now=at(minutes=1)) is False
    assert repo.record_proactive_slot(None, "purge", day, worker="wb", result={"x": 1}) is False
    assert repo.finish_proactive_slot(None, "purge", day, worker="wa", result={"assistant_inbox": 3},
                                      now=at(minutes=2)) is True
    slot = repo.proactive_slot(None, "purge", day)
    assert slot["finished_at"] == iso(minutes=2) and slot["result"] == {"attempts": 1, "assistant_inbox": 3}
    assert repo.finish_proactive_slot(None, "purge", day, worker="wa") is False
    assert repo.record_proactive_slot(None, "purge", day, worker="wa", result={"x": 1}) is False
    assert repo.claim_proactive_slot(None, "purge", day, "wb", at(hours=5)) is None
    assert repo.proactive_slot(None, "purge", date(2026, 11, 1)) is None


def test_two_workers_race_for_a_slot_and_one_wins(db):
    with ThreadPoolExecutor(2) as pool:
        got = list(pool.map(lambda w: repo.claim_proactive_slot(None, "digest_am", date(2026, 10, 30), w, at()),
                            ["wa", "wb"]))
    assert sum(g is not None for g in got) == 1


@pytest.mark.parametrize("round_", range(25))
def test_concurrent_inserts_of_one_key_never_raise_and_one_row_lands(db, round_):
    """Real PostgREST ignore-duplicates returns nothing for the loser; the
    fake must too, whichever thread appends first."""
    def inbox(_):
        return repo.insert_inbox_row(USER, _router_row(), now=at())

    def event(_):
        return repo.insert_events(None, [{"event_type": "todo.due", "dedupe_key": "p7:todo.due:race:2026-10-30"}])

    with ThreadPoolExecutor(4) as pool:
        made = list(pool.map(inbox, range(4)))
        landed = list(pool.map(event, range(4)))
    assert sum(m is not None for m in made) == 1 and sum(len(e) for e in landed) == 1
    assert len(rows(db, "assistant_inbox")) == 1 and len(rows(db, "assistant_events")) == 1


@pytest.mark.parametrize("round_", range(25))
def test_the_slot_race_is_stable_under_repetition(db, round_):
    with ThreadPoolExecutor(3) as pool:
        got = list(pool.map(lambda w: repo.claim_proactive_slot(None, "digest_pm", date(2026, 10, 30), w, at()),
                            ["wa", "wb", "wc"]))
    assert sum(g is not None for g in got) == 1


# ── The send ledger ──────────────────────────────────────────────────────────


def test_send_claim_honours_not_before_and_backoff_and_claims_one_row_per_call(db):
    db.tables["assistant_external_sends"] = [
        send_row(),
        send_row(id="5e5e5e5e-5e5e-45e5-85e5-5e5e5e5e5e52", target_id=INBOX2, not_before=iso(hours=6)),
        send_row(id="5e5e5e5e-5e5e-45e5-85e5-5e5e5e5e5e53", target_id=ITEM, next_attempt_at=iso(minutes=30), attempts=1),
        send_row(id="5e5e5e5e-5e5e-45e5-85e5-5e5e5e5e5e54", target_id=BELL, status="sent", sent_at=iso()),
    ]
    first = repo.external_sends_claim(None, worker="wa", now=at(minutes=1))
    assert [s["id"] for s in first] == [SEND] and first[0]["status"] == "claimed" and first[0]["attempts"] == 1
    assert first[0]["claimed_by"] == "wa" and first[0]["claimed_at"] == iso(minutes=1)
    assert repo.external_sends_claim(None, worker="wb", now=at(minutes=1)) == []  # the rest are held, backing off or sent


def test_send_claim_skips_backoff_and_future_rows(db):
    db.tables["assistant_external_sends"] = [
        send_row(not_before=iso(hours=6)),
        send_row(id="5e5e5e5e-5e5e-45e5-85e5-5e5e5e5e5e53", target_id=ITEM, next_attempt_at=iso(minutes=30),
                 attempts=1),
    ]
    assert repo.external_sends_claim(None, worker="wa", now=at(minutes=1)) == []
    assert [s["target_id"] for s in repo.external_sends_claim(None, worker="wa", now=at(minutes=31))] == [ITEM]
    assert [s["id"] for s in repo.external_sends_claim(None, worker="wa", limit=5, now=at(hours=7))] == [SEND]
    db.tables["assistant_external_sends"][0].update(status="pending", claimed_by=None, claimed_at=None)
    db.locked_sends.add(SEND)
    assert repo.external_sends_claim(None, worker="wb", now=at(hours=8)) == []


def test_a_stale_claim_is_retired_as_uncertain_and_never_handed_out_again(db):
    db.tables["assistant_external_sends"] = [
        send_row(status="claimed", claimed_by="dead", claimed_at=iso(), attempts=1)]
    assert repo.external_sends_claim(None, worker="wb", now=at(minutes=5)) == []
    assert one(db, "assistant_external_sends", id=SEND)["status"] == "claimed"
    assert repo.external_sends_claim(None, worker="wb", lease_seconds=600, now=at(minutes=11)) == []
    row = one(db, "assistant_external_sends", id=SEND)
    assert (row["status"], row["error_kind"], row["claimed_by"]) == ("uncertain", "lease_expired", None)
    assert repo.external_sends_claim(None, worker="wb", now=at(days=1)) == []


def _claimed(db, **over):
    db.tables["assistant_external_sends"] = [send_row(**over)]
    return repo.external_sends_claim(None, worker="wa", now=at())[0]


def test_settle_is_fenced_on_the_claim(db):
    _claimed(db)
    assert repo.external_send_settle(None, SEND, worker="wb", outcome="sent", now=at()) is False
    assert repo.external_send_settle(None, SEND, worker="wa", outcome="sent", now=at(seconds=2)) is True
    row = one(db, "assistant_external_sends", id=SEND)
    assert (row["status"], row["sent_at"], row["error_kind"]) == ("sent", iso(seconds=2), None)
    assert repo.external_send_settle(None, SEND, worker="wa", outcome="sent") is False  # no longer claimed


def test_settle_email_records_the_email_log_id_and_teams_never_does(db):
    db.tables["assistant_external_sends"] = [
        send_row(target_kind="digest", target_id=DIGEST, channel="email")]
    repo.external_sends_claim(None, worker="wa", now=at())
    repo.external_send_settle(None, SEND, worker="wa", outcome="sent", email_log_id=ITEM, now=at())
    assert one(db, "assistant_external_sends", id=SEND)["email_log_id"] == ITEM
    _claimed(db)
    repo.external_send_settle(None, SEND, worker="wa", outcome="sent", email_log_id=ITEM, now=at())
    assert one(db, "assistant_external_sends", id=SEND)["email_log_id"] is None


def test_transient_failures_back_off_and_dead_letter_at_the_fifth_attempt(db):
    db.tables["assistant_external_sends"] = [send_row()]
    ladder = [60, 300, 1800, 7200]
    now = at()
    for attempt, secs in enumerate(ladder, start=1):
        claimed = repo.external_sends_claim(None, worker="wa", now=now)
        assert claimed and claimed[0]["attempts"] == attempt
        assert repo.external_send_settle(None, SEND, worker="wa", outcome="transient", retry_seconds=secs, now=now)
        row = one(db, "assistant_external_sends", id=SEND)
        assert (row["status"], row["error_kind"], row["claimed_by"]) == ("pending", "transient", None)
        assert row["next_attempt_at"] == (now + timedelta(seconds=secs)).isoformat()
        assert repo.external_sends_claim(None, worker="wa", now=now + timedelta(seconds=secs - 1)) == []
        now += timedelta(seconds=secs)
    assert repo.external_sends_claim(None, worker="wa", now=now)[0]["attempts"] == 5
    assert repo.external_send_settle(None, SEND, worker="wa", outcome="transient", retry_seconds=60, now=now)
    row = one(db, "assistant_external_sends", id=SEND)
    assert (row["status"], row["error_kind"], row["next_attempt_at"]) == ("failed", "transient", None)
    assert repo.external_sends_claim(None, worker="wa", now=now + timedelta(days=1)) == []


@pytest.mark.parametrize("outcome,kind,status,error", [
    ("permanent", None, "failed", "permanent"),
    ("uncertain", None, "uncertain", "uncertain"),
    ("skipped", "opted_out", "skipped", "opted_out"),
    ("skipped", "stale", "skipped", "stale"),
    ("skipped", "channel_off", "skipped", "channel_off"),
    ("skipped", "offboarded", "skipped", "offboarded"),
])
def test_terminal_outcomes_are_never_retried(db, outcome, kind, status, error):
    _claimed(db)
    assert repo.external_send_settle(None, SEND, worker="wa", outcome=outcome, error_kind=kind, now=at())
    row = one(db, "assistant_external_sends", id=SEND)
    assert (row["status"], row["error_kind"], row["next_attempt_at"]) == (status, error, None)
    assert repo.external_sends_claim(None, worker="wb", now=at(days=2)) == []


def test_deferred_moves_not_before_and_gives_the_attempt_back(db):
    _claimed(db)
    assert one(db, "assistant_external_sends", id=SEND)["attempts"] == 1
    assert repo.external_send_settle(None, SEND, worker="wa", outcome="deferred", retry_seconds=3600, now=at(hours=1))
    row = one(db, "assistant_external_sends", id=SEND)
    assert (row["status"], row["attempts"], row["not_before"], row["error_kind"], row["claimed_by"]) == \
        ("pending", 0, iso(hours=2), "quiet_hours", None)
    assert repo.external_sends_claim(None, worker="wa", now=at(hours=1, minutes=59)) == []
    assert repo.external_sends_claim(None, worker="wa", now=at(hours=2))[0]["attempts"] == 1


def test_settle_refuses_unknown_outcomes_and_error_kinds(db):
    _claimed(db)
    with pytest.raises(ValueError):
        repo.external_send_settle(None, SEND, worker="wa", outcome="done")
    with pytest.raises(ValueError):
        repo.external_send_settle(None, SEND, worker="wa", outcome="permanent", error_kind="boom")
    with pytest.raises(Exception, match="invalid_outcome"):
        repo.external_send_settle(None, SEND, worker="wa", outcome="skipped")           # a skip names its kind
    with pytest.raises(Exception, match="invalid_outcome"):
        repo.external_send_settle(None, SEND, worker="wa", outcome="skipped", error_kind="transient")
    assert one(db, "assistant_external_sends", id=SEND)["status"] == "claimed"


def test_two_workers_send_each_ledger_row_exactly_once(db):
    db.tables["assistant_external_sends"] = [
        send_row(id=f"5e5e5e5e-5e5e-45e5-85e5-5e5e5e5e5e{n:02d}", target_id=f"00000000-0000-4000-8000-0000000000{n:02d}",
                 not_before=iso(seconds=n)) for n in range(1, 9)]
    sent: list[str] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        while True:
            claimed = repo.external_sends_claim(None, worker=name, now=at(minutes=1))
            if not claimed:
                return
            with lock:
                sent.append(claimed[0]["id"])
            assert repo.external_send_settle(None, claimed[0]["id"], worker=name, outcome="sent", now=at(minutes=1))

    threads = [threading.Thread(target=worker, args=(w,)) for w in ("wa", "wb")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(sent) == len(set(sent)) == 8
    assert {r["status"] for r in rows(db, "assistant_external_sends")} == {"sent"}


def test_the_ledger_is_unique_per_target_and_channel_and_state_checked(db):
    first = repo.external_send_insert(USER, {"target_kind": "inbox", "target_id": INBOX, "channel": "teams"}, now=at())
    assert first["status"] == "pending" and first["not_before"] == iso() and first["attempts"] == 0
    assert repo.external_send_insert(USER, {"target_kind": "inbox", "target_id": INBOX, "channel": "teams"},
                                     now=at()) is None
    assert repo.external_send(USER, first["id"])["id"] == first["id"] and repo.external_send(P7_OTHER, first["id"]) is None
    assert repo.external_send_for_target(USER, "inbox", INBOX, "teams")["id"] == first["id"]
    skipped = repo.external_send_insert(USER, {"target_kind": "digest", "target_id": DIGEST, "channel": "email",
                                               "status": "skipped", "error_kind": "channel_off"}, now=at())
    assert skipped["status"] == "skipped" and skipped["error_kind"] == "channel_off"
    for bad in ({"target_kind": "inbox", "target_id": INBOX, "channel": "email"},
                {"target_kind": "digest", "target_id": INBOX, "channel": "teams"},
                {"target_kind": "inbox", "target_id": INBOX, "channel": "teams", "status": "sent"},
                {"target_kind": "inbox", "target_id": INBOX, "channel": "teams", "error_kind": "boom"}):
        with pytest.raises(ValueError):
            repo.external_send_insert(USER, bad, now=at())
    with pytest.raises(UniqueViolation, match="assistant_external_sends_target_uq"):
        db._insert("assistant_external_sends", {"user_id": USER, "target_kind": "inbox", "target_id": INBOX,
                                                "channel": "teams", "not_before": iso(), "created_at": iso()})
    with pytest.raises(fk.CheckViolation):
        db._insert("assistant_external_sends", {"user_id": USER, "target_kind": "inbox", "target_id": ITEM,
                                                "channel": "teams", "not_before": iso(), "created_at": iso(),
                                                "status": "sent"})


# ── Offboarding, purge, report ───────────────────────────────────────────────


def test_offboarding_dismisses_undelivered_rows_skips_pending_sends_and_deletes_overrides(db):
    db.tables["assistant_inbox"] = [
        inbox_row(),                                                               # undelivered: dismissed
        inbox_row(id=INBOX2, dedupe_key="p7:d", delivery="digest"),                 # undelivered: dismissed
        inbox_row(id="bc000001-bc00-4c00-8c00-bc0000000001", dedupe_key="p7:rung", delivered_at=iso()),  # history
        inbox_row(id="bc000002-bc00-4c00-8c00-bc0000000002", dedupe_key="p7:o", user_id=P7_OTHER),
    ]
    db.tables["assistant_external_sends"] = [
        send_row(), send_row(id="5e5e5e5e-5e5e-45e5-85e5-5e5e5e5e5e52", target_id=INBOX2, status="claimed",
                             claimed_by="w", claimed_at=iso(), attempts=1),
        send_row(id="5e5e5e5e-5e5e-45e5-85e5-5e5e5e5e5e53", target_id=ITEM, user_id=P7_OTHER)]
    db.tables["assistant_subscriptions"] = [
        subscription_row(), subscription_row(event_type="bid.due_not_ready"),
        subscription_row(user_id=None, role="executive", source="admin"), subscription_row(user_id=P7_OTHER)]
    db.tables["assistant_project_mutes"] = [{"user_id": USER, "project_id": PROJECT, "created_at": iso()},
                                            {"user_id": P7_OTHER, "project_id": PROJECT, "created_at": iso()}]
    assert repo.offboard_dismiss_undelivered(USER, now=at()) == 2
    assert {r["dedupe_key"]: r["dismissed_reason"] for r in rows(db, "assistant_inbox")} == {
        "p7:todo.due:t1:2026-10-30": "owner_offboarded", "p7:d": "owner_offboarded", "p7:rung": None, "p7:o": None}
    assert repo.offboard_skip_pending_sends(USER, now=at()) == 1
    assert {r["id"]: (r["status"], r["error_kind"]) for r in rows(db, "assistant_external_sends")} == {
        SEND: ("skipped", "offboarded"), "5e5e5e5e-5e5e-45e5-85e5-5e5e5e5e5e52": ("claimed", None),
        "5e5e5e5e-5e5e-45e5-85e5-5e5e5e5e5e53": ("pending", None)}
    assert repo.offboard_delete_subscriptions(USER) == 2
    assert [(s["role"], s["user_id"]) for s in rows(db, "assistant_subscriptions")] == [("executive", None), (None, P7_OTHER)]
    assert repo.offboard_delete_project_mutes(USER) == 1
    assert [m["user_id"] for m in rows(db, "assistant_project_mutes")] == [P7_OTHER]
    assert repo.offboard_skip_pending_sends(USER, now=at()) == 0 and repo.offboard_delete_subscriptions(USER) == 0


def test_purge_removes_old_rows_skips_legal_hold_and_never_touches_direct_rows_or_live_sends(db):
    old = iso(days=-400)
    db.tables["assistant_profiles"] = [{"user_id": P7_OTHER, "legal_hold": True}]
    db.tables["projects"] = [{"id": PROJECT, "abandoned_at": iso(days=-1), "failed_to_bid_at": None,
                              "current_stage": "bidding"}, {"id": fk.PROJECT2, "abandoned_at": None,
                                                           "failed_to_bid_at": None, "current_stage": "bidding"}]
    db.tables["assistant_events"] = [
        event_row(status="processed", processed_at=old, dedupe_key="p7:old"),
        event_row(id="e2e2e2e2-e2e2-4e2e-8e2e-e2e2e2e2e2e2", dedupe_key="p7:held", status="processed",
                  processed_at=old, subject_user_id=P7_OTHER),
        event_row(id="e3e3e3e3-e3e3-4e3e-8e3e-e3e3e3e3e3e3", dedupe_key="p7:new"),
    ]
    db.tables["assistant_inbox"] = [
        inbox_row(created_at=old, event_id=EVENT_ID),
        inbox_row(id=INBOX2, dedupe_key="p7:direct", delivery="direct", deliver_after=None, created_at=old),
        inbox_row(id="bd000001-bd00-4d00-8d00-bd0000000001", dedupe_key="p7:held", user_id=P7_OTHER, created_at=old,
                  event_id="e2e2e2e2-e2e2-4e2e-8e2e-e2e2e2e2e2e2"),
        inbox_row(id="bd000002-bd00-4d00-8d00-bd0000000002", dedupe_key="p7:recent"),
    ]
    db.tables["assistant_digests"] = [digest_row(created_at=old), digest_row(
        id="d6d6d6d6-d6d6-4d6d-8d6d-d6d6d6d6d6d6", user_id=P7_OTHER, created_at=old)]
    db.tables["assistant_proactive_slots"] = [slot_row(claimed_at=old), slot_row(slot_date="2026-10-30")]
    db.tables["assistant_external_sends"] = [
        send_row(status="sent", sent_at=old, created_at=old), send_row(
            id="5e5e5e5e-5e5e-45e5-85e5-5e5e5e5e5e52", target_id=INBOX2, created_at=old),            # pending: kept
        send_row(id="5e5e5e5e-5e5e-45e5-85e5-5e5e5e5e5e53", target_id=ITEM, user_id=P7_OTHER,
                 status="failed", created_at=old)]
    db.tables["assistant_router_cursors"] = [cursor_row(cursor_at=old)]
    db.tables["assistant_project_mutes"] = [
        {"user_id": USER, "project_id": PROJECT, "created_at": old},
        {"user_id": USER, "project_id": fk.PROJECT2, "created_at": old},
        {"user_id": P7_OTHER, "project_id": PROJECT, "created_at": old}]
    out = repo.proactive_purge(None, inbox_days=90, event_days=90, digest_days=365, send_days=90, now=at())
    assert out == {"limit": 200, "assistant_inbox": 1, "assistant_events": 1, "assistant_digests": 1,
                   "assistant_proactive_slots": 1, "assistant_external_sends": 1, "assistant_project_mutes": 1}
    assert {r["dedupe_key"] for r in rows(db, "assistant_inbox")} == {"p7:direct", "p7:held", "p7:recent"}
    assert {r["dedupe_key"] for r in rows(db, "assistant_events")} == {"p7:held", "p7:new"}
    assert [r["user_id"] for r in rows(db, "assistant_digests")] == [P7_OTHER]
    assert {r["status"] for r in rows(db, "assistant_external_sends")} == {"pending", "failed"}
    assert len(rows(db, "assistant_router_cursors")) == 1
    assert {(m["user_id"], m["project_id"]) for m in rows(db, "assistant_project_mutes")} == {
        (USER, fk.PROJECT2), (P7_OTHER, PROJECT)}


def test_purge_is_bounded_and_refuses_bad_arguments(db):
    old = iso(days=-400)
    db.tables["assistant_inbox"] = [inbox_row(id=f"be00000{n}-be00-4e00-8e00-be0000000000", dedupe_key=f"p7:{n}",
                                              created_at=old) for n in range(5)]
    out = repo.proactive_purge(None, inbox_days=90, event_days=90, digest_days=365, send_days=90, limit=2, now=at())
    assert out["assistant_inbox"] == 2 and len(rows(db, "assistant_inbox")) == 3
    with pytest.raises(Exception, match="bad arguments"):
        repo.proactive_purge(None, inbox_days=0, event_days=90, digest_days=365, send_days=90, now=at())


def _report_rows(db, kind="todo.due", roles=("executive", "executive", "estimating_admin"), **over):
    ids = [f"0000000{n}-0000-4000-8000-00000000000{n}" for n in range(1, len(roles) + 1)]
    db.tables["profiles"] = [{"id": u, "role": role} for u, role in zip(ids, roles, strict=True)]
    db.tables["assistant_inbox"] = [
        inbox_row(id=f"bf00000{n}-bf00-4f00-8f00-bf0000000000", dedupe_key=f"p7:r{n}", user_id=u, event_type=kind,
                  delivered_at=iso(), feedback="useful" if n == 0 else None, **over) for n, u in enumerate(ids)]
    return ids


def test_the_report_returns_counts_and_hides_cells_under_the_floor(db):
    _report_rows(db)
    out = repo.inbox_feedback_report(None, at(days=-30), now=at())
    assert out["floor"] == 3 and out["since"] == iso(days=-30)
    kind = out["kinds"][0]
    assert (kind["event_type"], kind["suppressed"], kind["users"], kind["delivered"], kind["useful"]) == \
        ("todo.due", False, 3, 3, 1)
    # Two executives and one admin: both role cells are under the floor, so each is listed as suppressed.
    assert kind["roles_suppressed"] is False
    assert kind["roles"] == [{"role": "estimating_admin", "suppressed": True, "users": None},
                             {"role": "executive", "suppressed": True, "users": None}]
    # Three executives and one admin: the one hidden person would be isolated by subtraction, so the
    # whole breakdown is withheld.
    _report_rows(db, roles=("executive", "executive", "executive", "estimating_admin"))
    kind = repo.inbox_feedback_report(None, at(days=-30), now=at())["kinds"][0]
    assert kind["users"] == 4 and kind["roles_suppressed"] is True and kind["roles"] == []
    # Three and three: both cells visible.
    _report_rows(db, roles=("executive",) * 3 + ("estimating_admin",) * 3)
    kind = repo.inbox_feedback_report(None, at(days=-30), now=at())["kinds"][0]
    assert kind["roles_suppressed"] is False and [r["suppressed"] for r in kind["roles"]] == [False, False]
    assert kind["roles"][0]["users"] == 3
    assert "user_id" not in str(out).lower()
    assert set(out["health"]) == {"events_pending", "events_failed_7d", "events_lease_exhausted_7d",
                                  "slot_failures_7d", "workers", "cursors", "sends_7d"}


def test_a_kind_with_fewer_than_three_people_is_suppressed_and_direct_rows_are_excluded(db):
    _report_rows(db, roles=("executive", "estimating_admin"))
    out = repo.inbox_feedback_report(None, at(days=-30), floor=1, now=at())  # a floor below 3 is raised to 3
    assert out["floor"] == 3 and out["kinds"] == [{"event_type": "todo.due", "suppressed": True, "users": None}]
    db.tables["assistant_inbox"] += [inbox_row(id="bf000099-bf00-4f00-8f00-bf0000000099", dedupe_key="calendar:x",
                                               delivery="direct", event_type="assistant.calendar", deliver_after=None)]
    assert [k["event_type"] for k in repo.inbox_feedback_report(None, at(days=-30), now=at())["kinds"]] == ["todo.due"]


def test_the_report_health_block_counts_events_slots_and_sends(db):
    db.tables["assistant_events"] = [
        event_row(), event_row(id="e4e4e4e4-e4e4-4e4e-8e4e-e4e4e4e4e4e4", dedupe_key="p7:f", status="failed",
                               processed_at=iso(days=-1), last_error="lease_exhausted")]
    db.tables["assistant_proactive_slots"] = [slot_row(result={"failures": 2})]
    db.tables["assistant_router_cursors"] = [cursor_row()]
    db.tables["assistant_external_sends"] = [send_row(), send_row(
        id="5e5e5e5e-5e5e-45e5-85e5-5e5e5e5e5e52", target_id=INBOX2, status="failed", error_kind="permanent")]
    health = repo.inbox_feedback_report(None, at(days=-30), now=at())["health"]
    assert (health["events_pending"], health["events_failed_7d"], health["events_lease_exhausted_7d"],
            health["slot_failures_7d"]) == (1, 1, 1, 2)
    assert health["workers"] == [{"worker": "worker-a", "last_claim_at": iso()}]
    assert health["cursors"][0]["name"] == "notifications"
    assert health["sends_7d"] == [
        {"channel": "teams", "status": "failed", "error_kind": "permanent", "count": 1},
        {"channel": "teams", "status": "pending", "error_kind": None, "count": 1}]


# ── The fake delivery helpers ────────────────────────────────────────────────


def test_the_fake_helpers_record_calls_and_raise_scripted_errors(monkeypatch):
    fakes = fk.install_delivery_fakes(monkeypatch)
    from app.services import email_branding, graph_email, teams_alerts

    fakes.graph.fail_next(None, fk.graph_error("http_503"), ("after", fk.graph_error("supabase_connect")))
    assert graph_email.send_mail(to=["a@g3.test"], subject="s", body_html="<p>x</p>")["status"] == "sent"
    with pytest.raises(Exception, match="503"):
        graph_email.send_mail(to=["a@g3.test"], subject="s2", body_html="<p>y</p>")
    with pytest.raises(Exception, match="email_log"):
        graph_email.send_mail(to=["a@g3.test"], subject="s3", body_html="<p>z</p>")
    assert [c["delivered"] for c in fakes.graph.calls] == [True, False, True]
    assert len(fakes.graph.sent) == 2 and graph_email._active_test_session() is None
    fakes.graph.test_session = {"id": "s"}
    assert graph_email._active_test_session() == {"id": "s"}

    card = teams_alerts.build_card(heading="h", message="m", project_label="26.10.7301", cta_label="Open",
                                   cta_url="https://x/assistant")
    fakes.teams.fail_next(None, fk.teams_error("chat_not_open"))
    teams_alerts._deliver(USER, "a@g3.test", card, "summary", {})
    with pytest.raises(teams_alerts.TeamsAlertError, match="has not opened its chat yet"):
        teams_alerts._deliver(USER, "a@g3.test", card, "summary", {})
    assert len(fakes.teams.calls) == 2 and fakes.teams.cards == [card]
    fakes.teams.states[USER] = {"enabled": False}
    assert teams_alerts.get_state(USER) == {"enabled": False} and teams_alerts.status(USER)["enabled"] is False
    fakes.teams.configured = False
    assert teams_alerts.is_configured() is False

    html = email_branding.render_notification_email(recipient_name=None, heading="Your assistant digest",
                                                    message="Items", cta_label="Open", cta_url="https://x", linkify=False)
    assert "Hi there," in html and fakes.branding.renders[0]["linkify"] is False
    assert email_branding.logo_bytes() == b"LOGO"


def test_the_scripted_graph_errors_carry_the_host_the_classifier_reads():
    import httpx

    assert set(fk.GRAPH_ERROR_CLASSES) == {
        "connect_graph", "connect_login", "http_429", "http_503", "token", "redirect_refused", "supabase_connect",
        "http_500", "http_502", "http_504", "read_timeout", "write_error", "http_400", "http_403", "http_404"}
    hosts = {kind: (fk.graph_error(kind).request.url.host if isinstance(fk.graph_error(kind), httpx.HTTPError) else None)
             for kind in fk.GRAPH_ERROR_CLASSES}
    assert hosts["connect_graph"] == fk.GRAPH_HOST and hosts["connect_login"] == fk.LOGIN_HOST
    assert hosts["supabase_connect"] == fk.SUPABASE_HOST and hosts["http_500"] == fk.GRAPH_HOST
    assert fk.graph_error("http_429").response.status_code == 429
    assert isinstance(fk.graph_error("token"), RuntimeError) and isinstance(fk.graph_error("redirect_refused"), RuntimeError)
    assert fk.GRAPH_ERROR_CLASSES["supabase_connect"] == "uncertain" and fk.GRAPH_ERROR_CLASSES["http_403"] == "permanent"


def test_the_scripted_teams_texts_are_the_helpers_own_wording():
    source = (Path(repo.__file__).resolve().parents[1] / "services/teams_alerts.py").read_text(encoding="utf-8")
    fragments = {
        "resolve_429": "Microsoft Graph refused the call", "resolve_503": "(HTTP {code}",
        "graph_unreachable": ": could not reach Microsoft Graph (", "chat_not_open": "has not opened its chat yet",
        "bot_signin": "The bot could not sign in", "teams_connect": "Could not reach Teams (",
        "teams_429": "Teams returned HTTP ", "no_account": "No Microsoft 365 account found for",
        "bot_401": "Teams rejected the bot's sign-in", "refused_new_chat": "Teams refused the new BDR Alerts chat",
        "refused_chat": "Teams refused the BDR Alerts chat",
        "install_403": "BDR Alerts is not installed for you yet and Teams will not let BDR",
    }
    for kind, fragment in fragments.items():
        assert fragment in source, kind
        assert fragment.split(" {")[0].rstrip("(") in fk.TEAMS_ERROR_TEXTS[kind], kind
    assert set(fk.TEAMS_ERROR_TEXTS) == set(fk.TEAMS_ERROR_CLASSES)
    assert str(fk.teams_error("no_account")) == fk.TEAMS_ERROR_TEXTS["no_account"]


# ── The fake itself ──────────────────────────────────────────────────────────


def test_the_fake_enforces_the_inbox_and_event_checks(db):
    with pytest.raises(fk.CheckViolation):  # delivery_chk: immediate needs deliver_after
        db._insert("assistant_inbox", {"user_id": USER, "dedupe_key": "p7:x", "title": "t", "delivery": "immediate"})
    with pytest.raises(fk.CheckViolation):  # a source bell belongs to record rows only
        db._insert("assistant_inbox", {"user_id": USER, "dedupe_key": "p7:y", "title": "t", "delivery": "digest",
                                       "deliver_after": iso(), "source_notification_id": BELL})
    with pytest.raises(fk.CheckViolation):  # dismissed pair
        db._insert("assistant_inbox", {"user_id": USER, "dedupe_key": "p7:z", "title": "t", "dismissed_at": iso()})
    row = db._insert("assistant_inbox", {"user_id": USER, "dedupe_key": "calendar:abc", "title": "t"})
    assert row["event_type"] == "assistant.calendar" and row["delivery"] == "direct"  # the classify trigger
    assert db._insert("assistant_inbox", {"user_id": USER, "dedupe_key": "mail:abc", "title": "t"})["event_type"] \
        == "assistant.mailbox_access"
    assert db._insert("assistant_inbox", {"user_id": USER, "dedupe_key": "p7:k", "title": "t", "delivery": "record"}
                      )["event_type"] is None
    with pytest.raises(UniqueViolation, match="assistant_inbox_dedupe_uq"):
        db._insert("assistant_inbox", {"user_id": USER, "dedupe_key": "calendar:abc", "title": "t"})
    db._insert("assistant_events", {"event_type": "todo.due", "dedupe_key": "p7:e1"})
    with pytest.raises(UniqueViolation, match="assistant_events_dedupe_key_key"):
        db._insert("assistant_events", {"event_type": "todo.due", "dedupe_key": "p7:e1"})
    with pytest.raises(fk.CheckViolation):
        db._insert("assistant_events", {"event_type": "Bad Type", "dedupe_key": "p7:e2"})
    with pytest.raises(fk.CheckViolation):  # done_chk: a finished event carries processed_at
        db._insert("assistant_events", {"event_type": "todo.due", "dedupe_key": "p7:e3", "status": "processed"})


def test_a_failing_statement_inside_an_rpc_leaves_nothing_behind(db):
    db.tables["assistant_inbox"] = [inbox_row()]
    db.tables["assistant_external_sends"] = []
    original = db._p7_insert_send

    def refuse(row):
        raise fk.CheckViolation("boom")

    db._p7_insert_send = refuse
    with pytest.raises(fk.CheckViolation):
        repo.inbox_deliver(USER, INBOX, bell_message="m", teams=True, now=at())
    db._p7_insert_send = original
    assert rows(db, "notifications") == [] and one(db, "assistant_inbox", id=INBOX)["delivered_at"] is None
    assert repo.inbox_deliver(USER, INBOX, bell_message="m", teams=True, now=at())["delivered"] is True


def test_rpc_errors_can_be_scripted_for_the_new_rpcs(db):
    db.rpc_errors["assistant_inbox_deliver"] = RuntimeError("db down")
    with pytest.raises(RuntimeError):
        repo.inbox_deliver(USER, INBOX, bell_message="m", now=at())
    assert repo.inbox_deliver(USER, INBOX, bell_message="m", now=at()) == {"delivered": False}

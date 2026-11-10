"""Independent-session race probe (browser v2, plan 18.3) for a disposable LOCAL database.

Requires psycopg 3 and migrations through 0192 (the personal browser v2 RPCs:
assistant_browser_admission_create, assistant_browser_task_enqueue_v2,
assistant_browser_claim_user). Cases: company budget, one lease for one queued
job, two leases on one worker (the worker slot limit holds), the per-owner
lease limit across two workers, the per-owner queued limit and the owner's
daily budget. Set PHASE6_PROBE_DATABASE_URL to
a PostgreSQL URI on 127.0.0.1/::1, with database name phase6_probe_<suffix>.
The database must have no users, browser jobs, spend holds or model usage.
Run: python scripts/check_assistant_browser_concurrency.py

This COMMITS synthetic fixtures and temporary enabled settings. Discard the
entire disposable database afterward, including on failure. Never point this
at a forwarded hosted database. No application, worker or maintenance process
may use this database. No real secret, model call or external request is used.
The URI is never printed. Each race proves a contender actually waits on the
holder's PostgreSQL lock before releasing the holder transaction.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import os
import re
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4


def validate_local_uri(uri: str) -> str:
    """Reject libpq host overrides, services, DNS names and non-probe databases."""
    parsed = urlsplit(uri)
    if (parsed.scheme not in {"postgres", "postgresql"}
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or not re.fullmatch(r"/phase6_probe_[a-zA-Z0-9_]+", parsed.path)
            or parsed.query or parsed.fragment):
        raise ValueError("Use a disposable loopback phase6_probe_* database URI without options.")
    return uri


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def race(connect, first, second, *, timeout: float = 8.0) -> tuple:
    """Hold the first RPC's locks until an independent second RPC blocks on it."""
    release = threading.Event()
    held = threading.Event()
    started = threading.Event()
    pids = {}

    def holder():
        with connect() as conn:
            pids["holder"] = conn.execute("select pg_backend_pid()").fetchone()[0]
            result = first(conn)
            held.set()
            require(release.wait(timeout + 4), "Holder release timed out.")
            return result  # context manager commits only after release

    def contender():
        with connect() as conn:
            pids["contender"] = conn.execute("select pg_backend_pid()").fetchone()[0]
            started.set()
            return second(conn)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(holder)
        b = None
        try:
            require(held.wait(timeout), "First RPC failed or did not acquire its locks.")
            b = pool.submit(contender)
            require(started.wait(timeout), "Second session failed to start.")
            require(pids["holder"] != pids["contender"], "Sessions share a backend.")
            with connect() as observer:
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    blockers = observer.execute(
                        "select pg_blocking_pids(%s)", (pids["contender"],)
                    ).fetchone()[0]
                    if pids["holder"] in blockers:
                        break
                    require(not b.done(), "Contender returned without observed lock contention.")
                    time.sleep(0.025)
                else:
                    raise RuntimeError("Independent lock contention was not observed.")
        finally:
            release.set()
        return a.result(timeout=timeout), b.result(timeout=timeout)


@dataclass
class Owner:
    user: str
    conversation: str


SITE = "zz_probe_public"


def owner(conn) -> Owner:
    """An eligible it_admin owner with a chat and a standing grant on the public probe site."""
    ids = Owner(*(str(uuid4()) for _ in range(2)))
    email = f"browser-v2-concurrency-{ids.user}@example.invalid"
    conn.execute("insert into auth.users(id,email) values(%s,%s)", (ids.user, email))
    conn.execute("""insert into public.profiles(id,full_name,email,role,is_active)
        values(%s,'Concurrency fixture',%s,'it_admin',true)
        on conflict(id) do update set role='it_admin',is_active=true""", (ids.user, email))
    conn.execute("insert into public.assistant_delegations(user_id,created_session_aal) "
                 "values(%s,'aal2')", (ids.user,))
    conn.execute("insert into public.assistant_conversations(id,user_id,mode) "
                 "values(%s,%s,'normal')", (ids.conversation, ids.user))
    grant = conn.execute("select public.assistant_browser_site_grant(%s,%s,'domain',"
                         "'example.org',null,'chat_card','typed')", (ids.user, SITE)).fetchone()[0]
    require(grant.get("ok") is True, "Site grant fixture failed.")
    return ids


def admission(conn, who: Owner):
    """A ready admission (public site, granted, no credential) and the enqueue call for it."""
    record = {"conversation_id": who.conversation, "goal": "Read concurrency fixture",
              "start_site_key": SITE, "start_url": "https://example.org/a",
              "start_provenance": "typed", "fresh_user_message": True}
    made = conn.execute("select public.assistant_browser_admission_create(%s,%s::jsonb)",
                        (who.user, json.dumps(record))).fetchone()[0]
    require(made.get("ok") is True and made["admission"]["status"] == "ready",
            "Admission fixture is not ready.")
    admission_id = made["admission"]["id"]

    def enqueue(session):
        return session.execute("select public.assistant_browser_task_enqueue_v2("
                               "%s,%s,'{}'::jsonb,100,100)",
                               (who.user, admission_id)).fetchone()[0]
    return enqueue


def worker(conn, slots: int = 2):
    worker_id, boot = str(uuid4()), str(uuid4())
    conn.execute("""insert into public.browser_agent_workers
        (id,name,status,principal_kind,boot_id,allowed_types,capabilities,max_slots)
        values(%s,%s,'active','user',%s,array['user_browser'],
        '{"types":{"user_browser":"1.1.0"}}',%s)""",
                 (worker_id, "browser-v2-concurrency-" + worker_id, boot, slots))

    def claim(session, want: int = 1):
        rows = session.execute("select id,lease_owner,lease_epoch from "
                               "public.assistant_browser_claim_user(%s,%s,%s::jsonb,90)",
                               (worker_id, boot, json.dumps({"user_browser": want}))).fetchall()
        return rows
    claim.worker_id = worker_id
    return claim


def leases(conn, worker_id: str | None = None) -> int:
    if worker_id is None:
        return conn.execute("select count(*) from public.browser_agent_jobs "
                            "where lease_owner is not null").fetchone()[0]
    return conn.execute("select count(*) from public.browser_agent_jobs where lease_owner=%s",
                        (worker_id,)).fetchone()[0]


def probe(connect) -> None:
    with connect() as conn:
        for table in ("auth.users", "public.browser_agent_jobs",
                      "public.assistant_spend_reservations", "public.llm_call_log"):
            require(conn.execute(f"select count(*) from {table}").fetchone()[0] == 0,
                    "Probe requires an empty disposable database with migrations applied.")
        require(conn.execute("select public.assistant_browser_runtime_ready()").fetchone()[0],
                "Browser runtime schema is unavailable.")
        # Reservation step is 1 USD, so a 1.5 USD company cap admits exactly one task.
        conn.execute("update public.assistant_settings set enabled=true,global_daily_cap_usd=1.5 "
                     "where id=1")
        conn.execute("update public.browser_agent_settings set enabled=true,"
                     "user_per_job_cost_cap_usd=5,user_task_reserve_step_usd=1,"
                     "daily_cost_cap_usd=100,user_per_owner_concurrency=2,"
                     "user_per_owner_queued=5 where id=1")
        conn.execute("update public.browser_agent_types set enabled=true,provider='openai',"
                     "model='fixture',price_in_per_mtok=1,price_out_per_mtok=1,"
                     "max_concurrency=8,model_probe_ok_at=now() where key='user_browser'")
        conn.execute("insert into public.assistant_browser_sites(site_key,label,kind,"
                     "registrable_domains,login_kind) values(%s,'Probe public','curated',"
                     "array['example.org'],'none')", (SITE,))
        owners = [owner(conn) for _ in range(6)]
        first, second = (admission(conn, who) for who in owners[:2])
        claims = [worker(conn), worker(conn)]

    a, b = race(connect, first, second)
    require(a.get("created") is True and b.get("code") == "global_cap",
            "Concurrent admissions exceeded the company budget or failed unexpectedly.")
    with connect() as conn:
        held = conn.execute("select sum(amount_usd),count(*) from "
                            "public.assistant_spend_reservations where status='active'").fetchone()
        require(held == (1, 1), "Quota race left excess reservations.")
    print("PASS: independent concurrent admissions preserve the company budget.", flush=True)

    a, b = race(connect, *claims)
    require(len(a) + len(b) == 1, "Concurrent workers claimed the same job.")
    with connect() as conn:
        require(leases(conn) == 1, "Claim race left multiple leases.")
        conn.execute("update public.assistant_settings set global_daily_cap_usd=100 where id=1")
        pair = worker(conn, 2)
        d_first, e_first = admission(conn, owners[2]), admission(conn, owners[3])
        d_second = admission(conn, owners[2])
        for enqueue in (d_first, e_first):
            require(enqueue(conn).get("created") is True, "Setup enqueue failed.")
    print("PASS: independent workers produce exactly one lease for one queued job.", flush=True)

    # Two leases on one worker: two sessions of the same worker with one free slot each
    # take the two queued jobs once, and the worker's slot limit then refuses a third.
    a, b = race(connect, pair, pair)
    ids = [row[0] for row in a + b]
    require(len(ids) == 2 and len(set(ids)) == 2, "Two sessions of one worker did not split the jobs.")
    with connect() as conn:
        require(leases(conn, pair.worker_id) == 2, "Worker did not hold exactly two leases.")
        require(d_second(conn).get("created") is True, "Third job enqueue failed.")
        require(pair(conn, 3) == [], "Worker claimed past its slot limit.")
        require(leases(conn, pair.worker_id) == 2, "Slot limit race left a third lease.")
    print("PASS: two leases on one worker, and the slot limit holds.", flush=True)

    # Per-owner lease limit: one owner with two queued jobs, two workers, limit one lease.
    with connect() as conn:
        conn.execute("update public.browser_agent_settings set user_per_owner_concurrency=1 "
                     "where id=1")
        f_first, f_second = admission(conn, owners[4]), admission(conn, owners[4])
        for enqueue in (f_first, f_second):
            require(enqueue(conn).get("created") is True, "Setup enqueue failed.")
        owner_claims = [worker(conn), worker(conn)]
    a, b = race(connect, *owner_claims)
    require(len(a) + len(b) == 1, "Concurrent workers exceeded the per-owner lease limit.")
    with connect() as conn:
        held = conn.execute("select count(*) from public.browser_agent_jobs where "
                            "principal_user_id=%s and lease_owner is not null",
                            (owners[4].user,)).fetchone()[0]
        require(held == 1, "Per-owner lease race left more than one lease.")
        require(owner_claims[0](conn) == [] and owner_claims[1](conn) == [],
                "A later claim passed the per-owner lease limit.")
        conn.execute("update public.browser_agent_settings set user_per_owner_concurrency=2 "
                     "where id=1")
    print("PASS: independent claims preserve the per-owner lease limit.", flush=True)

    # Per-owner queued limit: one owner, limit one active job, two simultaneous approvals.
    with connect() as conn:
        conn.execute("update public.browser_agent_settings set user_per_owner_queued=1 "
                     "where id=1")
        same_owner = [admission(conn, owners[5]), admission(conn, owners[5])]
    a, b = race(connect, *same_owner)
    require(a.get("created") is True and b.get("code") == "concurrency_cap",
            "Concurrent approvals exceeded the owner's queued-job limit.")
    with connect() as conn:
        require(conn.execute("select count(*) from public.browser_agent_jobs "
                             "where principal_user_id=%s", (owners[5].user,)).fetchone()[0] == 1,
                "Owner queue race left duplicate jobs.")
    print("PASS: independent approvals preserve the owner's queued-job limit.", flush=True)

    with connect() as conn:
        conn.execute("insert into public.assistant_profiles(user_id,daily_cost_cap_usd) "
                     "values(%s,3) on conflict(user_id) do update set daily_cost_cap_usd=3",
                     (owners[1].user,))

    def reserve(session):
        return session.execute("select public.assistant_spend_reserve("
                               "%s,'browser_task',2,28800,3,100,null,null,null)",
                               (owners[1].user,)).fetchone()[0]

    a, b = race(connect, reserve, reserve)
    require(a.get("ok") is True and b.get("reason") == "user_cap",
            "Concurrent holds exceeded the owner's daily budget.")
    with connect() as conn:
        held = conn.execute("select sum(amount_usd),count(*) from "
                            "public.assistant_spend_reservations where status='active' "
                            "and user_id=%s", (owners[1].user,)).fetchone()
        require(held == (2, 1), "Owner budget race left excess reservations.")
    print("PASS: independent reservations preserve the owner's daily budget.", flush=True)


def main() -> None:
    uri = validate_local_uri(os.environ.get("PHASE6_PROBE_DATABASE_URL", ""))
    try:
        import psycopg
    except ImportError:
        raise SystemExit("This optional local probe requires psycopg 3.") from None

    def connect():
        return psycopg.connect(uri, connect_timeout=3, application_name="browser-v2-concurrency",
                               options="-c statement_timeout=15000 -c lock_timeout=12000")
    try:
        probe(connect)
    finally:
        print("Discard the disposable phase6_probe_* database, including on failure.", flush=True)


if __name__ == "__main__":
    main()

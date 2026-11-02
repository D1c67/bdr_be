"""Independent-session phase 6 race probe for a disposable LOCAL database.

Requires psycopg 3 and migrations through 0188. Set PHASE6_PROBE_DATABASE_URL to
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
    credential: str
    conversation: str


def owner(conn) -> Owner:
    ids = Owner(*(str(uuid4()) for _ in range(3)))
    email = f"phase6-concurrency-{ids.user}@example.invalid"
    conn.execute("insert into auth.users(id,email) values(%s,%s)", (ids.user, email))
    conn.execute("""insert into public.profiles(id,full_name,email,role,is_active)
        values(%s,'Concurrency fixture',%s,'it_admin',true)
        on conflict(id) do update set role='it_admin',is_active=true""", (ids.user, email))
    conn.execute("insert into public.assistant_delegations(user_id,created_session_aal) "
                 "values(%s,'aal2')", (ids.user,))
    conn.execute("insert into public.assistant_conversations(id,user_id,mode) "
                 "values(%s,%s,'normal')", (ids.conversation, ids.user))
    credential = {"id": ids.credential, "label": "Fixture", "username": email,
                  "origins": ["https://app.procore.com", "https://login.procore.com", "https://us02.procore.com"],
                  "wrapped_dek": "fake", "ciphertext": "fake", "nonce": "fake",
                  "key_id": "fixture", "fingerprint": "fixture"}
    result = conn.execute("select public.assistant_browser_credential_put(%s,'procore',0,%s::jsonb)",
                          (ids.user, json.dumps(credential))).fetchone()[0]
    require(result.get("ok") is True, "Credential fixture failed.")
    return ids


def admission(conn, who: Owner):
    approval, run = str(uuid4()), str(uuid4())
    payload = {"service": "procore", "url": "https://app.procore.com/123/project/rfi",
               "objective": "Read concurrency fixture", "credential_id": who.credential,
               "credential_version": 1, "client_request_id": str(uuid4()), "max_cost_usd": "2"}
    payload["authority"] = {"role": "it_admin", "model": "fixture",
                            "conversation_id": who.conversation,
                            **{k: payload[k] for k in ("credential_id", "credential_version",
                                                      "url", "objective", "max_cost_usd")}}
    wrapper = {"request": {"method": "POST", "path": "/assistant/browser-tasks", "json": payload}}
    conn.execute("""insert into public.assistant_approvals(id,user_id,user_label,
        conversation_id,action_kind,payload,payload_sha256,status,decided_by,decided_at,expires_at)
        values(%s,%s,'Fixture',%s,'user_browser_task',%s::jsonb,%s,'approved',%s,now(),
        now()+interval '1 hour')""",
                 (approval, who.user, who.conversation, json.dumps(wrapper), "a" * 64, who.user))

    def enqueue(session):
        return session.execute("select public.assistant_browser_task_enqueue("
                               "%s,%s,%s,%s,%s::jsonb,100,100)",
                               (who.user, approval, run, "a" * 64,
                                json.dumps(payload))).fetchone()[0]
    return enqueue


def worker(conn):
    worker_id, boot = str(uuid4()), str(uuid4())
    conn.execute("""insert into public.browser_agent_workers
        (id,name,status,principal_kind,boot_id,allowed_types,capabilities)
        values(%s,%s,'active','user',%s,array['user_browser_read'],
        '{"types":{"user_browser_read":"1.0.0"}}')""",
                 (worker_id, "phase6-concurrency-" + worker_id, boot))

    def claim(session):
        return session.execute("select id,lease_owner,lease_epoch from "
                               "public.claim_browser_agent_jobs(%s,%s,"
                               "'{\"user_browser_read\":1}',90)", (worker_id, boot)).fetchall()
    return claim


def probe(connect) -> None:
    with connect() as conn:
        for table in ("auth.users", "public.browser_agent_jobs",
                      "public.assistant_spend_reservations", "public.llm_call_log"):
            require(conn.execute(f"select count(*) from {table}").fetchone()[0] == 0,
                    "Probe requires an empty disposable database with migrations applied.")
        require(conn.execute("select public.assistant_browser_runtime_ready()").fetchone()[0],
                "Phase 6 runtime schema is unavailable.")
        conn.execute("update public.assistant_settings set enabled=true,global_daily_cap_usd=3 "
                     "where id=1")
        conn.execute("update public.browser_agent_settings set enabled=true,"
                     "per_job_cost_cap_usd=2,daily_cost_cap_usd=100 where id=1")
        conn.execute("update public.browser_agent_types set enabled=true,provider='openai',"
                     "model='fixture',price_in_per_mtok=1,price_out_per_mtok=1,"
                     "max_concurrency=8,model_probe_ok_at=now() where key='user_browser_read'")
        owners = [owner(conn) for _ in range(3)]
        first, second = (admission(conn, who) for who in owners[:2])
        claims = [worker(conn), worker(conn)]

    a, b = race(connect, first, second)
    require(a.get("created") is True and b.get("code") == "global_cap",
            "Concurrent admissions exceeded the company budget or failed unexpectedly.")
    with connect() as conn:
        held = conn.execute("select sum(amount_usd),count(*) from "
                            "public.assistant_spend_reservations where status='active'").fetchone()
        require(held == (2, 1), "Quota race left excess reservations.")
    print("PASS: independent concurrent admissions preserve the company budget.", flush=True)

    a, b = race(connect, *claims)
    require(len(a) == 1 and b == [], "Concurrent workers claimed the same job.")
    with connect() as conn:
        require(conn.execute("select count(*) from public.browser_agent_jobs "
                             "where lease_owner is not null").fetchone()[0] == 1,
                "Claim race left multiple leases.")
        conn.execute("update public.assistant_settings set global_daily_cap_usd=100 where id=1")
        same_owner = [admission(conn, owners[2]), admission(conn, owners[2])]
    print("PASS: independent workers produce exactly one lease for one queued job.", flush=True)

    a, b = race(connect, *same_owner)
    require(a.get("created") is True and b.get("code") == "concurrency_cap",
            "Concurrent approvals exceeded one active job per owner.")
    with connect() as conn:
        require(conn.execute("select count(*) from public.browser_agent_jobs "
                             "where principal_user_id=%s", (owners[2].user,)).fetchone()[0] == 1,
                "Owner cap race left duplicate jobs.")
    print("PASS: independent approvals preserve the owner's active-job cap.", flush=True)

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
        return psycopg.connect(uri, connect_timeout=3, application_name="phase6-concurrency",
                               options="-c statement_timeout=15000 -c lock_timeout=12000")
    try:
        probe(connect)
    finally:
        print("Discard the disposable phase6_probe_* database, including on failure.", flush=True)


if __name__ == "__main__":
    main()

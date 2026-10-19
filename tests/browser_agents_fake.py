"""In-memory Supabase stand-in for the browser agent tests (no network).

Covers the builder surface core/agent_auth.py, routers/agent_worker.py and
services/browser_agents/{queue,artifacts}.py use (select / insert / update /
upsert, eq / neq / in_ / is_ / not_.is_ / lt / lte / gt / gte / or_, order,
limit), the 0157 unique indexes, a storage bucket, and Python twins of the
0157 RPCs the worker API calls. The twins follow the SQL in
supabase/migrations/0157_browser_agents.sql line for line (same checks, same
order, same ErrorCode messages), so router tests exercise the real contract.
The SQL itself is pinned by tests/test_migration_0157.py and the dev smoke.
"""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from postgrest.exceptions import APIError

UTC = timezone.utc


def ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


def _cmp(value: Any) -> Any:
    if isinstance(value, str) and len(value) >= 10 and value[4:5] == "-":
        return ts(value) or value
    return value


def _raise(code: str) -> None:
    raise APIError({"message": code, "code": "P0001", "details": None, "hint": None})


def _eq(have: Any, want: Any) -> bool:
    if have == want:
        return True
    if isinstance(want, bool) or isinstance(have, bool):
        return str(have).lower() == str(want).lower()
    if isinstance(have, (int, float)) and not isinstance(have, bool):
        try:
            return float(have) == float(want)
        except (TypeError, ValueError):
            return False
    a, b = _cmp(have), _cmp(want)
    if isinstance(a, datetime) and isinstance(b, datetime):
        return a == b
    return str(have) == str(want) if have is not None and want is not None else False


def _term(row: dict, term: str) -> bool:
    col, op, val = term.split(".", 2)
    have = row.get(col)
    if op == "is":
        return have is None if val == "null" else str(have).lower() == val
    if have is None:
        return False
    if op == "eq":
        return _eq(have, val)
    if op in ("lt", "lte", "gt", "gte"):
        a, b = _cmp(have), _cmp(val)
        return {"lt": a < b, "lte": a <= b, "gt": a > b, "gte": a >= b}[op]
    raise AssertionError(f"unsupported or_ op {op}")


class _Q:
    def __init__(self, db: "FakeDB", table: str):
        self.db, self.table = db, table
        self.op: str | None = None
        self.payload: Any = None
        self.filters: list = []
        self.orders: list = []
        self.lim: int | None = None
        self.on_conflict: str | None = None

    # operations
    def select(self, *a, **k):
        self.op = "select"
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def upsert(self, payload, on_conflict=None, **k):
        self.op, self.payload, self.on_conflict = "upsert", payload, on_conflict
        return self

    def delete(self):
        self.op = "delete"
        return self

    # filters
    def eq(self, c, v):
        self.filters.append(lambda r: _eq(r.get(c), v))
        return self

    def neq(self, c, v):
        self.filters.append(lambda r: not _eq(r.get(c), v))
        return self

    def in_(self, c, vals):
        vals = [str(v) for v in vals]
        self.filters.append(lambda r: r.get(c) is not None and str(r.get(c)) in vals)
        return self

    def is_(self, c, v):
        self.filters.append(lambda r: r.get(c) is None)
        return self

    def lt(self, c, v):
        self.filters.append(lambda r: r.get(c) is not None and _cmp(r.get(c)) < _cmp(v))
        return self

    def lte(self, c, v):
        self.filters.append(lambda r: r.get(c) is not None and _cmp(r.get(c)) <= _cmp(v))
        return self

    def gt(self, c, v):
        self.filters.append(lambda r: r.get(c) is not None and _cmp(r.get(c)) > _cmp(v))
        return self

    def gte(self, c, v):
        self.filters.append(lambda r: r.get(c) is not None and _cmp(r.get(c)) >= _cmp(v))
        return self

    def like(self, c, pattern):
        import re as _re
        rx = _re.compile("^" + "".join(".*" if ch in "%*" else _re.escape(ch) for ch in pattern) + "$", _re.DOTALL)
        self.filters.append(lambda r: r.get(c) is not None and bool(rx.match(str(r.get(c)))))
        return self

    def ilike(self, c, pattern):
        import re as _re
        rx = _re.compile("^" + "".join(".*" if ch in "%*" else _re.escape(ch) for ch in pattern) + "$",
                         _re.DOTALL | _re.IGNORECASE)
        self.filters.append(lambda r: r.get(c) is not None and bool(rx.match(str(r.get(c)))))
        return self

    def or_(self, expr):
        terms = [t for t in expr.split(",") if t]
        self.filters.append(lambda r: any(_term(r, t) for t in terms))
        return self

    @property
    def not_(self):
        q = self

        class _Not:
            def is_(self, c, v):
                q.filters.append(lambda r: r.get(c) is not None)
                return q

        return _Not()

    def order(self, c, desc=False, **k):
        self.orders.append((c, desc))
        return self

    def limit(self, n, *a, **k):
        self.lim = n
        return self

    def _match(self, r):
        return all(f(r) for f in self.filters)

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self.op == "select":
            hits = [copy.deepcopy(r) for r in rows if self._match(r)]
            for c, desc in reversed(self.orders):
                hits.sort(key=lambda r: (r.get(c) is None, _cmp(r.get(c)) if r.get(c) is not None else 0),
                          reverse=desc)
            if self.lim is not None:
                hits = hits[: self.lim]
            return SimpleNamespace(data=hits)
        if self.op == "insert":
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            out = []
            for p in items:
                row = self.db.defaults(self.table, copy.deepcopy(p))
                self.db.check_unique(self.table, row)
                rows.append(row)
                out.append(copy.deepcopy(row))
            return SimpleNamespace(data=out)
        if self.op == "upsert":
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            keys = [k.strip() for k in (self.on_conflict or "id").split(",")]
            out = []
            for p in items:
                hit = next((r for r in rows if all(_eq(r.get(k), p.get(k)) for k in keys)), None)
                if hit is not None:
                    hit.update(copy.deepcopy(p))
                    out.append(copy.deepcopy(hit))
                else:
                    row = self.db.defaults(self.table, copy.deepcopy(p))
                    self.db.check_unique(self.table, row)
                    rows.append(row)
                    out.append(copy.deepcopy(row))
            return SimpleNamespace(data=out)
        if self.op == "update":
            out = []
            for r in rows:
                if self._match(r):
                    merged = {**r, **copy.deepcopy(self.payload)}
                    self.db.check_unique(self.table, merged, ignore=r)
                    self.db.guard_update(self.table, r, merged)
                    r.update(copy.deepcopy(self.payload))
                    self.db.after_update(self.table, r, set(self.payload))
                    out.append(copy.deepcopy(r))
            return SimpleNamespace(data=out)
        if self.op == "delete":
            if self.table == "browser_agent_steps":
                _raise("browser_agent_steps is append-only (DELETE refused)")
            removed = [r for r in rows if self._match(r)]
            self.db.tables[self.table] = [r for r in rows if not self._match(r)]
            return SimpleNamespace(data=removed)
        raise AssertionError(self.op)


# (table, index, predicate, key columns): the 0157 unique indexes the code relies on.
_UNIQUE = [
    ("browser_agent_jobs", "browser_agent_jobs_dedupe_uidx", lambda r: True, ("agent_type", "dedupe_key")),
    ("browser_agent_artifacts", "browser_agent_artifacts_job_sha_uidx", lambda r: r.get("job_id") is not None,
     ("job_id", "kind", "sha256")),
    ("browser_agent_artifacts", "browser_agent_artifacts_storage_path_key", lambda r: True, ("storage_path",)),
    ("browser_agent_steps", "browser_agent_steps_idem_uidx", lambda r: r.get("idem_key") is not None,
     ("job_id", "idem_key")),
    ("browser_agent_steps", "browser_agent_steps_seq_uidx", lambda r: True, ("job_id", "seq")),
    ("browser_agent_workers", "browser_agent_workers_name_key", lambda r: True, ("name",)),
    ("browser_agent_workers", "browser_agent_workers_token_hash_key", lambda r: r.get("token_hash") is not None,
     ("token_hash",)),
    ("browser_agent_enrollments", "browser_agent_enrollments_code_hash_key", lambda r: True, ("code_hash",)),
    ("browser_agent_enroll_attempts", "browser_agent_enroll_attempts_pkey", lambda r: True, ("ip", "window_start")),
    ("pm_rfi_sync_runs", "pm_rfi_sync_runs_agent_uidx", lambda r: r.get("agent_job_id") is not None,
     ("agent_job_id", "apply_attempt")),
    ("rfp_harvest_sessions", "rfp_harvest_sessions_pkey", lambda r: True, ("provider",)),
    ("browser_agent_help_requests", "browser_agent_help_open_uidx",
     lambda r: r.get("status") in ("open", "answered"), ("scope_key",)),
    ("browser_agent_help_waiters", "browser_agent_help_waiters_pkey", lambda r: True, ("help_id", "job_id")),
    ("browser_agent_help_reads", "browser_agent_help_reads_pkey", lambda r: True, ("job_id", "message_id")),
    ("browser_agent_help_messages", "browser_agent_help_messages_client_uniq",
     lambda r: r.get("client_msg_id") is not None, ("help_id", "client_msg_id")),
    ("browser_agent_help_messages", "browser_agent_help_messages_secret_id_key",
     lambda r: r.get("secret_id") is not None, ("secret_id",)),
    ("browser_agent_rfi_contributions", "browser_agent_rfi_contrib_uidx", lambda r: True,
     ("project_id", "platform", "external_rfi_id", "kind", "_contrib_key")),
]

_JOB_DEFAULTS = {
    "status": "queued", "outcome": None, "priority": 100, "platform": None, "project_id": None,
    "source_email_id": None, "config_snapshot": None, "lease_owner": None, "lease_boot_id": None,
    "lease_epoch": 0, "lease_expires_at": None, "attempts": 0, "interrupted_count": 0,
    "next_attempt_at": None, "resume_reason": None, "last_resume_reason": None, "checkpoint": {},
    "checkpoint_seq": 0, "step_count": 0, "turn_count": 0, "tokens_in": 0, "tokens_out": 0,
    "cost_usd": 0, "active_seconds": 0, "helper_retry_used": False, "apply_attempt": 0,
    "sync_run_id": None, "waiting_help_id": None, "hold_until": None, "result": None,
    "error_kind": None, "error": None, "control_mode": "agent", "cancel_requested_at": None,
    "cancel_requested_by": None, "created_by": None, "started_at": None, "finished_at": None,
}
# bigint identity primary keys (the rest are uuids).
_IDENTITY_TABLES = ("browser_agent_help_messages", "browser_agent_settings_changes")
_WORKER_DEFAULTS = {
    "status": "pending", "token_hash": None, "token_prefix": None, "draining": False, "max_slots": 3,
    "allowed_types": [], "allowed_ips": [], "boot_id": None, "boot_at": None, "hello_count_hour": 0,
    "hello_window_at": None, "version": None, "bad_versions": [], "hostname": None, "os_info": None,
    "chromium_version": None, "capabilities": None, "control_capabilities": {}, "last_seen_at": None,
    "last_ip": None, "enroll_ip": None,
}
_TYPE_DEFAULTS = {
    "enabled": False, "identity": None, "provider": None, "model": None, "price_in_per_mtok": None,
    "price_out_per_mtok": None, "model_probe_ok_at": None, "max_concurrency": 2, "max_attempts": 3,
    "max_interruptions": 20, "max_steps": 40, "max_total_tokens": 400000, "max_active_seconds": 1200,
    "host_narrowing": None, "priority_default": 100,
}
# Columns whose change bumps config_version (the 0157 triggers).
_TYPE_VERSION_COLS = {"enabled", "identity", "provider", "model", "max_concurrency", "max_steps",
                      "host_narrowing", "max_active_seconds"}
_WORKER_VERSION_COLS = {"status", "draining", "max_slots", "allowed_types", "allowed_ips"}


class _Bucket:
    def __init__(self, store: dict, name: str):
        self.store, self.name = store, name

    def upload(self, path, data, options=None):
        self.store[(self.name, path)] = bytes(data)
        return {"Key": path}

    def create_signed_url(self, path, ttl, options=None):
        return {"signedURL": f"https://storage.test/{self.name}/{path}?token=signed&ttl={ttl}"}

    def download(self, path):
        return self.store[(self.name, path)]


class _Storage:
    def __init__(self, store: dict):
        self.store = store

    def from_(self, name):
        return _Bucket(self.store, name)


class _Rpc:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return SimpleNamespace(data=self.fn())


class FakeDB:
    def __init__(self, now: datetime | None = None):
        self.now = now or datetime(2026, 10, 19, 17, 0, tzinfo=UTC)
        self.tables: dict[str, list[dict]] = {
            "browser_agent_settings": [{
                "id": 1, "enabled": True, "config_version": 1, "daily_cost_cap_usd": 25,
                "per_job_cost_cap_usd": 2, "help_hold_seconds": 900, "help_reminder_minutes": 120,
                "help_claim_lapse_minutes": 30, "help_expire_hours": 48, "worker_offline_alert_minutes": 60,
                "worker_offline_email_hours": 4, "job_ttl_hours": 168, "desired_worker_version": None,
            }],
            "browser_agent_runtime_state": [{
                "id": 1, "cost_window_started_at": None, "half_cap_notified_at": None,
                "cap_hit_notified_at": None, "offline_notified_at": None, "offline_emailed_at": None,
                "provider_alerts": {},
            }],
            "browser_agent_types": [
                {**_TYPE_DEFAULTS, "key": "rfi_from_email", "label": "RFI from email", "identity": "procore",
                 "prompt_version": "rfi_from_email.v1"},
                {**_TYPE_DEFAULTS, "key": "canary", "label": "Worker self-test", "prompt_version": "canary.v1",
                 "enabled": True, "provider": "openai", "model": "gpt-test", "price_in_per_mtok": 1,
                 "price_out_per_mtok": 2},
            ],
        }
        self.storage = _Storage({})
        self.rpc_calls: list[tuple[str, dict]] = []

    # ── plumbing ──
    def table(self, name):
        return _Q(self, name)

    def rpc(self, name, params):
        self.rpc_calls.append((name, copy.deepcopy(params)))
        fn = getattr(self, "rpc_" + name)
        return _Rpc(lambda: fn(**params))

    def stamp(self) -> str:
        return iso(self.now)

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def rows(self, table, **eq) -> list[dict]:
        return [r for r in self.tables.get(table, []) if all(_eq(r.get(k), v) for k, v in eq.items())]

    def one(self, table, **eq) -> dict:
        hits = self.rows(table, **eq)
        assert len(hits) == 1, (table, eq, hits)
        return hits[0]

    def defaults(self, table: str, row: dict) -> dict:
        if table == "browser_agent_jobs":
            row = {**_JOB_DEFAULTS, **row}
        elif table == "browser_agent_workers":
            row = {**_WORKER_DEFAULTS, **row}
        elif table == "browser_agent_types":
            row = {**_TYPE_DEFAULTS, **row}
        elif table == "browser_agent_login_attempts":
            row = {"refused_reason": None, "secret_version": None, "outcome": None, "step": None,
                   "counted_as_failure": False, "reported_at": None, **row}
        elif table == "browser_agent_rfi_contributions":
            row = {"applied_at": None, "applied_rfi_id": None, "source_message_ids": [], "note_text": None,
                   "artifact_id": None, **row}
            row["_contrib_key"] = str(row.get("artifact_id") or row.get("note_text"))
        elif table == "browser_agent_help_requests":
            row = {"status": "open", "claimed_by": None, "claimed_at": None, "last_helper_activity_at": None,
                   "resolved_by": None, "resolved_at": None, "resolution": None, "notified_at": None,
                   "reminder_count": 0, "reminder_sent_at": None, "expiry_emailed_at": None, "project_id": None,
                   "agent_message": None, "screenshot_artifact_id": None, "agent_type": None, **row}
        elif table == "browser_agent_help_messages":
            row = {"author_id": None, "author_label": None, "job_id": None, "client_msg_id": None,
                   "action": "message", "body": None, "is_secret": False, "secret_id": None,
                   "secret_ciphertext": None, "secret_nonce": None, "secret_hmac": None,
                   "secret_consumed_at": None, "secret_consumed_by_job": None, "secret_purged_at": None,
                   "artifact_ids": [], **row}
            if row["is_secret"] and (row["body"] is not None or row["secret_id"] is None):
                _raise("browser_agent_help_messages_secret_shape")
            if not row["is_secret"] and row["secret_ciphertext"] is not None:
                _raise("browser_agent_help_messages_secret_shape")
        elif table == "browser_agent_artifacts":
            row = {"job_id": None, "help_id": None, "attachable": False, "stripped_at": None, "filename": None,
                   "sniffed_mime": None, "uploaded_by_worker": None, "uploaded_by_user": None, **row}
        elif table == "browser_agent_steps":
            row = {"actor_user_id": None, "actor_label": None, "redacted_at": None, "tool_name": None, "args": None,
                   "result": None, "turn_no": None, "ok": None, "duration_ms": None, "url_before": None,
                   "url_after": None, "screenshot_artifact_id": None, "worker_id": None, "idem_key": None, **row}
        if table not in ("browser_agent_enroll_attempts", "browser_agent_help_waiters", "browser_agent_help_reads"):
            row.setdefault("id", str(uuid.uuid4()) if table not in _IDENTITY_TABLES
                           else len(self.tables.get(table, [])) + 1)
        row.setdefault("created_at", self.stamp())
        if table == "browser_agent_jobs":
            row.setdefault("updated_at", self.stamp())
        if table == "browser_agent_steps":
            mine = [s["seq"] for s in self.tables.get(table, []) if s["job_id"] == row["job_id"]]
            row["seq"] = (max(mine) if mine else 0) + 1
            row.setdefault("late", False)
            row.setdefault("at", self.stamp())
        return row

    def check_unique(self, table, row, ignore=None):
        for t, name, pred, cols in _UNIQUE:
            if t != table or not pred(row):
                continue
            key = tuple(str(row.get(c)) for c in cols)
            for other in self.tables.get(table, []):
                if other is ignore or (ignore is None and other is row):
                    continue
                if pred(other) and tuple(str(other.get(c)) for c in cols) == key:
                    raise APIError({"message": f'duplicate key value violates unique constraint "{name}"',
                                    "code": "23505", "details": None, "hint": None})

    def guard_update(self, table, old, new):
        if table == "browser_agent_steps":
            keep = {"args", "result", "redacted_at", "redacted_by_label"}
            if {k: v for k, v in old.items() if k not in keep} != {k: v for k, v in new.items() if k not in keep}:
                _raise("browser_agent_steps is append-only")
        if table == "browser_agent_settings_changes":
            _raise("browser_agent_settings_changes is append-only (UPDATE refused)")
        if table == "browser_agent_help_messages":
            keep = {"secret_consumed_at", "secret_consumed_by_job", "secret_purged_at", "secret_ciphertext",
                    "secret_nonce", "secret_hmac"}
            if {k: v for k, v in old.items() if k not in keep} != {k: v for k, v in new.items() if k not in keep}:
                _raise("browser_agent_help_messages: only secret bookkeeping may change")
            for col in ("secret_ciphertext", "secret_nonce", "secret_hmac"):
                if new.get(col) is not None and new.get(col) != old.get(col):
                    _raise("browser_agent_help_messages: secret material may only be nulled")
            if old.get("secret_consumed_at") is not None and new.get("secret_consumed_at") != old.get("secret_consumed_at"):
                _raise("browser_agent_help_messages: a secret is consumed once")

    def after_update(self, table, row, cols):
        if table == "browser_agent_jobs":
            row["updated_at"] = self.stamp()
        if (table == "browser_agent_types" and cols & _TYPE_VERSION_COLS) or (
            table == "browser_agent_workers" and cols & _WORKER_VERSION_COLS
        ):
            self.tables["browser_agent_settings"][0]["config_version"] += 1

    # ── 0157 RPC twins ──
    def _job(self, job_id) -> dict | None:
        hits = self.rows("browser_agent_jobs", id=str(job_id))
        return hits[0] if hits else None

    def _worker(self, worker_id) -> dict | None:
        hits = self.rows("browser_agent_workers", id=str(worker_id))
        return hits[0] if hits else None

    def _type(self, key) -> dict:
        return self.one("browser_agent_types", key=key)

    def _step(self, job_id, epoch, worker, kind, actor, summary, result=None, **extra):
        self.tables.setdefault("browser_agent_steps", []).append(self.defaults("browser_agent_steps", {
            "job_id": str(job_id), "epoch": int(epoch), "worker_id": worker, "idem_key": None,
            "kind": kind, "actor": actor, "summary": summary[:500], "result": result, **extra,
        }))

    def rpc_browser_agent_log_step(self, p_job, p_epoch, p_worker, p_kind, p_actor, p_summary, p_result=None):
        self._step(p_job, p_epoch, p_worker, p_kind, p_actor, p_summary, p_result)

    def rpc_browser_agent_fence(self, p_job, p_worker, p_boot, p_epoch, p_statuses):
        w = self._worker(p_worker)
        if w is None:
            _raise("unauthorized")
        if w["status"] == "revoked":
            _raise("revoked")
        if w["status"] != "active" or str(w.get("boot_id")) != str(p_boot):
            _raise("stale_boot")
        j = self._job(p_job)
        if (j is None or str(j.get("lease_owner")) != str(p_worker) or int(j["lease_epoch"]) != int(p_epoch)
                or str(j.get("lease_boot_id")) != str(p_boot)):
            _raise("lease_lost")
        if j["status"] not in p_statuses:
            _raise("job_not_running")
        return copy.deepcopy(j)

    def rpc_claim_browser_agent_jobs(self, p_worker, p_boot, p_capacity, p_lease_seconds=90):
        w = self._worker(p_worker)
        if w is None:
            _raise("unauthorized")
        if w["status"] == "revoked":
            _raise("revoked")
        if w["status"] != "active" or str(w.get("boot_id")) != str(p_boot):
            _raise("stale_boot")
        if w["draining"]:
            return []
        settings = self.tables["browser_agent_settings"][0]
        if not settings["enabled"]:
            return []
        out = []
        for key, want in (p_capacity or {}).items():
            want = min(max(int(want), 0), 8)
            if want <= 0 or key not in (w.get("allowed_types") or []):
                continue
            if not w.get("capabilities") or key not in (w["capabilities"].get("types") or {}):
                continue
            t = next((t for t in self.tables["browser_agent_types"] if t["key"] == key and t["enabled"]), None)
            if t is None:
                continue
            busy = len([j for j in self.tables.get("browser_agent_jobs", [])
                        if j["agent_type"] == key and j.get("lease_owner") is not None])
            take = min(want, int(t["max_concurrency"]) - busy)
            if take <= 0:
                continue
            pick = [
                j for j in self.tables.get("browser_agent_jobs", [])
                if j["agent_type"] == key and j["status"] == "queued" and not j.get("cancel_requested_at")
                and (ts(j.get("next_attempt_at")) or self.now) <= self.now and ts(j["expires_at"]) > self.now
            ]
            pick.sort(key=lambda j: (j["priority"], ts(j["created_at"])))
            snapshot = {"type": {k: v for k, v in t.items() if k not in ("updated_by", "updated_at")},
                        "per_job_cost_cap_usd": settings["per_job_cost_cap_usd"],
                        "daily_cost_cap_usd": settings["daily_cost_cap_usd"],
                        "config_version": settings["config_version"], "claimed_at": self.stamp()}
            for j in pick[:take]:
                j.update({
                    "status": "running", "lease_owner": str(p_worker), "lease_boot_id": str(p_boot),
                    "lease_epoch": int(j["lease_epoch"]) + 1,
                    "lease_expires_at": iso(self.now + timedelta(seconds=p_lease_seconds)),
                    "attempts": int(j["attempts"]) + (1 if (j.get("resume_reason") or "retry") == "retry" else 0),
                    "last_resume_reason": j.get("resume_reason"), "resume_reason": None,
                    "next_attempt_at": None, "hold_until": None, "config_snapshot": snapshot,
                    "started_at": j.get("started_at") or self.stamp(),
                })
                for turn in self.rows("browser_agent_turns", job_id=j["id"], status="thinking"):
                    turn["status"] = "abandoned"
                self._step(j["id"], j["lease_epoch"], str(p_worker), "claim", "bdr",
                           f"Claimed (attempt {j['attempts']}, epoch {j['lease_epoch']})",
                           {"resume_reason": j["last_resume_reason"], "boot_id": str(p_boot)})
                out.append(copy.deepcopy(j))
        return out

    def rpc_browser_agent_hello(self, p_worker, p_boot, p_info, p_ip=None):
        w = self._worker(p_worker)
        if w is None:
            _raise("unauthorized")
        if w["status"] == "revoked":
            _raise("revoked")
        window = ts(w.get("hello_window_at"))
        if window is None or window < self.now - timedelta(hours=1):
            w["hello_window_at"], w["hello_count_hour"] = self.stamp(), 1
        else:
            w["hello_count_hour"] += 1
        count = w["hello_count_hour"]
        w.update({"boot_id": str(p_boot), "boot_at": self.stamp(),
                  "version": p_info.get("version") or w.get("version"),
                  "chromium_version": p_info.get("chromium_version") or w.get("chromium_version"),
                  "bad_versions": p_info.get("bad_versions", w.get("bad_versions")),
                  "capabilities": p_info.get("capabilities") or w.get("capabilities"),
                  "last_seen_at": self.stamp(), "last_ip": p_ip or w.get("last_ip")})
        if count > 6 and w["status"] == "active" and not w["draining"]:
            w["draining"] = True
            self.tables["browser_agent_settings"][0]["config_version"] += 1
        requeued = failed = parked = 0
        for j in self.tables.get("browser_agent_jobs", []):
            if str(j.get("lease_owner")) != str(p_worker) or str(j.get("lease_boot_id")) == str(p_boot):
                continue
            if j["status"] not in ("running", "waiting_help"):
                continue
            if j["status"] == "waiting_help":
                j.update(lease_owner=None, lease_boot_id=None, lease_expires_at=None, hold_until=None)
                self._step(j["id"], j["lease_epoch"], str(p_worker), "job.parked", "bdr", "Parked")
                parked += 1
                continue
            cap = int(self._type(j["agent_type"])["max_interruptions"])
            if j["interrupted_count"] + 1 > cap:
                j.update(status="failed", error_kind="interrupted_repeatedly", error="Interrupted too many times",
                         interrupted_count=j["interrupted_count"] + 1, lease_epoch=j["lease_epoch"] + 1,
                         lease_owner=None, lease_boot_id=None, lease_expires_at=None, finished_at=self.stamp())
                failed += 1
            else:
                j.update(status="queued", resume_reason="interrupted", interrupted_count=j["interrupted_count"] + 1,
                         lease_epoch=j["lease_epoch"] + 1, lease_owner=None, lease_boot_id=None,
                         lease_expires_at=None)
                self._step(j["id"], j["lease_epoch"] - 1, str(p_worker), "job.interrupted", "bdr", "Requeued")
                requeued += 1
        return {"status": "pending" if w["status"] == "pending" else ("suspended" if count > 6 else "active"),
                "hello_count_hour": count, "requeued": requeued, "failed": failed, "parked": parked,
                "config_version": self.tables["browser_agent_settings"][0]["config_version"],
                "server_time": self.stamp()}

    def rpc_browser_agent_heartbeat(self, p_worker, p_boot, p_jobs, p_ip=None, p_lease_seconds=90):
        w = self._worker(p_worker)
        if w is None:
            _raise("unauthorized")
        if w["status"] == "revoked":
            _raise("revoked")
        if w["status"] != "active" or str(w.get("boot_id")) != str(p_boot):
            _raise("stale_boot")
        w["last_seen_at"] = self.stamp()
        w["last_ip"] = p_ip or w.get("last_ip")
        renewed, lost, cancel, listed, renewed_ids = [], [], [], [], []
        for item in p_jobs or []:
            listed.append(str(item["job_id"]))
            j = self._job(item["job_id"])
            if (j and str(j.get("lease_owner")) == str(p_worker) and int(j["lease_epoch"]) == int(item["epoch"])
                    and str(j.get("lease_boot_id")) == str(p_boot) and j["status"] in ("running", "waiting_help")):
                j["lease_expires_at"] = iso(self.now + timedelta(seconds=p_lease_seconds))
                if j["status"] == "running":
                    j["active_seconds"] += min(max(int(item.get("active_seconds_delta") or 0), 0), 30)
                renewed.append({"job_id": j["id"], "lease_expires_at": j["lease_expires_at"]})
                renewed_ids.append(str(j["id"]))
                if j.get("cancel_requested_at"):
                    cancel.append(j["id"])
            else:
                lost.append(str(item["job_id"]))
        orphans = [{"job_id": j["id"], "epoch": j["lease_epoch"]} for j in self.tables.get("browser_agent_jobs", [])
                   if str(j.get("lease_owner")) == str(p_worker) and str(j.get("lease_boot_id")) == str(p_boot)
                   and j["status"] in ("running", "waiting_help") and str(j["id"]) not in listed]
        help_updates = []
        for wt in self.tables.get("browser_agent_help_waiters", []):
            if str(wt["job_id"]) in renewed_ids:
                h = self.one("browser_agent_help_requests", id=wt["help_id"])
                ids = [m["id"] for m in self.rows("browser_agent_help_messages", help_id=h["id"])]
                help_updates.append({"job_id": wt["job_id"], "help_id": h["id"], "status": h["status"],
                                     "latest_message_id": max(ids) if ids else None})
        return {"config_version": self.tables["browser_agent_settings"][0]["config_version"],
                "renewed": renewed, "lost": lost, "cancel": cancel, "orphans": orphans,
                "help_updates": help_updates, "drain": w["draining"], "controls": []}

    def rpc_browser_agent_append_steps(self, p_job, p_worker, p_boot, p_epoch, p_rows):
        late = False
        try:
            self.rpc_browser_agent_fence(p_job, p_worker, p_boot, p_epoch, ["running", "waiting_help"])
        except APIError as exc:
            claimed = any(s for s in self.rows("browser_agent_steps", job_id=str(p_job), kind="claim")
                          if int(s["epoch"]) == int(p_epoch) and str(s.get("worker_id")) == str(p_worker))
            if exc.message in ("lease_lost", "job_not_running", "stale_boot") and claimed:
                late = True
            else:
                raise
        if any(r.get("actor") not in ("worker", "playbook", "model") for r in p_rows):
            _raise("invalid_request")
        inserted = 0
        for r in p_rows:
            if r.get("idem_key") and self.rows("browser_agent_steps", job_id=str(p_job), idem_key=r["idem_key"]):
                continue
            if r.get("screenshot_artifact_id") and not self.rows("browser_agent_artifacts", id=r["screenshot_artifact_id"]):
                raise APIError({"message": "violates foreign key", "code": "23503", "details": None, "hint": None})
            self.tables.setdefault("browser_agent_steps", []).append(self.defaults("browser_agent_steps", {
                **{k: r.get(k) for k in ("idem_key", "kind", "actor", "summary", "tool_name", "args", "result",
                                         "payload_sha256", "url_before", "url_after", "screenshot_artifact_id",
                                         "turn_no", "ok", "duration_ms")},
                "job_id": str(p_job), "epoch": int(p_epoch), "late": late, "worker_id": str(p_worker),
            }))
            inserted += 1
        return {"appended": 0 if late else inserted, "duplicates": len(p_rows) - inserted,
                "late": inserted if late else 0}

    def rpc_browser_agent_checkpoint(self, p_job, p_worker, p_boot, p_epoch, p_seq, p_data):
        self.rpc_browser_agent_fence(p_job, p_worker, p_boot, p_epoch, ["running", "waiting_help"])
        j = self._job(p_job)
        if p_seq <= j["checkpoint_seq"]:
            return False
        j.update(checkpoint=copy.deepcopy(p_data) or {}, checkpoint_seq=p_seq)
        return True

    def rpc_browser_agent_park(self, p_job, p_worker, p_boot, p_epoch):
        self.rpc_browser_agent_fence(p_job, p_worker, p_boot, p_epoch, ["waiting_help"])
        self._job(p_job).update(lease_owner=None, lease_boot_id=None, lease_expires_at=None, hold_until=None)
        self._step(p_job, p_epoch, p_worker, "help.parked", "bdr", "Parked until a helper answers")

    def rpc_browser_agent_finish(self, p_job, p_worker, p_boot, p_epoch, p_status, p_outcome, p_error_kind,
                                 p_error, p_result):
        if p_status not in ("succeeded", "failed", "canceled"):
            _raise("invalid_request")
        j = self._job(p_job)
        if j and j["status"] == p_status:
            return False
        self.rpc_browser_agent_fence(p_job, p_worker, p_boot, p_epoch, ["running", "waiting_help"])
        j.update(status=p_status, outcome=p_outcome, error_kind=p_error_kind, error=(p_error or None),
                 result=p_result, finished_at=self.stamp(), hold_until=None, lease_owner=None,
                 lease_boot_id=None, lease_expires_at=None)
        self._step(p_job, p_epoch, p_worker, "job.finished", "bdr", f"Finished {p_status}")
        return True

    def rpc_browser_agent_requeue(self, p_job, p_worker, p_boot, p_epoch, p_reason, p_retry_at,
                                  p_service_stop=False):
        if p_reason not in ("shutdown", "drain", "crash", "login_spacing", "provider_outage", "interrupted"):
            _raise("invalid_request")
        self.rpc_browser_agent_fence(p_job, p_worker, p_boot, p_epoch, ["running", "waiting_help"])
        w = self._worker(p_worker)
        if p_reason in ("shutdown", "drain") and not (w["draining"] or p_service_stop):
            _raise("invalid_request")
        j, t = self._job(p_job), self._type(self._job(p_job)["agent_type"])
        cleared = dict(hold_until=None, lease_owner=None, lease_boot_id=None, lease_expires_at=None)
        if p_reason == "crash" and j["attempts"] >= t["max_attempts"]:
            j.update(status="failed", error_kind="crashed_repeatedly", error="Crashed too many times",
                     lease_epoch=j["lease_epoch"] + 1, finished_at=self.stamp(), **cleared)
            return "failed"
        if p_reason == "interrupted" and j["interrupted_count"] + 1 > t["max_interruptions"]:
            j.update(status="failed", error_kind="interrupted_repeatedly", error="Interrupted too many times",
                     interrupted_count=j["interrupted_count"] + 1, lease_epoch=j["lease_epoch"] + 1,
                     finished_at=self.stamp(), **cleared)
            return "failed"
        j.update(status="queued", resume_reason="retry" if p_reason == "crash" else p_reason,
                 next_attempt_at=p_retry_at,
                 interrupted_count=j["interrupted_count"] + (1 if p_reason in ("interrupted", "shutdown", "drain") else 0),
                 lease_epoch=j["lease_epoch"] + 1, **cleared)
        self._step(p_job, p_epoch, p_worker, "job.requeued", "bdr", f"Requeued ({p_reason})")
        return "queued"

    def rpc_browser_agent_submit_rfi_capture(self, p_job, p_worker, p_boot, p_epoch, p_artifact, p_external_rfi_id):
        j = self.rpc_browser_agent_fence(p_job, p_worker, p_boot, p_epoch, ["running", "waiting_help"])
        import re as _re
        if j.get("project_id") is None or not _re.fullmatch(r"\d{1,20}", str(p_external_rfi_id)):
            _raise("capture_invalid")
        runs = self.tables.setdefault("pm_rfi_sync_runs", [])
        run = next((r for r in runs if str(r.get("agent_job_id")) == str(p_job)
                    and r.get("apply_attempt") == j["apply_attempt"]), None)
        if run is None:
            run = self.defaults("pm_rfi_sync_runs", {
                "project_id": j["project_id"], "trigger": "agent", "status": "queued", "agent_job_id": str(p_job),
                "apply_attempt": j["apply_attempt"], "detail_artifact_id": str(p_artifact),
                "target_external_rfi_id": str(p_external_rfi_id), "source_email_id": j.get("source_email_id"),
                "attempts": 0, "stats": {},
            })
            runs.append(run)
        self._job(p_job).update(status="applying", outcome="pending", sync_run_id=run["id"], hold_until=None,
                                lease_owner=None, lease_boot_id=None, lease_expires_at=None)
        self._step(p_job, p_epoch, p_worker, "capture.submit", "bdr", f"Capture of RFI {p_external_rfi_id} queued",
                   {"run_id": run["id"], "artifact_id": str(p_artifact)})
        return run["id"]

    # 0157 shared-login RPCs (tests/procore_login_fake.py, the same twins the
    # other FakeDBs mount).
    def rpc_procore_reserve_login(self, p_provider, p_min_interval, p_source):
        from tests import procore_login_fake

        return procore_login_fake.reserve_login(self.tables.setdefault("rfp_harvest_sessions", []), self.now,
                                                p_provider, p_min_interval, p_source)

    def rpc_procore_record_login_failure(self, p_provider, p_error, p_max_failures, p_lock_seconds):
        from tests import procore_login_fake

        return procore_login_fake.record_login_failure(self.tables.setdefault("rfp_harvest_sessions", []), self.now,
                                                       p_provider, p_error, p_max_failures, p_lock_seconds)

    def rpc_browser_agent_append_rfi_supplement(self, p_rfi, p_entry):
        return append_rfi_supplement(self.tables.setdefault("rfis", []), p_rfi, p_entry)

    def rpc_browser_agent_spend_today(self):
        rs = self.tables["browser_agent_runtime_state"][0]
        s = self.tables["browser_agent_settings"][0]
        midnight = self.now.astimezone(_PT).replace(hour=0, minute=0, second=0, microsecond=0)
        start = max(midnight.astimezone(UTC), ts(rs.get("cost_window_started_at")) or midnight.astimezone(UTC))
        spend = sum(float(t.get("cost_usd") or 0) for t in self.tables.get("browser_agent_turns", [])
                    if ts(t["created_at"]) >= start)
        return {"window_started_at": iso(start), "spend_usd": spend,
                "daily_cap_usd": s["daily_cost_cap_usd"], "per_job_cap_usd": s["per_job_cost_cap_usd"]}


    # ── help, settings and spend RPC twins (0157 section 9d/9e) ──
    def rpc_browser_agent_open_help(self, p_job, p_worker, p_boot, p_epoch, p_scope, p_reason, p_title, p_message,
                                    p_shot, p_project, p_hold_seconds, p_expires_hours):
        j = self.rpc_browser_agent_fence(p_job, p_worker, p_boot, p_epoch, ["running"])
        helps = self.tables.setdefault("browser_agent_help_requests", [])
        existing = next((h for h in helps if h["scope_key"] == p_scope and h["status"] in ("open", "answered")), None)
        joined = existing is not None
        if existing is None:
            existing = self.defaults("browser_agent_help_requests", {
                "scope_key": p_scope, "agent_type": j["agent_type"], "reason_code": p_reason, "title": p_title,
                "agent_message": (p_message or None) and p_message[:1000], "screenshot_artifact_id": p_shot,
                "project_id": p_project, "opened_by_job_id": str(p_job), "holder_job_id": str(p_job),
                "expires_at": iso(self.now + timedelta(hours=p_expires_hours)),
            })
            self.check_unique("browser_agent_help_requests", existing)
            helps.append(existing)
        waiters = self.tables.setdefault("browser_agent_help_waiters", [])
        if not any(w["help_id"] == existing["id"] and w["job_id"] == str(p_job) for w in waiters):
            waiters.append({"help_id": existing["id"], "job_id": str(p_job), "added_at": self.stamp()})
        hold = iso(self.now + timedelta(seconds=p_hold_seconds))
        self._job(p_job).update(status="waiting_help", waiting_help_id=existing["id"], hold_until=hold)
        self._step(p_job, p_epoch, p_worker, "help.opened", "bdr",
                   ("Joined" if joined else "Opened") + f" help request {existing['id']} ({p_reason})",
                   {"help_id": existing["id"], "scope": p_scope, "joined": joined})
        return {"help_id": existing["id"], "joined_existing": joined,
                "holder": str(existing["holder_job_id"]) == str(p_job), "hold_until": hold}

    _SETTINGS_SCOPES = {
        "global": ("browser_agent_settings", "id", ("enabled", "daily_cost_cap_usd", "per_job_cost_cap_usd",
                   "help_hold_seconds", "help_reminder_minutes", "help_claim_lapse_minutes", "help_expire_hours",
                   "worker_offline_alert_minutes", "worker_offline_email_hours", "job_ttl_hours",
                   "desired_worker_version")),
        "agent_type": ("browser_agent_types", "key", ("label", "enabled", "provider", "model", "price_in_per_mtok",
                       "price_out_per_mtok", "model_probe_ok_at", "max_concurrency", "max_attempts",
                       "max_interruptions", "max_steps", "max_total_tokens", "max_active_seconds", "host_narrowing",
                       "prompt_version", "priority_default")),
        "worker": ("browser_agent_workers", "id", ("name", "status", "draining", "max_slots", "allowed_types",
                   "allowed_ips")),
    }

    def rpc_browser_agent_update_settings(self, p_scope, p_target, p_patch, p_actor, p_actor_label, p_probe=None):
        if p_scope not in self._SETTINGS_SCOPES:
            _raise("invalid_request")
        table, key_col, allowed = self._SETTINGS_SCOPES[p_scope]
        patch = dict(p_patch or {})
        for k in patch:
            if k not in allowed:
                _raise("invalid_request")
        if p_scope in ("global", "agent_type"):
            patch.update(updated_by=p_actor, updated_at=self.stamp())
        if p_scope == "worker" and "status" in patch:
            if patch["status"] == "active":
                patch.update(approved_by=p_actor, approved_at=self.stamp())
            elif patch["status"] == "revoked":
                patch.update(revoked_by=p_actor, revoked_at=self.stamp(), token_hash=None)
            else:
                _raise("invalid_request")
        target = 1 if p_scope == "global" else p_target
        hits = self.rows(table, **{key_col: target})
        if not hits:
            _raise("invalid_request")
        row = hits[0]
        old = copy.deepcopy(row)
        merged = {**row, **patch}
        if p_scope == "agent_type" and merged.get("enabled") and not (
            merged.get("provider") and merged.get("model") and merged.get("price_in_per_mtok") is not None
            and merged.get("price_out_per_mtok") is not None
        ):
            raise APIError({"message": 'new row violates check constraint "browser_agent_types_enabled_needs_model"',
                            "code": "23514", "details": None, "hint": None})
        row.update(patch)
        changed = [k for k in (p_patch or {}) if old.get(k) != row.get(k)]
        if changed:
            if p_scope == "global":
                row["config_version"] = int(old.get("config_version") or 1) + 1
            else:
                self.after_update(table, row, set(changed))
            for k in changed:
                self.table("browser_agent_settings_changes").insert({
                    "scope": p_scope, "target": p_target, "field": k, "old_value": old.get(k),
                    "new_value": row.get(k), "probe": p_probe, "changed_by": p_actor,
                    "changed_by_label": p_actor_label, "changed_at": self.stamp()}).execute()
            self.tables.setdefault("audit_log", []).append({
                "actor_id": p_actor, "action": "browser_agent.settings_update", "entity": table,
                "entity_id": p_target if p_scope == "worker" else None,
                "payload": {"scope": p_scope, "target": p_target, "fields": changed, "actor_label": p_actor_label}})
        return {"changed": changed, "config_version": self.tables["browser_agent_settings"][0]["config_version"]}

    def rpc_browser_agent_reset_spend_window(self, p_actor, p_actor_label, p_note=None):
        before = self.rpc_browser_agent_spend_today()
        rs = self.tables["browser_agent_runtime_state"][0]
        rs.update(cost_window_started_at=self.stamp(), cost_window_reset_by=p_actor, half_cap_notified_at=None,
                  cap_hit_notified_at=None)
        after = self.rpc_browser_agent_spend_today()
        self.table("browser_agent_settings_changes").insert({
            "scope": "spend", "target": None, "field": "cost_window_reset",
            "old_value": {"window_started_at": before["window_started_at"], "spend_usd": before["spend_usd"]},
            "new_value": {"window_started_at": after["window_started_at"], "note": (p_note or None)},
            "changed_by": p_actor, "changed_by_label": p_actor_label, "changed_at": self.stamp()}).execute()
        self.tables.setdefault("audit_log", []).append({
            "actor_id": p_actor, "action": "browser_agent.spend_reset", "entity": "browser_agent_runtime_state",
            "payload": {"spend_before_usd": before["spend_usd"], "actor_label": p_actor_label, "note": p_note}})
        return after


def append_rfi_supplement(rfis: list[dict], rfi_id, entry: dict) -> bool:
    """0158 browser_agent_append_rfi_supplement: one atomic append, skipped
    when the contribution id is already there."""
    if not (entry or {}).get("contribution_id"):
        _raise("invalid_request")
    row = next((r for r in rfis if str(r.get("id")) == str(rfi_id)), None)
    if row is None:
        return False
    current = list(row.get("external_supplement") or [])
    if not any(isinstance(e, dict) and str(e.get("contribution_id")) == str(entry["contribution_id"]) for e in current):
        row["external_supplement"] = current + [copy.deepcopy(entry)]
    return True


from zoneinfo import ZoneInfo  # noqa: E402

_PT = ZoneInfo("America/Los_Angeles")

"""In-memory Supabase fake for the assistant tests (house convention: own
copy per family). Covers the builder slice app/assistant/repo.py and
app/assistant/principal.py use: select / insert / update / upsert, eq / neq /
in_ / is_ / gt / gte / lt / lte / contains / ilike / or_ / not_, order /
limit / range, rpc for assistant_tool_call_begin, plus the one-active-turn
unique index on assistant_messages and the lease fence on
assistant_tool_calls.

Phase 1 (docs/ASSISTANT_PHASE1_PLAN.md, WI-3a) adds the approvals ledger:
the decide RPC (section 2.3) and the supersede RPC (section 2.2) in Python,
the partial unique index assistant_approvals_one_revert_uq on insert and
update, the column defaults, the immutability and status-rank trigger, the
expired_reason check, and the append-only refusals (no update or delete on
assistant_tool_calls, no delete on assistant_approvals). `FakeDB.now` is
the database clock the RPCs compare expires_at against.

Phase 2 (docs/ASSISTANT_PHASE2_PLAN.md, WI-2) adds: assistant_files with
the 0170 caps trigger in Python (2 GB of live unconsumed staging per user
raises a `file_cap_user` check_violation, 10 per message) and the unique
storage_path; assistant_tasks with assistant_tasks_approval_uq;
assistant_spend_reservations with the one-split-hold-per-task index; the
0174 rpcs assistant_spend_state and assistant_spend_reserve in Python
(Pacific day, live holds reduced by the log rows stamped with their id,
the admission under one lock, money arguments asserted to be strings);
an insert that keeps an explicit id and
raises 23505 on a duplicate primary key; the message delete's `on delete
set null` on assistant_files and assistant_tasks; and FakeStorage for the
staging objects. Updates return only the rows their filters matched, which
is what the claim fence counts.

Phase 3 (docs/ASSISTANT_PHASE3_PLAN.md, WI-3) adds, per 0176: the tables
assistant_schedules (the caps trigger in Python with BYSECOND refused as in
0170, the timezone, params, active_next and ended checks, the approval
index), assistant_schedule_runs (the forward-only status trigger, the
identity columns, no delete, the occurrence key), assistant_memories (the
0170 caps, the pin guard, the content-only updated_at trigger, the
idempotency index), assistant_memory_events (append-only, the widened event
check), assistant_memory_revisions (one pending per memory), assistant_sweeps
(the primary key) and the memory_ids column of assistant_tool_results; and
the RPCs claim_due_assistant_schedules (skip locked through
`locked_schedules` and `locked_runs`, the occurrence key, missed and overlap,
the manual CTE's overlap guard, all from one snapshot), the run channel
ledger, assistant_memory_apply (every op and actor, legal hold, the pending
cap, the delete rewrite), assistant_redact_memory_items (`redact_memory_items`
is its Python twin), assistant_memory_similar (a token Jaccard stand-in for
pg_trgm) and assistant_memory_touch. Every statement that fires a trigger is
all or nothing. `rpc_errors[name]` makes one RPC raise.

Phase 4 (docs/ASSISTANT_PHASE4_PLAN.md, WI-5) adds, per 0180: the tables
assistant_evidence_access (append-only, the approval unique key, the reason
length check), assistant_reports (the path check, the purged pair check, the
approval unique key, the spec and status checks) and plans_reader_notes (the
plain unique key on run, scope, category, version and source key, the kind
and scope checks, category defaulting to an empty string); the columns
assistant_approvals.decision_reason (set once, 10 to 500 characters) and
assistant_messages.server_data (default false); the two AI Estimating hold
kinds (plans_pages, ai_estimate) with their one-hold-per-approval and
one-hold-per-task indexes, the four-kind reserve guard and the spend state
term that counts the settled AND released actuals of those kinds; and the
RPCs assistant_approval_decide_with_reason, assistant_memory_search (a token
overlap stand-in for the tsvector and trigram rank) and search_plans_notes
(a substring stand-in).

Phase 7 (docs/ASSISTANT_PHASE7_PLAN.md, WI-3) adds, per 0190: the eight
router tables (assistant_events, assistant_inbox, assistant_subscriptions,
assistant_digests, assistant_project_mutes, assistant_router_cursors,
assistant_proactive_slots, assistant_external_sends) with their checks and
unique keys, Python twins of the sixteen router RPCs (see the _Phase7Mixin
comment), row builders (event_row, inbox_row, subscription_row, digest_row,
send_row, bell_row, cursor_row, slot_row), and FakeGraphEmail,
FakeTeamsAlerts and FakeEmailBranding (record calls, raise scripted errors;
`install_delivery_fakes` patches the real helper modules). The link-bell
and fold twins follow 0191 (severity from the payload; the fold keeps the
prior precedence in payload folded_from_precedence and the un-fold restores
Phase 8 (docs/ASSISTANT_PHASE8_PLAN.md, WI-5) adds, per 0196: the company
spend identity COMPANY_SPEND_ID; the reserve twin mirrors B2 (kind triage
only under the company id, with no per-person cap check, and the company id
refused for every other kind); Python twins of the triage RPCs (enqueue,
claim, finish with the escalation emit, skip-user, skip-all, counts, company
spend, purge, and the retry helper they share; see the _Phase8Mixin comment);
and the row builders triage_item_row and triage_run_row. `FakeDB.now` is the
real clock (clock_timestamp) of those twins; p_now is the travelled one."""

from __future__ import annotations

import contextlib
import copy
import json
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

SECRET = "unit-test-assistant-token-secret-0123456789abcdef"
USER = "11111111-1111-4111-8111-111111111111"
RUN = "22222222-2222-4222-8222-222222222222"
CONV = "44444444-4444-4444-8444-444444444444"
MSG_USER = "55555555-5555-4555-8555-555555555555"
MSG_ASSISTANT = "66666666-6666-4666-8666-666666666666"
PROJECT = "77777777-7777-4777-8777-777777777777"
PROJECT2 = "88888888-8888-4888-8888-888888888888"
NOW = datetime.now(timezone.utc)


APPROVAL_ID = "99999999-9999-4999-8999-999999999999"


class UniqueViolation(Exception):
    code = "23505"

    def __init__(self, what: str):
        super().__init__(f'duplicate key value violates unique constraint "{what}" (23505)')


class TriggerRefused(Exception):
    """What a refusing trigger or check constraint raises (restrict_violation
    and check_violation)."""

    code = "23001"


class CheckViolation(Exception):
    """A check_violation raised by a trigger or function (SQLSTATE 23514)."""

    code = "23514"


# assistant_approvals (0170 plus 0172).
APPROVAL_IMMUTABLE = (
    "payload", "payload_sha256", "preview", "precondition", "user_id", "user_label", "action_kind",
    "expires_at", "project_id", "conversation_id", "message_id", "reverts_approval_id", "created_at",
)
APPROVAL_RANK = {"pending": 0, "approved": 1, "executing": 2}
REVERT_LIVE = ("pending", "approved", "executing", "executed")
APPROVAL_NULLABLE = (
    "conversation_id", "message_id", "tool_call_id", "project_id", "expired_reason", "decided_by",
    "decided_at", "decided_session_aal", "execute_job_id", "executed_at", "result", "email_log_id",
    "superseded_by", "reverts_approval_id", "decision_reason",
)
# The explicit column list of the supersede RPC's insert.
SUPERSEDE_COLUMNS = (
    "id", "user_id", "user_label", "conversation_id", "message_id", "project_id", "action_kind",
    "payload", "payload_sha256", "preview", "precondition", "status", "expires_at", "reverts_approval_id",
)


# Phase 4 (0180).
AE_HOLD_KINDS = ("plans_pages", "ai_estimate")
RESERVATION_KINDS = ("turn_call", "split_task", "plans_pages", "ai_estimate")
# Phase 8 (0196 A2a, B1): kind triage, held only by the company spend id.
RESERVATION_KINDS += ("triage",)
COMPANY_SPEND_ID = "00000000-0000-0000-0000-000000000001"
REPORT_SPECS = ("bid_invitations", "quote_readiness", "estimator_vs_bid", "project_timeline",
                "project_file_access", "change_history", "ai_usage", "person_evidence")
EVIDENCE_VIEWS = ("activity", "file_access", "notifications", "ai_usage", "estimator_timeline")
NOTE_KINDS = (
    "category_summary", "page_note", "scope", "spec", "schedule", "detail", "question", "brief_summary",
    "system", "assumption", "exclusion", "risk", "rfi", "missing_document", "other_trade", "sheet_summary",
    "spec_highlight", "material_note", "confidence_note",
)
NOTE_KEY = ("run_id", "source_scope", "category", "source_version", "source_key")
REPORT_FILENAME = re.compile(r"^[A-Za-z0-9 ._()-]{1,120}\.xlsx$")
# Tables the 0180 B4 triggers make append-only (no update, no delete).
APPEND_ONLY_PHASE4 = ("assistant_evidence_access",)


def _ts(value) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _D(value, default=None) -> Decimal:
    """numeric as Decimal; None takes `default` (itself a numeric)."""
    if value is None:
        return Decimal("0") if default is None else Decimal(str(default))
    return Decimal(str(value))


def _contains(have, want) -> bool:
    if isinstance(want, dict):
        return isinstance(have, dict) and all(k in have and _contains(have[k], v) for k, v in want.items())
    if isinstance(want, list):
        return isinstance(have, list) and all(any(_contains(h, w) for h in have) for w in want)
    return have == want


class _Query:
    def __init__(self, db: "FakeDB", table: str):
        self.db, self.table_name = db, table
        self.op, self.payload = "select", None
        self.filters = []
        self.orders: list[tuple[str, bool]] = []
        self.limit_n = None
        self.range_lo_hi = None
        self.negate_next = False
        self.ignore_dup = False

    # ── ops ──
    def select(self, *a, **k):
        self.op = "select"
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def upsert(self, payload, on_conflict=None, ignore_duplicates=False, **k):
        self.op, self.payload, self.ignore_dup = "upsert", payload, ignore_duplicates
        self.on_conflict = on_conflict
        return self

    def delete(self):
        self.op = "delete"
        return self

    # ── filters ──
    def _add(self, fn):
        if self.negate_next:
            self.negate_next = False
            self.filters.append(lambda r, f=fn: not f(r))
        else:
            self.filters.append(fn)
        return self

    @property
    def not_(self):
        self.negate_next = True
        return self

    def eq(self, col, val):
        return self._add(lambda r: str(r.get(col)) == str(val) if r.get(col) is not None else val is None)

    def neq(self, col, val):
        return self._add(lambda r: r.get(col) != val)

    def in_(self, col, vals):
        vals = [str(v) for v in vals]
        return self._add(lambda r: str(r.get(col)) in vals)

    def is_(self, col, val):
        assert val == "null"
        return self._add(lambda r: r.get(col) is None)

    def gt(self, col, val):
        return self._add(lambda r: r.get(col) is not None and str(r.get(col)) > str(val))

    def gte(self, col, val):
        return self._add(lambda r: r.get(col) is not None and str(r.get(col)) >= str(val))

    def lt(self, col, val):
        return self._add(lambda r: r.get(col) is not None and str(r.get(col)) < str(val))

    def lte(self, col, val):
        return self._add(lambda r: r.get(col) is not None and str(r.get(col)) <= str(val))

    def contains(self, col, val):
        return self._add(lambda r: _contains(r.get(col), val))

    def ilike(self, col, pattern):
        needle = pattern.strip("*%").lower()
        return self._add(lambda r: needle in str(r.get(col) or "").lower())

    def or_(self, expr):
        self.db.or_exprs.append(expr)
        if expr == "expires_at.is.null,expires_at.gt.now()":
            return self._add(lambda r: r.get("expires_at") is None or r["expires_at"] > self.db.now_iso())
        keyset = re.fullmatch(r"created_at\.lt\.(.+?),and\(created_at\.eq\.(.+?),id\.lt\.(.+?)\)", expr)
        if keyset:  # the inbox keyset cursor (repo.inbox_list before=)
            at, same, ident = keyset.groups()
            return self._add(lambda r: _ts(r["created_at"]) < _ts(at)
                             or (_ts(r["created_at"]) == _ts(same) and str(r["id"]) < ident))
        # The search or=() is accepted without filtering.
        return self

    def order(self, col, desc=False, **k):
        self.orders.append((col, desc))
        return self

    def limit(self, n):
        self.limit_n = n
        return self

    def range(self, lo, hi):
        self.range_lo_hi = (lo, hi)
        return self

    def single(self):
        self.limit_n = 1
        return self

    # ── execute ──
    def _rows(self):
        rows = [r for r in self.db.tables.setdefault(self.table_name, []) if all(f(r) for f in self.filters)]
        for col, desc in reversed(self.orders):
            rows.sort(key=lambda r: (r.get(col) is None, str(r.get(col) or "")), reverse=desc)
        if self.range_lo_hi:
            lo, hi = self.range_lo_hi
            rows = rows[lo: hi + 1]
        if self.limit_n is not None:
            rows = rows[: self.limit_n]
        return rows

    def execute(self):
        # The phase 7 tables take their writes atomically (a row is one
        # statement in Postgres), so two worker threads sharing one FakeDB
        # cannot interleave a check and an append.
        if self.op != "select" and self.table_name in P7_INSERT_HOOKS:
            with self.db._p7_lock:
                return self._execute()
        return self._execute()

    def _execute(self):
        self.db.calls.append((self.op, self.table_name))
        if self.op == "select":
            return SimpleNamespace(data=[dict(r) for r in self._rows()])
        if self.op == "insert":
            payloads = self.payload if isinstance(self.payload, list) else [self.payload]
            out = [self.db._insert(self.table_name, dict(p)) for p in payloads]
            return SimpleNamespace(data=out)
        if self.op == "upsert":
            payloads = self.payload if isinstance(self.payload, list) else [self.payload]
            out = []
            for p in payloads:
                p = dict(p)
                if self.table_name == "plans_reader_notes":
                    p.setdefault("category", "")
                key = getattr(self, "on_conflict", None)
                existing = None
                if key:
                    existing = next((r for r in self.db.tables.setdefault(self.table_name, [])
                                     if all(str(r.get(k)) == str(p.get(k)) for k in key.split(","))), None)
                if existing is not None:
                    if not self.ignore_dup:
                        existing.update(p)
                        out.append(dict(existing))
                    continue
                try:
                    out.append(self.db._insert(self.table_name, dict(p)))
                except UniqueViolation:
                    # ON CONFLICT DO NOTHING on a unique index the on_conflict
                    # key does not name (or a row a racing worker just wrote).
                    if not (self.ignore_dup and self.table_name in P7_INSERT_HOOKS):
                        raise
            return SimpleNamespace(data=out)
        if self.op == "update":
            if self.table_name == "assistant_tool_calls":
                raise TriggerRefused("assistant_tool_calls is append-only (UPDATE refused)")
            if self.table_name == "assistant_memory_events":
                raise TriggerRefused("assistant_memory_events is append-only (UPDATE refused)")
            if self.table_name in APPEND_ONLY_PHASE4:
                raise TriggerRefused(f"{self.table_name} is append-only (UPDATE refused)")
            hook = self.db.UPDATE_HOOKS.get(self.table_name)
            if hook is not None:
                return SimpleNamespace(data=self.db._statement(
                    lambda: [dict(getattr(self.db, hook)(r, dict(self.payload))) for r in self._rows()]))
            out = []
            for r in self._rows():
                if self.table_name == "assistant_approvals":
                    self.db._update_approval(r, dict(self.payload))
                else:
                    r.update(self.payload)
                out.append(dict(r))
            return SimpleNamespace(data=out)
        if self.op == "delete":
            if self.table_name in ("assistant_tool_calls", "assistant_approvals", "assistant_schedule_runs",
                                   "assistant_memory_events", *APPEND_ONLY_PHASE4):
                raise TriggerRefused(f"{self.table_name} is append-only (DELETE refused)")
            victims = self._rows()
            if self.table_name == "assistant_memories":
                self.db._cascade_memories({str(v.get("id")) for v in victims})
            self.db.tables[self.table_name] = [r for r in self.db.tables[self.table_name] if r not in victims]
            if self.table_name == "assistant_messages":
                # assistant_files.message_id and assistant_tasks.message_id
                # reference the message with on delete set null (0170).
                gone = {str(v.get("id")) for v in victims}
                for child in ("assistant_files", "assistant_tasks"):
                    for r in self.db.tables.get(child, []):
                        if str(r.get("message_id")) in gone:
                            r["message_id"] = None
            return SimpleNamespace(data=victims)
        raise AssertionError(self.op)


# ── Phase 7 (0190): the proactive router ─────────────────────────────────────
#
# Tables assistant_events (claim and status columns), assistant_inbox,
# assistant_subscriptions (both partial unique indexes), assistant_digests,
# assistant_project_mutes, assistant_router_cursors, assistant_proactive_slots
# and assistant_external_sends, with their checks, defaults and unique keys,
# and the RPCs of docs/ASSISTANT_PHASE7_PLAN.md section 3 in Python: the
# event claim (skip locked through `locked_events`, the lease, the p_ids
# claim, the lease_exhausted retire) and finish, inbox deliver, digest
# deliver, fold and un-fold (one per-user advisory lock shared by those
# three: `advisory_locks` records the keys taken, `user_lock(user)` hands a
# test the same lock), mark read, feedback (the part B version), snooze,
# subscription set and reset, dismiss, link bell, the send claim and settle
# (including the deferred branch), the feedback report and the purge. The
# ledger inserts inside inbox deliver and digest deliver are in the same
# statement as the bell, all or nothing. A global lock serializes the RPC
# bodies the way row locks do, so two simulated workers can share one FakeDB
# from two threads. Every RPC needs p_now (the real ones default it, the
# repo wrappers always pass it).

P7_ROLES = ("executive", "estimating_admin", "it_admin", "accountant",
            "estimating_engineer_materials", "estimating_engineer_labor")
P7_KIND = re.compile(r"^[a-z][a-z0-9_.]{2,63}$")
P7_SEVERITIES = ("low", "normal", "high", "urgent")
# 0191 link bell: the precedence rule id the router passes in the payload.
P7_RULE_ID_RE = re.compile(r"R[0-9]+[a-z]?")
P7_SEND_ERRORS = ("transient", "permanent", "uncertain", "lease_expired", "channel_off", "opted_out",
                  "stale", "offboarded", "quiet_hours")
P7_ROUTER_KINDS = ("bid.due_not_ready", "todo.due", "assistant.approval_expiring")
P7_INSERT_HOOKS = {
    "assistant_events": "_p7_insert_event",
    "assistant_inbox": "_p7_insert_inbox",
    "assistant_subscriptions": "_p7_insert_subscription",
    "assistant_digests": "_p7_insert_digest",
    "assistant_project_mutes": "_p7_insert_mute",
    "assistant_router_cursors": "_p7_insert_cursor",
    "assistant_proactive_slots": "_p7_insert_slot",
    "assistant_external_sends": "_p7_insert_send",
}
P7_UPDATE_HOOKS = {
    "assistant_inbox": "_p7_update_inbox",
    "assistant_subscriptions": "_p7_update_subscription",
    "assistant_external_sends": "_p7_update_send",
}
P7_RPCS = {
    "assistant_events_claim": "_p7_events_claim",
    "assistant_event_finish": "_p7_event_finish",
    "assistant_inbox_deliver": "_p7_inbox_deliver",
    "assistant_digest_deliver": "_p7_digest_deliver",
    "assistant_inbox_mark_read": "_p7_mark_read",
    "assistant_inbox_feedback": "_p7_feedback",
    "assistant_inbox_snooze": "_p7_snooze",
    "assistant_subscription_set": "_p7_subscription_set",
    "assistant_subscription_reset": "_p7_subscription_reset",
    "assistant_inbox_dismiss": "_p7_dismiss",
    "assistant_inbox_feedback_report": "_p7_feedback_report",
    "assistant_inbox_link_bell": "_p7_link_bell",
    "assistant_external_sends_claim": "_p7_sends_claim",
    "assistant_external_send_settle": "_p7_send_settle",
    "assistant_inbox_fold": "_p7_fold",
    "assistant_proactive_purge": "_p7_purge",
}


def _p7_s(value) -> str:
    """A time as the UTC ISO string the real columns return."""
    return _ts(value).astimezone(timezone.utc).isoformat()


class _Phase7Mixin:
    def _p7_init(self) -> None:
        self._p7_lock = threading.RLock()
        self._user_locks: dict[str, threading.RLock] = {}
        self.advisory_locks: list[str] = []
        self.locked_events: set[str] = set()
        self.locked_sends: set[str] = set()

    # ── locks ──

    def user_lock(self, user_id) -> threading.RLock:
        """The per-user advisory lock that inbox deliver, digest deliver and
        fold take (hashtext('assistant_inbox_deliver:' || user))."""
        return self._user_locks.setdefault(f"assistant_inbox_deliver:{user_id}", threading.RLock())

    @contextlib.contextmanager
    def _p7_advisory(self, user_id):
        lock = self.user_lock(user_id)
        with lock:
            self.advisory_locks.append(f"assistant_inbox_deliver:{user_id}")
            with self._p7_lock:
                yield

    def _p7_rows(self, table: str) -> list[dict]:
        return self.tables.setdefault(table, [])

    # ── checks and defaults per table ──

    def _p7_event_checks(self, row: dict) -> None:
        done = row.get("status") in ("processed", "skipped", "failed")
        if row.get("status") not in ("pending", "claimed", "processed", "skipped", "failed"):
            raise CheckViolation("assistant_events_status_chk (check_violation)")
        if done != (row.get("processed_at") is not None):
            raise CheckViolation("assistant_events_done_chk (check_violation)")
        if (not P7_KIND.match(str(row.get("event_type") or "")) or not 4 <= len(str(row.get("dedupe_key") or "")) <= 300
                or int(row.get("attempts") or 0) < 0 or len(row.get("last_error") or "") > 500
                or row.get("source") not in (None, "bell_tail", "audit_tail", "sweeper", "emit")
                or len(json.dumps(row.get("payload") or {})) > 4096):
            raise CheckViolation("assistant_events_shape_chk (check_violation)")

    def _p7_insert_event(self, row: dict) -> None:
        for col in ("project_id", "subject_user_id", "processed_at", "source", "claimed_by", "claimed_at",
                    "next_attempt_at", "last_error"):
            row.setdefault(col, None)
        row.setdefault("payload", {})
        row.setdefault("occurred_at", self.now_iso())
        row.setdefault("status", "pending")
        row.setdefault("attempts", 0)
        row.setdefault("routed", {})
        self._p7_event_checks(row)
        if any(r.get("dedupe_key") == row.get("dedupe_key") for r in self._p7_rows("assistant_events")):
            raise UniqueViolation("assistant_events_dedupe_key_key")

    def _p7_inbox_checks(self, row: dict) -> None:
        if (row.get("severity") not in P7_SEVERITIES or row.get("title") is None
                or row.get("feedback") not in (None, "useful", "not_useful", "mute", "mute_project")):
            raise CheckViolation("assistant_inbox severity, title or feedback check (check_violation)")
        delivery = row.get("delivery")
        if (delivery not in ("direct", "immediate", "digest", "record")
                or (delivery in ("immediate", "digest") and row.get("deliver_after") is None)
                or (row.get("source_notification_id") is not None and delivery != "record")
                or len(row.get("precedence") or "") > 64 or not 0 <= int(row.get("snooze_count") or 0) <= 3
                or (row.get("event_type") is not None and not P7_KIND.match(str(row["event_type"])))):
            raise CheckViolation("assistant_inbox_delivery_chk (check_violation)")
        if ((row.get("dismissed_at") is None) != (row.get("dismissed_reason") is None)
                or row.get("dismissed_reason") not in (None, "user", "project_closed", "owner_offboarded",
                                                       "superseded", "muted")):
            raise CheckViolation("assistant_inbox_dismissed_chk (check_violation)")

    def _p7_insert_inbox(self, row: dict) -> None:
        for col in ("event_id", "why", "project_id", "delivered_at", "read_at", "feedback", "feedback_at",
                    "event_type", "precedence", "deliver_after", "digest_id", "notification_id",
                    "source_notification_id", "dismissed_at", "dismissed_reason"):
            row.setdefault(col, None)
        row.setdefault("severity", "normal")
        row.setdefault("payload", {})
        row.setdefault("delivery", "direct")
        row.setdefault("snooze_count", 0)
        if row.get("event_type") is None:  # the classify trigger (0190 A5)
            prefix = str(row.get("dedupe_key") or "").split(":")[0]
            row["event_type"] = (None if prefix == "p7" else
                                 {"browser": "assistant.browser", "calendar": "assistant.calendar"}
                                 .get(prefix, "assistant.mailbox_access"))
        self._p7_inbox_checks(row)
        if any(str(r.get("user_id")) == str(row.get("user_id")) and r.get("dedupe_key") == row.get("dedupe_key")
               for r in self._p7_rows("assistant_inbox")):
            raise UniqueViolation("assistant_inbox_dedupe_uq")

    def _p7_update_inbox(self, row: dict, patch: dict) -> dict:
        new = {**row, **patch}
        self._p7_inbox_checks(new)
        row.update(new)
        return row

    def _p7_sub_checks(self, row: dict) -> None:
        if ((row.get("role") is None) == (row.get("user_id") is None) or row.get("severity") not in P7_SEVERITIES
                or row.get("source") not in ("admin", "user", "feedback_auto")
                or (row.get("role") is not None and row.get("source") != "admin")
                or not P7_KIND.match(str(row.get("event_type") or ""))
                or (row.get("role") is not None and row["role"] not in P7_ROLES)):
            raise CheckViolation("assistant_subscriptions check (check_violation)")

    def _p7_insert_subscription(self, row: dict) -> None:
        row.setdefault("role", None)
        row.setdefault("user_id", None)
        row.setdefault("enabled", True)
        row.setdefault("severity", "normal")
        row.setdefault("source", "admin")
        row.setdefault("updated_by", None)
        row.setdefault("updated_at", row["created_at"])
        self._p7_sub_checks(row)
        for r in self._p7_rows("assistant_subscriptions"):
            if r.get("event_type") != row.get("event_type"):
                continue
            if row["user_id"] is None and r.get("user_id") is None and r.get("role") == row["role"]:
                raise UniqueViolation("assistant_subscriptions_role_uq")
            if row["role"] is None and r.get("role") is None and str(r.get("user_id")) == str(row["user_id"]):
                raise UniqueViolation("assistant_subscriptions_user_uq")

    def _p7_update_subscription(self, row: dict, patch: dict) -> dict:
        new = {**row, **patch}
        self._p7_sub_checks(new)
        row.update(new)
        return row

    def _p7_insert_digest(self, row: dict) -> None:
        for col in ("notification_id", "claimed_by"):
            row.setdefault(col, None)
        row.setdefault("item_count", 0)
        row.setdefault("record_count", 0)
        if row.get("slot") not in ("am", "pm"):
            raise CheckViolation("assistant_digests slot check (check_violation)")
        if any(str(r.get("user_id")) == str(row.get("user_id")) and str(r.get("digest_date")) == str(row.get("digest_date"))
               and r.get("slot") == row.get("slot") for r in self._p7_rows("assistant_digests")):
            raise UniqueViolation("assistant_digests_slot_uq")

    def _p7_insert_mute(self, row: dict) -> None:
        row.pop("id", None)
        if any(str(r.get("user_id")) == str(row.get("user_id")) and str(r.get("project_id")) == str(row.get("project_id"))
               for r in self._p7_rows("assistant_project_mutes")):
            raise UniqueViolation("assistant_project_mutes_pkey")

    def _p7_insert_cursor(self, row: dict) -> None:
        row.pop("id", None)
        row.pop("created_at", None)
        row.setdefault("cursor_id", None)
        row.setdefault("updated_by", None)
        row.setdefault("updated_at", self.now_iso())
        if row.get("name") not in ("notifications", "audit_log"):
            raise CheckViolation("assistant_router_cursors name check (check_violation)")
        if any(r.get("name") == row["name"] for r in self._p7_rows("assistant_router_cursors")):
            raise UniqueViolation("assistant_router_cursors_pkey")

    def _p7_insert_slot(self, row: dict) -> None:
        row.pop("id", None)
        row.pop("created_at", None)
        row.setdefault("finished_at", None)
        row.setdefault("result", {})
        if row.get("step") not in ("digest_am", "digest_pm", "purge"):
            raise CheckViolation("assistant_proactive_slots step check (check_violation)")
        if row.get("claimed_by") is None or row.get("claimed_at") is None:
            raise CheckViolation("assistant_proactive_slots claimed_by and claimed_at are not null")
        if any(r.get("step") == row["step"] and str(r.get("slot_date")) == str(row.get("slot_date"))
               for r in self._p7_rows("assistant_proactive_slots")):
            raise UniqueViolation("assistant_proactive_slots_pkey")

    def _p7_send_checks(self, row: dict) -> None:
        if (row.get("status") not in ("pending", "claimed", "sent", "failed", "skipped", "uncertain")
                or not 0 <= int(row.get("attempts") or 0) <= 5
                or (row.get("status") == "sent") != (row.get("sent_at") is not None)
                or (row.get("status") == "claimed" and (row.get("claimed_by") is None or row.get("claimed_at") is None))
                or row.get("error_kind") not in (None, *P7_SEND_ERRORS)
                or (row.get("channel") != "email" and row.get("email_log_id") is not None)
                or (row.get("target_kind"), row.get("channel")) not in (("inbox", "teams"), ("digest", "email"))):
            raise CheckViolation("assistant_external_sends_state_chk (check_violation)")

    def _p7_insert_send(self, row: dict) -> None:
        for col in ("claimed_by", "claimed_at", "next_attempt_at", "sent_at", "error_kind", "email_log_id"):
            row.setdefault(col, None)
        row.setdefault("status", "pending")
        row.setdefault("attempts", 0)
        row.setdefault("updated_at", row["created_at"])
        if row.get("not_before") is None:
            raise CheckViolation("assistant_external_sends not_before is not null")
        self._p7_send_checks(row)
        if any(str(r.get("target_kind")) == str(row["target_kind"]) and str(r.get("target_id")) == str(row["target_id"])
               and r.get("channel") == row["channel"] for r in self._p7_rows("assistant_external_sends")):
            raise UniqueViolation("assistant_external_sends_target_uq")

    def _p7_update_send(self, row: dict, patch: dict) -> dict:
        new = {**row, **patch}
        self._p7_send_checks(new)
        row.update(new)
        return row

    # ── small shared steps ──

    def _p7_bell(self, user_id, type_: str, message: str, metadata: dict, now: datetime) -> str:
        return self._insert("notifications", {
            "user_id": str(user_id), "project_id": None, "type": type_, "message": message,
            "metadata": metadata, "read_at": None, "dismissed_at": None, "created_at": _p7_s(now),
        })["id"]

    def _p7_mark_bells_read(self, user_id, bell_ids, now: datetime) -> None:
        wanted = {str(b) for b in bell_ids if b}
        for n in self._p7_rows("notifications"):
            if str(n.get("id")) in wanted and str(n.get("user_id")) == str(user_id) and n.get("read_at") is None:
                n["read_at"] = _p7_s(now)

    def _p7_user_sub(self, user_id, event_type) -> dict | None:
        return next((s for s in self._p7_rows("assistant_subscriptions")
                     if str(s.get("user_id")) == str(user_id) and s.get("role") is None
                     and s.get("event_type") == event_type), None)

    # ── A11.1 and A11.2 ──

    def _p7_events_claim(self, p: dict) -> list[dict]:
        if not p.get("p_worker") or not str(p["p_worker"]).strip() or p.get("p_now") is None:
            raise CheckViolation("assistant_events_claim: bad arguments (check_violation)")
        now = _ts(p["p_now"])
        lease = timedelta(seconds=max(60, int(600 if p.get("p_lease_seconds") is None else p["p_lease_seconds"])))
        ids = None if p.get("p_ids") is None else {str(i) for i in p["p_ids"]}
        limit = max(1, min(int(50 if p.get("p_limit") is None else p["p_limit"]), 200))

        def body() -> list[dict]:
            events = self._p7_rows("assistant_events")

            def stale(e) -> bool:
                return e.get("status") == "claimed" and _ts(e["claimed_at"]) < now - lease

            for e in events:
                if (e.get("processed_at") is None and stale(e) and int(e.get("attempts") or 0) >= 5
                        and (ids is None or str(e["id"]) in ids)):
                    e.update(status="failed", processed_at=_p7_s(now), last_error="lease_exhausted")
            picked = sorted(
                (e for e in events
                 if e.get("processed_at") is None and int(e.get("attempts") or 0) < 5
                 and (ids is None or str(e["id"]) in ids) and str(e["id"]) not in self.locked_events
                 and (e.get("next_attempt_at") is None or _ts(e["next_attempt_at"]) <= now)
                 and (e.get("status") == "pending" or stale(e))),
                key=lambda e: (_ts(e["occurred_at"]), str(e["id"])))[:limit]
            for e in picked:
                e.update(status="claimed", claimed_by=p["p_worker"], claimed_at=_p7_s(now),
                         attempts=int(e.get("attempts") or 0) + 1)
            return [dict(e) for e in picked]

        with self._p7_lock:
            return self._statement(body)

    def _p7_event_finish(self, p: dict) -> bool:
        status = p.get("p_status")
        if status not in ("processed", "skipped", "failed", "retry"):
            raise CheckViolation("assistant_event_finish: invalid status (check_violation)")
        now = _ts(p["p_now"])
        with self._p7_lock:
            e = next((r for r in self._p7_rows("assistant_events") if str(r["id"]) == str(p["p_event_id"])
                      and r.get("claimed_by") == p["p_worker"] and r.get("status") == "claimed"), None)
            if e is None:
                return False
            err = None if p.get("p_error") is None else str(p["p_error"])[:500]
            routed = e.get("routed") if p.get("p_routed") is None else p["p_routed"]
            if status == "retry":
                dead = int(e.get("attempts") or 0) >= 5
                secs = max(0, int(60 if p.get("p_retry_seconds") is None else p["p_retry_seconds"]))
                e.update(status="failed" if dead else "pending", claimed_by=e["claimed_by"] if dead else None,
                         next_attempt_at=None if dead else _p7_s(now + timedelta(seconds=secs)),
                         processed_at=_p7_s(now) if dead else e.get("processed_at"), last_error=err, routed=routed)
            else:
                e.update(status=status, processed_at=_p7_s(now), next_attempt_at=None,
                         last_error=err if err is not None else e.get("last_error"), routed=routed)
            self._p7_event_checks(e)
            return True

    # ── A11.3 and A11.4 ──

    def _p7_inbox_deliver(self, p: dict) -> dict:
        user, now = str(p["p_user_id"]), _ts(p["p_now"])
        with self._p7_advisory(user):
            return self._statement(lambda: self._p7_inbox_deliver_body(p, user, now))

    def _p7_inbox_deliver_body(self, p: dict, user: str, now: datetime) -> dict:
        row = next((i for i in self._p7_rows("assistant_inbox")
                    if str(i["id"]) == str(p["p_inbox_id"]) and str(i["user_id"]) == user
                    and i.get("delivery") == "immediate" and i.get("delivered_at") is None
                    and i.get("dismissed_at") is None and _ts(i["deliver_after"]) <= now), None)
        if row is None:
            return {"delivered": False}
        bell = None
        if p.get("p_bell_message") is not None:
            bell = self._p7_bell(user, "assistant.inbox", str(p["p_bell_message"])[:300],
                                 {**(p.get("p_bell_metadata") or {}), "inbox_id": str(p["p_inbox_id"]),
                                  "actor_kind": "system"}, now)
        self._p7_update_inbox(row, {"delivered_at": _p7_s(now), "notification_id": bell})
        send = None
        if p.get("p_teams"):
            before = p.get("p_teams_not_before")
            not_before = max(now, _ts(before)) if before is not None else now
            if not any(s.get("target_kind") == "inbox" and str(s.get("target_id")) == str(p["p_inbox_id"])
                       and s.get("channel") == "teams" for s in self._p7_rows("assistant_external_sends")):
                send = self._insert("assistant_external_sends", {
                    "user_id": user, "target_kind": "inbox", "target_id": str(p["p_inbox_id"]), "channel": "teams",
                    "status": "pending", "not_before": _p7_s(not_before), "created_at": _p7_s(now),
                    "updated_at": _p7_s(now)})["id"]
        return {"delivered": True, "notification_id": bell, "teams_send_id": send}

    def _p7_digest_deliver(self, p: dict) -> dict:
        user, now = str(p["p_user_id"]), _ts(p["p_now"])
        if p.get("p_slot") not in ("am", "pm") or p.get("p_slot_at") is None or p.get("p_date") is None:
            raise CheckViolation("assistant_digest_deliver: bad arguments (check_violation)")
        with self._p7_advisory(user):
            return self._statement(lambda: self._p7_digest_deliver_body(p, user, now))

    def _p7_digest_deliver_body(self, p: dict, user: str, now: datetime) -> dict:
        slot_at = _ts(p["p_slot_at"])
        inbox = self._p7_rows("assistant_inbox")

        def due(i) -> bool:
            return (str(i["user_id"]) == user and i.get("delivery") == "digest" and i.get("delivered_at") is None
                    and i.get("dismissed_at") is None and _ts(i["deliver_after"]) <= slot_at)

        if not any(due(i) for i in inbox):
            return {"claimed": False}
        rang = [_ts(d["created_at"]) for d in self._p7_rows("assistant_digests")
                if str(d["user_id"]) == user and d.get("notification_id") is not None]
        since = max(max(rang, default=now - timedelta(days=3)), now - timedelta(days=3))
        if any(str(d["user_id"]) == user and str(d["digest_date"]) == str(p["p_date"]) and d["slot"] == p["p_slot"]
               for d in self._p7_rows("assistant_digests")):
            return {"claimed": False}
        digest = self._insert("assistant_digests", {
            "user_id": user, "digest_date": str(p["p_date"]), "slot": p["p_slot"], "slot_at": _p7_s(slot_at),
            "since_at": _p7_s(since), "claimed_by": p.get("p_worker"), "created_at": _p7_s(now)})
        attached = [i for i in inbox if due(i)]
        for i in attached:
            self._p7_update_inbox(i, {"digest_id": digest["id"], "delivered_at": _p7_s(now)})
        bells = {str(n["id"]): n for n in self._p7_rows("notifications")}
        records = 0
        for i in inbox:
            src = bells.get(str(i.get("source_notification_id")))
            if (str(i["user_id"]) == user and i.get("delivery") == "record" and i.get("dismissed_at") is None
                    and since <= _ts(i["created_at"]) < now and src is not None and str(src.get("user_id")) == user
                    and src.get("dismissed_at") is None and src.get("read_at") is None):
                records += 1
        n_items, bell, send = len(attached), None, None
        if n_items > 0:
            label = "7:00 AM" if p["p_slot"] == "am" else "1:00 PM"
            bell = self._p7_bell(user, "assistant.digest",
                                 f"Your {label} digest: {n_items} {'item' if n_items == 1 else 'items'}.",
                                 {"digest_id": digest["id"], "actor_kind": "system"}, now)
            if p.get("p_email"):
                send = self._insert("assistant_external_sends", {
                    "user_id": user, "target_kind": "digest", "target_id": digest["id"], "channel": "email",
                    "status": "pending", "not_before": _p7_s(now), "created_at": _p7_s(now),
                    "updated_at": _p7_s(now)})["id"]
        row = next(d for d in self._p7_rows("assistant_digests") if d["id"] == digest["id"])
        row.update(item_count=n_items, record_count=records, notification_id=bell)
        return {"claimed": True, "digest_id": digest["id"], "item_count": n_items, "record_count": records,
                "notification_id": bell, "email_send_id": send}

    # ── A11.5 to A11.7 ──

    def _p7_mark_read(self, p: dict) -> int:
        user, now = str(p["p_user_id"]), _ts(p["p_now"])
        ids = None if p.get("p_ids") is None else {str(i) for i in p["p_ids"]}
        with self._p7_lock:
            upd = [i for i in self._p7_rows("assistant_inbox")
                   if str(i["user_id"]) == user and i.get("delivery") in ("immediate", "digest")
                   and i.get("delivered_at") is not None and i.get("read_at") is None
                   and (ids is None or str(i["id"]) in ids)]
            for i in upd:
                i["read_at"] = _p7_s(now)
            self._p7_mark_bells_read(user, [i.get("notification_id") for i in upd], now)
            return len(upd)

    def _p7_feedback(self, p: dict) -> dict:
        user, now, value = str(p["p_user_id"]), _ts(p["p_now"]), p.get("p_value")
        if value not in ("useful", "not_useful", "mute", "mute_project"):
            return {"ok": False, "code": "invalid_feedback"}
        with self._p7_lock:
            return self._statement(lambda: self._p7_feedback_body(p, user, now, value))

    def _p7_feedback_body(self, p: dict, user: str, now: datetime, value: str) -> dict:
        inbox = self._p7_rows("assistant_inbox")
        row = next((i for i in inbox if str(i["id"]) == str(p["p_inbox_id"]) and str(i["user_id"]) == user), None)
        if row is None:
            return {"ok": False, "code": "not_found"}
        kind = row.get("event_type")
        if value == "mute" and (row["delivery"] == "direct" or kind is None
                                or (kind.startswith("assistant.") and kind != "assistant.approval_expiring")):
            return {"ok": False, "code": "not_mutable"}
        if value == "mute_project" and (row["delivery"] == "direct" or row.get("project_id") is None):
            return {"ok": False, "code": "not_mutable"}
        self._p7_update_inbox(row, {"feedback": value, "feedback_at": _p7_s(now)})

        def pending(match) -> None:
            for i in inbox:
                if (str(i["user_id"]) == user and match(i) and i["delivery"] in ("immediate", "digest")
                        and i.get("delivered_at") is None and i.get("dismissed_at") is None):
                    self._p7_update_inbox(i, {"dismissed_at": _p7_s(now), "dismissed_reason": "muted"})

        auto = False
        if value == "mute":
            existing = self._p7_user_sub(user, kind)
            if existing is None:
                self._insert("assistant_subscriptions", {
                    "user_id": user, "event_type": kind, "enabled": False, "severity": "normal", "source": "user",
                    "updated_by": user, "created_at": _p7_s(now), "updated_at": _p7_s(now)})
            else:
                self._p7_update_subscription(existing, {"enabled": False, "source": "user", "updated_by": user,
                                                        "updated_at": _p7_s(now)})
            pending(lambda i: i.get("event_type") == kind)
        elif value == "mute_project":
            if not any(str(m["user_id"]) == user and str(m["project_id"]) == str(row["project_id"])
                       for m in self._p7_rows("assistant_project_mutes")):
                self._insert("assistant_project_mutes", {"user_id": user, "project_id": str(row["project_id"]),
                                                         "created_at": _p7_s(now)})
            pending(lambda i: str(i.get("project_id")) == str(row["project_id"]))
        elif value == "not_useful" and row["delivery"] in ("immediate", "digest") and kind is not None:
            count = sum(1 for i in inbox if str(i["user_id"]) == user and i.get("event_type") == kind
                        and i["delivery"] in ("immediate", "digest") and i.get("feedback") == "not_useful"
                        and i.get("feedback_at") is not None and _ts(i["feedback_at"]) > now - timedelta(days=14))
            if count >= 3 and self._p7_user_sub(user, kind) is None:
                self._insert("assistant_subscriptions", {
                    "user_id": user, "event_type": kind, "enabled": True, "severity": "low",
                    "source": "feedback_auto", "updated_by": user, "created_at": _p7_s(now),
                    "updated_at": _p7_s(now)})
                auto = True
        return {"ok": True, "feedback": value, "event_type": kind, "auto_downgraded": auto}

    def _p7_snooze(self, p: dict) -> dict:
        user, now = str(p["p_user_id"]), _ts(p["p_now"])
        if p.get("p_next_slot") is None or _ts(p["p_next_slot"]) <= now:
            raise CheckViolation("assistant_inbox_snooze: bad slot (check_violation)")
        with self._p7_lock:
            row = next((i for i in self._p7_rows("assistant_inbox")
                        if str(i["id"]) == str(p["p_inbox_id"]) and str(i["user_id"]) == user), None)
            if row is None:
                return {"ok": False, "code": "not_found"}
            if (row["delivery"] not in ("immediate", "digest") or row.get("delivered_at") is None
                    or row.get("dismissed_at") is not None):
                return {"ok": False, "code": "not_mutable"}
            if int(row.get("snooze_count") or 0) >= 3:
                return {"ok": False, "code": "snooze_limit"}
            old_bell = row.get("notification_id")
            self._p7_update_inbox(row, {
                "delivery": "digest", "delivered_at": None, "read_at": None, "digest_id": None,
                "notification_id": None, "deliver_after": _p7_s(p["p_next_slot"]), "precedence": "snoozed",
                "snooze_count": int(row.get("snooze_count") or 0) + 1})
            self._p7_mark_bells_read(user, [old_bell], now)
            return {"ok": True, "inbox_id": row["id"], "next_slot": p["p_next_slot"],
                    "snooze_count": int(row["snooze_count"])}

    # ── A11.8, A11.9, B3 ──

    def _p7_subscription_set(self, p: dict) -> dict:
        role, user, kind = p.get("p_role"), p.get("p_user_id"), p.get("p_event_type")
        if (role is None) == (user is None):
            return {"ok": False, "code": "bad_target"}
        if p.get("p_source") not in ("admin", "user", "feedback_auto") or (role is not None and p["p_source"] != "admin"):
            return {"ok": False, "code": "bad_source"}
        if role is not None and role not in P7_ROLES:
            return {"ok": False, "code": "bad_role"}
        if kind is None or not P7_KIND.match(str(kind)):
            return {"ok": False, "code": "unknown_kind"}
        sev = p.get("p_severity")
        if (p.get("p_enabled") is None or sev not in P7_SEVERITIES
                or (sev == "urgent" and (role is None or kind not in P7_ROUTER_KINDS))):
            return {"ok": False, "code": "invalid_mode"}
        with self._p7_lock:
            if role is not None:
                row = next((s for s in self._p7_rows("assistant_subscriptions")
                            if s.get("role") == role and s.get("user_id") is None and s.get("event_type") == kind), None)
            else:
                row = self._p7_user_sub(user, kind)
            source = "admin" if role is not None else p["p_source"]
            if row is None:
                row = self._insert("assistant_subscriptions", {
                    "role": role, "user_id": None if role is not None else str(user), "event_type": kind,
                    "enabled": bool(p["p_enabled"]), "severity": sev, "source": source,
                    "updated_by": p.get("p_actor"), "created_at": self.now_iso(), "updated_at": self.now_iso()})
            else:
                self._p7_update_subscription(row, {"enabled": bool(p["p_enabled"]), "severity": sev, "source": source,
                                                   "updated_by": p.get("p_actor")})
            return {"ok": True, "subscription": dict(row)}

    def _p7_subscription_reset(self, p: dict) -> bool:
        role, user, kind = p.get("p_role"), p.get("p_user_id"), p.get("p_event_type")
        if (role is None) == (user is None) or kind is None:
            raise CheckViolation("assistant_subscription_reset: bad_target (check_violation)")
        with self._p7_lock:
            subs = self._p7_rows("assistant_subscriptions")
            keep = [s for s in subs if not (
                s.get("event_type") == kind and ((role is not None and s.get("role") == role and s.get("user_id") is None)
                                                 or (user is not None and str(s.get("user_id")) == str(user)
                                                     and s.get("role") is None)))]
            removed = len(keep) != len(subs)
            subs[:] = keep
            return removed

    def _p7_dismiss(self, p: dict) -> int:
        user, now, reason = str(p["p_user_id"]), _ts(p["p_now"]), p.get("p_reason")
        if reason not in ("user", "project_closed", "owner_offboarded", "superseded", "muted"):
            raise CheckViolation("assistant_inbox_dismiss: invalid reason (check_violation)")
        if not p.get("p_ids"):
            return 0
        ids = {str(i) for i in p["p_ids"]}
        with self._p7_lock:
            upd = [i for i in self._p7_rows("assistant_inbox")
                   if str(i["user_id"]) == user and str(i["id"]) in ids and i.get("dismissed_at") is None]
            for i in upd:
                self._p7_update_inbox(i, {"dismissed_at": _p7_s(now), "dismissed_reason": reason})
            self._p7_mark_bells_read(user, [i.get("notification_id") for i in upd], now)
            return len(upd)

    # ── A11.11 link bell ──

    def _p7_link_bell(self, p: dict) -> dict:
        user, now = str(p["p_user_id"]), _ts(p["p_now"])
        key, notif = p.get("p_dedupe_key"), p.get("p_notification_id")
        if (key is None or not str(key).startswith("p7:bell:") or notif is None or p.get("p_bell_at") is None
                or p.get("p_rank") is None or p.get("p_title") is None):
            raise CheckViolation("assistant_inbox_link_bell: bad arguments (check_violation)")
        with self._p7_lock:
            return self._statement(lambda: self._p7_link_bell_body(p, user, now, str(key), str(notif)))

    def _p7_link_bell_body(self, p: dict, user: str, now: datetime, key: str, notif: str) -> dict:
        inbox = self._p7_rows("assistant_inbox")
        row = next((i for i in inbox if str(i["user_id"]) == user and i["dedupe_key"] == key), None)
        rank, bell_at = int(p["p_rank"]), p["p_bell_at"]
        # 0191: severity from p_payload->>'severity' (an allowed value, else
        # normal) and the precedence rule id from p_payload->>'precedence'
        # (^R[0-9]+[a-z]?$, at most 16 characters, else null), both kept out
        # of the stored payload.
        given = dict(p.get("p_payload") or {})
        raw = given.pop("severity", None)
        severity = raw if isinstance(raw, str) and raw in P7_SEVERITIES else "normal"
        rule = given.pop("precedence", None)
        precedence = rule if isinstance(rule, str) and len(rule) <= 16 and P7_RULE_ID_RE.fullmatch(rule) else None
        if row is None:
            made = self._insert("assistant_inbox", {
                "user_id": user, "dedupe_key": key, "event_type": p.get("p_event_type"), "severity": severity,
                "title": p["p_title"], "why": p.get("p_why"),
                "payload": {**given, "rank": rank, "source_bell_at": bell_at,
                            "linked_notification_ids": []},
                "project_id": p.get("p_project_id"), "delivery": "record", "source_notification_id": notif,
                "delivered_at": _p7_s(now), "created_at": _p7_s(now), "precedence": precedence})
            return {"inbox_id": made["id"], "linked": False}
        if row.get("delivery") != "record":
            return {"inbox_id": row["id"], "linked": False, "code": "not_record"}
        linked = list((row.get("payload") or {}).get("linked_notification_ids") or [])
        if str(row.get("source_notification_id")) == notif or notif in linked:
            return {"inbox_id": row["id"], "linked": True, "noop": True}
        cur_rank = int((row.get("payload") or {}).get("rank") or 0)
        cur_at = _ts((row.get("payload") or {}).get("source_bell_at") or row["created_at"])
        new_at = _ts(bell_at)
        later = rank == cur_rank and new_at > cur_at + timedelta(seconds=120)
        new_source = (rank > cur_rank or later
                      or (rank == cur_rank and new_at >= cur_at - timedelta(seconds=120)
                          and (new_at, notif) < (cur_at, str(row.get("source_notification_id")))))
        if new_source:
            if row.get("source_notification_id") is not None:
                linked.append(str(row["source_notification_id"]))
            seen: list[str] = []
            for x in linked:
                if x != notif and x not in seen:
                    seen.append(x)
            linked = seen[:20]
            patch = {
                "event_type": p.get("p_event_type"), "severity": severity, "title": p["p_title"],
                "why": p.get("p_why"), "precedence": precedence, "source_notification_id": notif,
                "project_id": p["p_project_id"] if p.get("p_project_id") is not None else row.get("project_id"),
                "payload": {**given, "rank": rank, "source_bell_at": bell_at,
                            "linked_notification_ids": linked},
            }
            if later:
                patch["created_at"] = _p7_s(new_at)
                if row.get("dismissed_reason") == "user":
                    patch.update(dismissed_at=None, dismissed_reason=None)
            self._p7_update_inbox(row, patch)
        elif len(linked) < 20:
            self._p7_update_inbox(row, {"payload": {**(row.get("payload") or {}),
                                                    "linked_notification_ids": [*linked, notif]}})
        return {"inbox_id": row["id"], "linked": True, "source": new_source}

    # ── A11.12 and A11.13 ──

    def _p7_sends_claim(self, p: dict) -> list[dict]:
        if not p.get("p_worker") or not str(p["p_worker"]).strip() or p.get("p_now") is None:
            raise CheckViolation("assistant_external_sends_claim: bad arguments (check_violation)")
        now = _ts(p["p_now"])
        lease = timedelta(seconds=max(60, int(600 if p.get("p_lease_seconds") is None else p["p_lease_seconds"])))
        limit = max(1, min(int(1 if p.get("p_limit") is None else p["p_limit"]), 50))

        def body() -> list[dict]:
            sends = self._p7_rows("assistant_external_sends")
            for s in sends:
                if s.get("status") == "claimed" and _ts(s["claimed_at"]) < now - lease:
                    self._p7_update_send(s, {"status": "uncertain", "error_kind": "lease_expired",
                                             "claimed_by": None, "updated_at": _p7_s(now)})
            picked = sorted(
                (s for s in sends
                 if s.get("status") == "pending" and _ts(s["not_before"]) <= now and str(s["id"]) not in self.locked_sends
                 and (s.get("next_attempt_at") is None or _ts(s["next_attempt_at"]) <= now)
                 and int(s.get("attempts") or 0) < 5),
                key=lambda s: (_ts(s["not_before"]), str(s["id"])))[:limit]
            for s in picked:
                self._p7_update_send(s, {"status": "claimed", "claimed_by": p["p_worker"], "claimed_at": _p7_s(now),
                                         "attempts": int(s.get("attempts") or 0) + 1, "updated_at": _p7_s(now)})
            return [dict(s) for s in picked]

        with self._p7_lock:
            return self._statement(body)

    def _p7_send_settle(self, p: dict) -> bool:
        outcome, kind = p.get("p_outcome"), p.get("p_error_kind")
        if (outcome not in ("sent", "transient", "permanent", "uncertain", "skipped", "deferred")
                or (kind is not None and kind not in P7_SEND_ERRORS)
                or (outcome == "skipped" and kind not in ("channel_off", "opted_out", "stale", "offboarded"))):
            raise CheckViolation("invalid_outcome (check_violation)")
        now = _ts(p["p_now"])
        secs = timedelta(seconds=max(0, int(60 if p.get("p_retry_seconds") is None else p["p_retry_seconds"])))
        with self._p7_lock:
            s = next((r for r in self._p7_rows("assistant_external_sends") if str(r["id"]) == str(p["p_id"])
                      and r.get("claimed_by") == p["p_worker"] and r.get("status") == "claimed"), None)
            if s is None:
                return False
            attempts = int(s.get("attempts") or 0)
            status = {"sent": "sent", "permanent": "failed", "uncertain": "uncertain", "skipped": "skipped",
                      "transient": "failed" if attempts >= 5 else "pending"}.get(outcome, "pending")
            retry = outcome == "transient" and attempts < 5
            self._p7_update_send(s, {
                "status": status, "sent_at": _p7_s(now) if outcome == "sent" else None,
                "email_log_id": (p.get("p_email_log_id") if outcome == "sent" and s["channel"] == "email"
                                 else s.get("email_log_id")),
                "error_kind": {"sent": None, "transient": "transient", "permanent": "permanent",
                               "uncertain": "uncertain", "skipped": kind}.get(outcome, "quiet_hours"),
                "claimed_by": None if (outcome == "deferred" or retry) else s.get("claimed_by"),
                "next_attempt_at": _p7_s(now + secs) if retry else None,
                "not_before": _p7_s(now + secs) if outcome == "deferred" else s["not_before"],
                "attempts": max(0, attempts - 1) if outcome == "deferred" else attempts,
                "updated_at": _p7_s(now)})
            return True

    # ── A11.14 fold ──

    def _p7_fold(self, p: dict) -> str:
        user, now = str(p["p_user_id"]), _ts(p["p_now"])
        with self._p7_advisory(user):
            row = next((i for i in self._p7_rows("assistant_inbox")
                        if str(i["id"]) == str(p["p_inbox_id"]) and str(i["user_id"]) == user), None)
            if row is None:
                return "none"
            am = p.get("p_am_date") is not None and any(
                str(d["user_id"]) == user and str(d["digest_date"]) == str(p["p_am_date"]) and d["slot"] == "am"
                for d in self._p7_rows("assistant_digests"))
            until = None if p.get("p_fold_until") is None else _ts(p["p_fold_until"])
            if (row["delivery"] == "immediate" and row.get("delivered_at") is None and row.get("dismissed_at") is None
                    and _ts(row["deliver_after"]) <= now and row.get("severity") == "high"
                    and (row.get("event_type") or "") != "assistant.approval_expiring"
                    and _ts(row["deliver_after"]) > _ts(row["created_at"])):
                if p.get("p_am_date") is not None and p.get("p_am_slot_at") is not None and until is not None \
                        and now < until and not am:
                    # 0191: the prior precedence rides in the payload until the un-fold.
                    self._p7_update_inbox(row, {
                        "delivery": "digest", "precedence": "quiet_hours", "deliver_after": _p7_s(p["p_am_slot_at"]),
                        "payload": {**(row.get("payload") or {}), "folded_from_precedence": row.get("precedence")}})
                    return "folded"
                return "deliver"
            if (row["delivery"] == "digest" and row.get("precedence") == "quiet_hours"
                    and row.get("delivered_at") is None and row.get("dismissed_at") is None):
                if am or until is None or now >= until:
                    payload = dict(row.get("payload") or {})
                    patch = {"delivery": "immediate", "deliver_after": _p7_s(now)}
                    if "folded_from_precedence" in payload:
                        patch["precedence"] = payload.pop("folded_from_precedence")
                    patch["payload"] = payload
                    self._p7_update_inbox(row, patch)
                    return "deliver"
                return "wait"
            return "none"

    # ── B4 purge ──

    def _p7_purge(self, p: dict) -> dict:
        days = [p.get(k) for k in ("p_inbox_days", "p_event_days", "p_digest_days", "p_send_days")]
        if p.get("p_now") is None or any(int(d or 0) < 1 for d in days):
            raise CheckViolation("assistant_proactive_purge: bad arguments (check_violation)")
        now = _ts(p["p_now"])
        limit = max(1, min(int(200 if p.get("p_limit") is None else p["p_limit"]), 5000))
        inbox_d, event_d, digest_d, send_d = (timedelta(days=int(d)) for d in days)
        with self._p7_lock:
            held = {str(r["user_id"]) for r in self._p7_rows("assistant_profiles") if r.get("legal_hold")}

            def drop(table, doomed, key=lambda r: r["id"]):
                gone = {str(key(r)) for r in doomed}
                rows = self._p7_rows(table)
                rows[:] = [r for r in rows if str(key(r)) not in gone]
                return len(gone)

            inbox = sorted((i for i in self._p7_rows("assistant_inbox")
                            if i["delivery"] in ("immediate", "digest", "record")
                            and _ts(i["created_at"]) < now - inbox_d and str(i["user_id"]) not in held),
                           key=lambda i: _ts(i["created_at"]))[:limit]
            n_inbox = drop("assistant_inbox", inbox)
            events = sorted((e for e in self._p7_rows("assistant_events")
                             if e.get("processed_at") is not None and _ts(e["processed_at"]) < now - event_d
                             and str(e.get("subject_user_id")) not in held
                             and not any(str(i.get("event_id")) == str(e["id"]) and str(i["user_id"]) in held
                                         for i in self._p7_rows("assistant_inbox"))),
                            key=lambda e: _ts(e["processed_at"]))[:limit]
            gone_events = {str(e["id"]) for e in events}
            for i in self._p7_rows("assistant_inbox"):  # assistant_inbox.event_id: on delete set null
                if str(i.get("event_id")) in gone_events:
                    i["event_id"] = None
            n_events = drop("assistant_events", events)
            digests = sorted((d for d in self._p7_rows("assistant_digests")
                              if _ts(d["created_at"]) < now - digest_d and str(d["user_id"]) not in held),
                             key=lambda d: _ts(d["created_at"]))[:limit]
            gone_digests = {str(d["id"]) for d in digests}
            for i in self._p7_rows("assistant_inbox"):  # assistant_inbox.digest_id: on delete set null
                if str(i.get("digest_id")) in gone_digests:
                    i["digest_id"] = None
            n_digests = drop("assistant_digests", digests)
            slots = sorted((s for s in self._p7_rows("assistant_proactive_slots")
                            if _ts(s["claimed_at"]) < now - digest_d), key=lambda s: _ts(s["claimed_at"]))[:limit]
            n_slots = drop("assistant_proactive_slots", slots, key=lambda s: (s["step"], s["slot_date"]))
            sends = sorted((s for s in self._p7_rows("assistant_external_sends")
                            if s["status"] in ("sent", "failed", "skipped", "uncertain")
                            and _ts(s["created_at"]) < now - send_d and str(s["user_id"]) not in held),
                           key=lambda s: _ts(s["created_at"]))[:limit]
            n_sends = drop("assistant_external_sends", sends)
            projects = {str(r["id"]): r for r in self._p7_rows("projects")}
            mutes = [m for m in self._p7_rows("assistant_project_mutes")
                     if str(m["project_id"]) in projects and str(m["user_id"]) not in held
                     and (projects[str(m["project_id"])].get("abandoned_at") is not None
                          or projects[str(m["project_id"])].get("failed_to_bid_at") is not None
                          or projects[str(m["project_id"])].get("current_stage") in
                          ("declined", "bid_outcome", "pm_only", "cp_only"))][:limit]
            n_mutes = drop("assistant_project_mutes", mutes, key=lambda m: (m["user_id"], m["project_id"]))
            return {"limit": limit, "assistant_inbox": n_inbox, "assistant_events": n_events,
                    "assistant_digests": n_digests, "assistant_proactive_slots": n_slots,
                    "assistant_external_sends": n_sends, "assistant_project_mutes": n_mutes}

    # ── A11.10 feedback report ──

    def _p7_feedback_report(self, p: dict) -> dict:
        since, now = _ts(p["p_since"]), _ts(p["p_now"])
        floor = max(3, int(3 if p.get("p_floor") is None else p["p_floor"]))
        roles = {str(r["id"]): r.get("role") for r in self._p7_rows("profiles")}
        bells = {str(n["id"]): n for n in self._p7_rows("notifications")}
        src = []
        for i in self._p7_rows("assistant_inbox"):
            if i["delivery"] == "direct" or i.get("event_type") is None or _ts(i["created_at"]) < since:
                continue
            own, source = bells.get(str(i.get("notification_id"))), bells.get(str(i.get("source_notification_id")))
            is_read = (bool(source and source.get("read_at")) if i["delivery"] == "record"
                       else bool(i.get("read_at")) or bool(own and own.get("read_at")))
            src.append({"event_type": i["event_type"], "user": str(i["user_id"]),
                        "role": roles.get(str(i["user_id"])) or "unknown", "delivered": i.get("delivered_at") is not None,
                        "read": is_read, "feedback": i.get("feedback"), "snoozed": int(i.get("snooze_count") or 0) > 0})

        def cell(rows: list[dict]) -> dict:
            return {"users": len({r["user"] for r in rows}), "delivered": sum(r["delivered"] for r in rows),
                    "read": sum(r["read"] for r in rows), "useful": sum(r["feedback"] == "useful" for r in rows),
                    "not_useful": sum(r["feedback"] == "not_useful" for r in rows),
                    "muted": sum(r["feedback"] == "mute" for r in rows),
                    "project_muted": sum(r["feedback"] == "mute_project" for r in rows),
                    "snoozed": sum(r["snoozed"] for r in rows)}

        subs = [s for s in self._p7_rows("assistant_subscriptions") if s.get("role") is None]
        kinds = []
        for kind in sorted({r["event_type"] for r in src}):
            total = cell([r for r in src if r["event_type"] == kind])
            if total["users"] < floor:
                kinds.append({"event_type": kind, "suppressed": True, "users": None})
                continue
            role_cells = {role: cell([r for r in src if r["event_type"] == kind and r["role"] == role])
                          for role in sorted({r["role"] for r in src if r["event_type"] == kind})}
            hidden = sum(c["users"] for c in role_cells.values() if c["users"] < floor)
            withheld = 1 <= hidden <= floor - 1
            role_list = [] if withheld else [
                ({"role": role, "suppressed": True, "users": None} if c["users"] < floor
                 else {"role": role, "suppressed": False, **c}) for role, c in role_cells.items()]
            kinds.append({"event_type": kind, "suppressed": False, **total,
                          "auto_downgraded": sum(1 for s in subs if s["event_type"] == kind and s["source"] == "feedback_auto"),
                          "muted_kind": sum(1 for s in subs if s["event_type"] == kind and not s["enabled"]),
                          "roles_suppressed": withheld, "roles": role_list})
        week = now - timedelta(days=7)
        events, slots = self._p7_rows("assistant_events"), self._p7_rows("assistant_proactive_slots")
        by_worker: dict[str, str] = {}
        for s in slots:
            by_worker[s["claimed_by"]] = max(by_worker.get(s["claimed_by"], s["claimed_at"]), s["claimed_at"], key=_ts)
        groups: dict[tuple, int] = {}
        for s in self._p7_rows("assistant_external_sends"):
            if _ts(s["created_at"]) >= week or s["status"] in ("pending", "claimed"):
                k = (s["channel"], s["status"], s.get("error_kind"))
                groups[k] = groups.get(k, 0) + 1
        health = {
            "events_pending": sum(1 for e in events if e.get("processed_at") is None),
            "events_failed_7d": sum(1 for e in events if e["status"] == "failed" and _ts(e["processed_at"]) >= week),
            "events_lease_exhausted_7d": sum(1 for e in events if e["status"] == "failed"
                                             and e.get("last_error") == "lease_exhausted" and _ts(e["processed_at"]) >= week),
            "slot_failures_7d": sum(int((s.get("result") or {}).get("failures") or 0) for s in slots
                                    if _ts(s["claimed_at"]) >= week),
            "workers": [{"worker": w, "last_claim_at": at} for w, at in
                        sorted(by_worker.items(), key=lambda kv: _ts(kv[1]), reverse=True)[:10]],
            "cursors": [{"name": c["name"], "cursor_at": c["cursor_at"], "updated_by": c.get("updated_by"),
                         "updated_at": c.get("updated_at")}
                        for c in sorted(self._p7_rows("assistant_router_cursors"), key=lambda c: c["name"])],
            "sends_7d": [{"channel": ch, "status": st, "error_kind": ek, "count": n}
                         for (ch, st, ek), n in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] is None, kv[0][2] or ""))],
        }
        return {"since": p["p_since"], "floor": floor, "kinds": kinds, "health": health}


# ── Phase 8 (0196): model triage ─────────────────────────────────────────────
#
# Python twins of the triage RPCs of docs/ASSISTANT_PHASE8_PLAN.md 4.1 A6 and
# 4.2 B4 over the tables assistant_triage_runs and assistant_triage_items (with
# their checks): enqueue (switch, epoch, latch, window, the allowlist, one
# item per record row), claim (switch and latch, the epoch sweep, both fuse
# wires and the latch, the lease retirement, the candidate pick with the
# debounce and the batch size, the re-check, an item that failed an answer
# check taken alone), finish (the fence, the per-item skips first, the epoch
# and latch fence, decisions with the escalation event in the same statement,
# the error handling), skip-user, skip-all, counts, company spend and purge,
# plus the retry helper they share. `FakeDB.now` stands in for the real clock
# (clock_timestamp: leases, enqueued_at, claimed_at, the latch time); p_now is
# the travelled clock. The phase 7 lock serializes the bodies, so two
# simulated workers can share one FakeDB.

P8_RPCS = {
    "assistant_triage_enqueue": "_p8_enqueue",
    "assistant_triage_claim": "_p8_claim",
    "assistant_triage_finish": "_p8_finish",
    "assistant_triage_skip_user": "_p8_skip_user",
    "assistant_triage_skip_all": "_p8_skip_all",
    "assistant_triage_counts": "_p8_counts",
    "assistant_triage_company_spend": "_p8_company_spend",
    "assistant_triage_purge": "_p8_purge",
    "assistant_triage_set_enabled": "_p8_set_enabled",
}
P8_ERROR_KINDS = ("cap_global", "cap_test", "spend_check", "ineligible", "model_error", "bad_answer",
                  "truncated", "unpriced", "busy", "timeout", "lease_expired", "switched_off", "fuse", "breaker")
P8_INFRA_KINDS = ("model_error", "timeout", "busy", "spend_check", "unpriced", "lease_expired", "breaker")
P8_SKIP_REASONS = ("cap", "ineligible", "offboarded", "stale", "dismissed", "switched_off", "fuse", "bad_answer")
P8_WORD = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
P8_MODEL = re.compile(r"^[A-Za-z0-9._:/-]{1,120}$")
P8_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PT_ZONE = ZoneInfo("America/Los_Angeles")


def p8_cap_offset_seconds(item_id) -> int:
    """The fake's stable per-item midnight offset in [0, 7200) (the real
    helper uses hashtext; only the range and the stability matter)."""
    import zlib

    return zlib.crc32(str(item_id).encode()) % 7200


def p8_next_midnight(now) -> datetime:
    """The next Pacific midnight strictly after `now` (the retry helper's
    cap branch, before the offset)."""
    local = _ts(now).astimezone(PT_ZONE)
    day = (local + timedelta(days=1)).date()
    return datetime(day.year, day.month, day.day, tzinfo=PT_ZONE).astimezone(timezone.utc)


class _Phase8Mixin:
    def _p8_settings(self) -> dict:
        return next((r for r in self.tables.get("assistant_settings", []) if str(r.get("id")) == "1"), {})

    def _p8_update_settings(self, row: dict, patch: dict) -> dict:
        """The 0170 set_updated_at trigger on assistant_settings: updated_at
        is the database's now() whatever the SET sends. It mirrors the
        trigger only; the B6 switch stamps its epoch from clock_timestamp()
        (D-1), and only the part A fallback of repo.set_triage_enabled reads
        updated_at back as the epoch."""
        row.update(patch)
        row["updated_at"] = self.now_iso()
        return row

    def _p8_items(self) -> list[dict]:
        return self.tables.setdefault("assistant_triage_items", [])

    def _p8_runs(self) -> list[dict]:
        return self.tables.setdefault("assistant_triage_runs", [])

    def _p8_run(self, run_id) -> dict | None:
        return next((r for r in self._p8_runs() if str(r.get("id")) == str(run_id)), None)

    def _p8_checks(self, item: dict) -> None:
        if (item.get("status") not in ("pending", "claimed", "decided", "skipped", "failed")
                or item.get("skip_reason") not in (None, *P8_SKIP_REASONS)
                or not 0 <= int(item.get("attempts") or 0) <= 3
                or item.get("decision") not in (None, "none", "suggest", "escalate")
                or item.get("severity_hint") not in (None, "normal", "high", "urgent")
                or (item.get("status") == "decided") != (item.get("decision") is not None)
                or (item.get("decision") == "escalate" and item.get("severity_hint") is None)
                or (item.get("status") == "skipped") != (item.get("skip_reason") is not None)):
            raise CheckViolation("assistant_triage_items check (check_violation)")

    def _p8_set(self, item: dict, **fields) -> None:
        candidate = {**item, **fields}
        self._p8_checks(candidate)
        item.update(fields)

    @staticmethod
    def _p8_bad(name: str) -> None:
        raise CheckViolation(f"{name}: bad arguments (check_violation)")

    # ── the retry helper (A4) ──

    def _p8_retry(self, item: dict, kind: str, p_now) -> str | None:
        if item.get("status") != "claimed":
            return None
        stamp = _p7_s(p_now)
        if kind in ("bad_answer", "truncated"):
            attempts = min(int(item.get("attempts") or 0) + 1, 3)
            if attempts >= 3:
                status = "failed" if kind == "truncated" else "skipped"
                self._p8_set(item, attempts=attempts, status=status,
                             skip_reason=None if kind == "truncated" else "bad_answer",
                             next_attempt_at=None, updated_at=stamp)
                return status
            wait = timedelta(seconds=60) if attempts == 1 else timedelta(minutes=5)
            self._p8_set(item, attempts=attempts, status="pending", next_attempt_at=_p7_s(_ts(p_now) + wait),
                         updated_at=stamp)
            return "pending"
        if kind in P8_INFRA_KINDS:
            retries = int(item.get("infra_retries") or 0)
            wait = min(60 * 2 ** min(retries, 10), 900)
            self._p8_set(item, infra_retries=min(retries + 1, 1000), status="pending",
                         next_attempt_at=_p7_s(_ts(p_now) + timedelta(seconds=wait)), updated_at=stamp)
            return "pending"
        if kind == "cap":
            at = p8_next_midnight(p_now) + timedelta(seconds=p8_cap_offset_seconds(item["id"]))
            self._p8_set(item, cap_deferred_at=item.get("cap_deferred_at") or stamp, status="pending",
                         next_attempt_at=_p7_s(at), updated_at=stamp)
            return "pending"
        raise CheckViolation("assistant_triage_item_retry: unknown kind (check_violation)")

    # ── A6.1 enqueue ──

    def _p8_enqueue(self, p: dict) -> int:
        now, window, limit = p.get("p_now"), p.get("p_window_hours"), p.get("p_limit")
        if now is None or window is None or not 1 <= int(window) <= 72 or limit is None or not 1 <= int(limit) <= 1000:
            self._p8_bad("assistant_triage_enqueue")
        with self._p7_lock:
            sett = self._p8_settings()
            if (sett.get("triage_enabled") is not True or sett.get("triage_enabled_at") is None
                    or sett.get("triage_fuse_tripped_at") is not None):
                return 0
            p_now = _ts(now)
            floor = max(p_now - timedelta(hours=int(window)), _ts(sett["triage_enabled_at"]))
            allow = None if p.get("p_user_ids") is None else {str(u) for u in p["p_user_ids"]}
            have = {(str(t["user_id"]), str(t["inbox_id"])) for t in self._p8_items()}
            rows = [r for r in self.tables.get("assistant_inbox", [])
                    if r.get("delivery") == "record" and r.get("dismissed_at") is None
                    and str(r.get("event_type") or "").startswith("bell.")
                    and floor <= _ts(r["created_at"]) <= p_now
                    and (allow is None or str(r.get("user_id")) in allow)
                    and (str(r.get("user_id")), str(r.get("id"))) not in have]
            rows.sort(key=lambda r: (_ts(r["created_at"]), str(r["id"])))
            for r in rows[: int(limit)]:
                self._p8_items().append(triage_item_row(
                    id=str(uuid.uuid4()), user_id=str(r["user_id"]), inbox_id=str(r["id"]),
                    event_type=r["event_type"], project_id=r.get("project_id"),
                    source_notification_id=r.get("source_notification_id"), created_at=_p7_s(p_now),
                    updated_at=_p7_s(p_now), enqueued_at=self.now_iso()))
            return len(rows[: int(limit)])

    # ── A6.2 claim ──

    def _p8_fuse_sample(self, sett: dict, p_now) -> tuple[int, int, int]:
        epoch = _ts(sett["triage_enabled_at"])
        runs = {str(r["id"]): r for r in self._p8_runs()}

        def after(t: dict) -> bool:
            run = runs.get(str(t.get("run_id")))
            return run is not None and _ts(run["claimed_at"]) >= epoch

        decided = [t for t in self._p8_items() if t.get("status") == "decided" and after(t)]
        decided.sort(key=lambda t: (_ts(t["decided_at"]), str(t["id"])), reverse=True)
        sample = decided[:50]
        hour = [t for t in decided if t.get("decision") == "escalate"
                and _ts(t["decided_at"]) > _ts(p_now) - timedelta(hours=1)]
        return len(sample), sum(1 for t in sample if t.get("decision") == "escalate"), len(hour)

    def _p8_live_run(self, user_id) -> bool:
        return any(str(r.get("user_id")) == str(user_id) and r.get("status") == "claimed"
                   and _ts(r["lease_until"]) > self.now for r in self._p8_runs())

    def _p8_claim(self, p: dict):
        worker, now = p.get("p_worker"), p.get("p_now")
        lease, debounce, batch = p.get("p_lease_seconds"), p.get("p_debounce_seconds"), p.get("p_batch_max")
        ratio, per_hour = p.get("p_fuse_ratio"), p.get("p_fuse_per_hour")
        if (worker is None or not 1 <= len(str(worker)) <= 200 or now is None
                or lease is None or not 60 <= int(lease) <= 1800 or debounce is None or not 0 <= int(debounce) <= 3600
                or batch is None or not 1 <= int(batch) <= 20 or ratio is None or not 0 <= float(ratio) <= 1
                or per_hour is None or not 0 <= int(per_hour) <= 5000):
            self._p8_bad("assistant_triage_claim")
        with self._p7_lock:
            sett = self._p8_settings()
            p_now = _ts(now)
            stamp = _p7_s(p_now)
            # 1. Switch and latch.
            if sett.get("triage_enabled") is not True or sett.get("triage_enabled_at") is None:
                return {"switched_off": True}
            if sett.get("triage_fuse_tripped_at") is not None:
                return {"fuse": True, "tripped_now": False}
            epoch = _ts(sett["triage_enabled_at"])
            # 2. Epoch sweep (real clock against real clock).
            old = [t for t in self._p8_items() if t.get("status") == "pending" and _ts(t["enqueued_at"]) < epoch]
            old.sort(key=lambda t: _ts(t["enqueued_at"]))
            for t in old[:500]:
                self._p8_set(t, status="skipped", skip_reason="switched_off", next_attempt_at=None, updated_at=stamp)
            # 3. Fuse.
            sample, sample_esc, hour_esc = self._p8_fuse_sample(sett, p_now)
            if ((float(ratio) > 0 and sample >= 20 and sample_esc / max(sample, 1) > float(ratio))
                    or (int(per_hour) > 0 and hour_esc > int(per_hour))):
                tripped = sett.get("triage_fuse_tripped_at") is None
                if tripped:
                    sett["triage_fuse_tripped_at"] = self.now_iso()
                return {"fuse": True, "tripped_now": tripped}
            # 4. Retire expired claims.
            for run in sorted((r for r in self._p8_runs() if r.get("status") == "claimed"
                               and _ts(r["lease_until"]) < self.now), key=lambda r: _ts(r["lease_until"]))[:50]:
                run.update(status="failed", error_kind="lease_expired", finished_at=stamp)
                for t in sorted((t for t in self._p8_items() if str(t.get("run_id")) == str(run["id"])
                                 and t.get("status") == "claimed"), key=lambda t: str(t["id"])):
                    self._p8_retry(t, "lease_expired", p_now)
            # 5. Candidates.
            allow = None if p.get("p_user_ids") is None else {str(u) for u in p["p_user_ids"]}

            def eligible(t: dict) -> bool:
                return (t.get("status") == "pending"
                        and (t.get("next_attempt_at") is None or _ts(t["next_attempt_at"]) <= p_now)
                        and _ts(t["enqueued_at"]) >= epoch)

            groups: dict[str, list[dict]] = {}
            for t in self._p8_items():
                if eligible(t) and (allow is None or str(t["user_id"]) in allow) \
                        and not self._p8_live_run(t["user_id"]):
                    groups.setdefault(str(t["user_id"]), []).append(t)
            cands = [(min(_ts(t["created_at"]) for t in ts), uid) for uid, ts in groups.items()
                     if min(_ts(t["created_at"]) for t in ts) <= p_now - timedelta(seconds=int(debounce))
                     or len(ts) >= int(batch)]
            for _first_at, uid in sorted(cands)[:5]:
                # 6. Re-check under the per-person lock.
                self.advisory_locks.append(f"assistant_triage:{uid}")
                if self._p8_live_run(uid):
                    continue
                mine = sorted((t for t in self._p8_items() if str(t["user_id"]) == uid and eligible(t)),
                              key=lambda t: (_ts(t["created_at"]), str(t["id"])))
                if not mine:
                    continue
                # 7. The batch (an item that failed an answer check goes alone), then the run.
                if int(mine[0].get("attempts") or 0) > 0:
                    batch_items = [mine[0]]
                else:
                    batch_items = [t for t in mine if int(t.get("attempts") or 0) == 0][: int(batch)]
                if not batch_items:
                    continue
                run = triage_run_row(
                    id=str(uuid.uuid4()), user_id=uid, claimed_by=str(worker), claimed_at=self.now_iso(),
                    lease_until=(self.now + timedelta(seconds=int(lease))).isoformat(),
                    item_count=len(batch_items), created_at=stamp)
                self._p8_runs().append(run)
                for t in batch_items:
                    self._p8_set(t, status="claimed", run_id=run["id"], updated_at=stamp)
                inbox = {str(r["id"]): r for r in self.tables.get("assistant_inbox", [])}
                return {"run_id": run["id"], "user_id": uid, "items": [
                    {"id": t["id"], "inbox_id": t["inbox_id"], "event_type": t["event_type"],
                     "project_id": t.get("project_id"), "source_notification_id": t.get("source_notification_id"),
                     "created_at": t["created_at"], "inbox_created_at": inbox[str(t["inbox_id"])]["created_at"],
                     "cap_deferred_at": t.get("cap_deferred_at")}
                    for t in batch_items if str(t["inbox_id"]) in inbox]}
            return None

    # ── A6.3 finish ──

    def _p8_finish(self, p: dict) -> dict:
        status, error = p.get("p_status"), p.get("p_error_kind")
        entries = p.get("p_items") if p.get("p_items") is not None else []
        if (p.get("p_run_id") is None or p.get("p_worker") is None or p.get("p_now") is None or status is None
                or status not in ("decided", "skipped", "failed") or not isinstance(entries, list)
                or (status == "decided" and error is not None)
                or (status != "decided" and error not in P8_ERROR_KINDS)):
            self._p8_bad("assistant_triage_finish")
        usage = p.get("p_usage") or {}
        with self._p7_lock:
            p_now = _ts(p["p_now"])
            stamp = _p7_s(p_now)
            run = self._p8_run(p["p_run_id"])
            if (run is None or run.get("claimed_by") != p["p_worker"] or run.get("status") != "claimed"
                    or _ts(run["lease_until"]) <= self.now):
                return {"fenced": False, "emitted": 0}
            sett = self._p8_settings()

            def claimed() -> list[dict]:
                return sorted((t for t in self._p8_items() if str(t.get("run_id")) == str(run["id"])
                               and t.get("status") == "claimed"), key=lambda t: str(t["id"]))

            def item(entry: dict) -> dict | None:
                if not P8_UUID.match(str(entry.get("id") or "")):
                    return None
                return next((t for t in claimed() if str(t["id"]) == str(entry["id"])), None)

            # Per-item skips first, for every status (MC-6).
            for entry in entries:
                if not isinstance(entry, dict) or not entry.get("skip_reason"):
                    continue
                if entry["skip_reason"] not in ("dismissed", "stale", "cap"):
                    raise CheckViolation("assistant_triage_finish: bad skip_reason (check_violation)")
                target = item(entry)
                if target is not None:
                    self._p8_set(target, status="skipped", skip_reason=entry["skip_reason"],
                                 next_attempt_at=None, updated_at=stamp)
            # The company switch, fenced on the epoch, then the latch.
            if (sett.get("triage_enabled") is not True or sett.get("triage_enabled_at") is None
                    or _ts(run["claimed_at"]) < _ts(sett["triage_enabled_at"])):
                status, error = "skipped", "switched_off"
            elif sett.get("triage_fuse_tripped_at") is not None:
                status, error = "skipped", "fuse"
            emitted = 0
            if status == "decided":
                for entry in entries:
                    if not isinstance(entry, dict) or entry.get("skip_reason"):
                        continue
                    target = item(entry)
                    decision, reason = entry.get("decision"), entry.get("reason")
                    action, severity = entry.get("action"), entry.get("severity_hint")
                    if target is None or decision not in ("none", "suggest", "escalate"):
                        continue
                    if reason is None or not P8_WORD.match(str(reason)):
                        continue
                    if decision == "suggest":
                        if action is None or not P8_WORD.match(str(action)):
                            continue
                    else:
                        action = None
                    if decision == "escalate":
                        if severity not in ("normal", "high", "urgent"):
                            continue
                    else:
                        severity = None
                    self._p8_set(target, status="decided", decision=decision, reason=reason, action=action,
                                 severity_hint=severity, decided_at=stamp, next_attempt_at=None, updated_at=stamp)
                    if decision == "escalate":
                        key = f"p8:esc:{target['inbox_id']}"
                        event = next((e for e in self._p7_rows("assistant_events") if e.get("dedupe_key") == key), None)
                        if event is None:
                            event = self._insert("assistant_events", {
                                "event_type": "assistant.triage_escalation", "dedupe_key": key,
                                "project_id": target.get("project_id"), "subject_user_id": target["user_id"],
                                "payload": {"inbox_id": target["inbox_id"], "triage_item_id": target["id"],
                                            "run_id": run["id"], "reason": reason, "severity_hint": severity,
                                            "source_kind": target["event_type"], "emitted_at": self.now_iso()},
                                "occurred_at": stamp, "status": "pending", "source": "emit"})
                            emitted += 1
                        target["escalation_event_id"] = event["id"]
                for left in claimed():
                    self._p8_retry(left, "bad_answer", p_now)
            elif error in ("cap_global", "cap_test"):
                for left in claimed():
                    self._p8_retry(left, "cap", p_now)
            elif error in ("ineligible", "switched_off", "fuse"):
                for left in claimed():
                    self._p8_set(left, status="skipped", skip_reason=error, next_attempt_at=None, updated_at=stamp)
            else:
                for left in claimed():
                    self._p8_retry(left, error, p_now)
            model = usage.get("model")

            def number(key):
                value = usage.get(key)
                return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

            run.update(
                status=status, error_kind=error,
                model=model if model is not None and P8_MODEL.match(str(model)) else run.get("model"),
                reservation_id=(usage["reservation_id"] if P8_UUID.match(str(usage.get("reservation_id") or ""))
                                else run.get("reservation_id")),
                input_tokens=number("input_tokens"), cached_tokens=number("cached_tokens"),
                output_tokens=number("output_tokens"), cost_usd=number("cost_usd"), finished_at=stamp)
            return {"fenced": True, "emitted": emitted, "status": status, "error_kind": error}

    # ── A6.4 skip-user, A6.8 skip-all ──

    def _p8_skip_user(self, p: dict) -> int:
        if (p.get("p_user_id") is None or p.get("p_now") is None
                or p.get("p_reason") not in ("offboarded", "switched_off", "ineligible")):
            self._p8_bad("assistant_triage_skip_user")
        with self._p7_lock:
            uid, stamp = str(p["p_user_id"]), _p7_s(p["p_now"])
            self.advisory_locks.append(f"assistant_triage:{uid}")
            kind = "ineligible" if p["p_reason"] == "ineligible" else "switched_off"
            for run in self._p8_runs():
                if str(run.get("user_id")) == uid and run.get("status") == "claimed":
                    run.update(status="failed", error_kind=kind, finished_at=stamp)
            mine = sorted((t for t in self._p8_items() if str(t["user_id"]) == uid
                           and t.get("status") in ("pending", "claimed")),
                          key=lambda t: (_ts(t["created_at"]), str(t["id"])))[:500]
            for t in mine:
                self._p8_set(t, status="skipped", skip_reason=p["p_reason"], next_attempt_at=None, updated_at=stamp)
            return len(mine)

    def _p8_skip_all(self, p: dict) -> int:
        reason, limit = p.get("p_reason"), p.get("p_limit")
        if reason not in ("switched_off", "fuse") or p.get("p_now") is None or limit is None \
                or not 1 <= int(limit) <= 1000:
            self._p8_bad("assistant_triage_skip_all")
        with self._p7_lock:
            sett = self._p8_settings()
            if reason == "fuse" and sett.get("triage_fuse_tripped_at") is None:
                return 0
            if (reason == "switched_off" and sett.get("enabled") is True and sett.get("proactive_enabled") is True
                    and sett.get("triage_enabled") is True):
                return 0
            stamp = _p7_s(p["p_now"])
            pending = sorted((t for t in self._p8_items() if t.get("status") == "pending"),
                             key=lambda t: (_ts(t["created_at"]), str(t["id"])))[: int(limit)]
            for t in pending:
                self._p8_set(t, status="skipped", skip_reason=reason, next_attempt_at=None, updated_at=stamp)
            return len(pending)

    # ── A6.7 company spend, A6.5 counts ──

    def _p8_company_spend(self, p: dict) -> dict:
        since = self._pacific_day_start()
        log = self.tables.get("llm_call_log", [])
        spent = sum((_D(r.get("cost_usd")) for r in log if str(r.get("user_id")) == COMPANY_SPEND_ID
                     and r.get("created_at") and _ts(r["created_at"]) >= since), Decimal("0"))
        held = Decimal("0")
        for res in self.tables.get("assistant_spend_reservations", []):
            if (res.get("kind") != "triage" or str(res.get("user_id")) != COMPANY_SPEND_ID
                    or res.get("status") != "active" or _ts(res["expires_at"]) <= self.now):
                continue
            logged = sum((_D(r.get("cost_usd")) for r in log
                          if str(r.get("spend_reservation_id")) == str(res["id"])), Decimal("0"))
            held += max(_D(res.get("amount_usd")) - logged, Decimal("0"))
        return {"spent": float(spent), "held": float(held), "since": since.isoformat()}

    def _p8_counts(self, p: dict) -> dict:
        if p.get("p_since") is None or p.get("p_now") is None:
            self._p8_bad("assistant_triage_counts")
        since, p_now = _ts(p["p_since"]), _ts(p["p_now"])
        sett = self._p8_settings()
        sample = sample_esc = hour_esc = 0
        if sett.get("triage_enabled_at") is not None:
            sample, sample_esc, hour_esc = self._p8_fuse_sample(sett, p_now)
        items = [t for t in self._p8_items() if _ts(t["created_at"]) >= since]
        runs = [r for r in self._p8_runs() if _ts(r["created_at"]) >= since]

        def tally(rows, key):
            out: dict = {}
            for r in rows:
                if r.get(key) is not None:
                    out[r[key]] = out.get(r[key], 0) + 1
            return out

        state = self._spend_state({"p_user_id": None, "p_user_cap_default": 0, "p_global_cap_default": 50})
        browser = Decimal("0")
        log = self.tables.get("llm_call_log", [])
        for res in self.tables.get("assistant_spend_reservations", []):
            if res.get("kind") == "browser_task" and res.get("status") == "active" and _ts(res["expires_at"]) > self.now:
                logged = sum((_D(r.get("cost_usd")) for r in log
                              if str(r.get("spend_reservation_id")) == str(res["id"])), Decimal("0"))
                browser += max(_D(res.get("amount_usd")) - logged, Decimal("0"))
        company = self._p8_company_spend({})
        roles = {str(r["id"]): r.get("role") for r in self.tables.get("profiles", [])}
        cells: dict[str, dict] = {}
        for t in items:
            role = roles.get(str(t["user_id"])) or "unknown"
            cell = cells.setdefault(role, {"people": set(), "items": 0, "escalations": 0})
            cell["people"].add(str(t["user_id"]))
            cell["items"] += 1
            cell["escalations"] += 1 if t.get("decision") == "escalate" else 0
        hidden = any(len(c["people"]) < 3 for c in cells.values())
        return {
            "since": _p7_s(since), "triage_enabled": bool(sett.get("triage_enabled")),
            "triage_enabled_at": sett.get("triage_enabled_at"), "fuse_tripped_at": sett.get("triage_fuse_tripped_at"),
            "status": tally(items, "status"), "decision": tally(items, "decision"), "reason": tally(items, "reason"),
            "severity_hint": tally(items, "severity_hint"), "skip_reason": tally(items, "skip_reason"),
            "error_kind": tally(runs, "error_kind"), "runs": len(runs),
            "cost_usd": float(sum((_D(r.get("cost_usd")) for r in runs), Decimal("0"))),
            "cap_deferred_pending": sum(1 for t in self._p8_items()
                                        if t.get("status") == "pending" and t.get("cap_deferred_at") is not None),
            "fuse": {"sample": sample, "sample_escalations": sample_esc, "hour_escalations": hour_esc},
            "spend": {"global_cap": state["global_cap"], "global_spent": state["global_spent"],
                      "global_held": state["global_held"], "browser_held": float(browser),
                      "triage_spent": company["spent"], "triage_held": company["held"]},
            "roles": [] if hidden else [{"role": k, "items": v["items"], "escalations": v["escalations"]}
                                        for k, v in sorted(cells.items())],
            "roles_suppressed": hidden,
        }

    # ── B4 purge ──

    def _p8_purge(self, p: dict) -> dict:
        days, limit = p.get("p_run_days"), p.get("p_limit")
        if p.get("p_now") is None or days is None or not 7 <= int(days) <= 3650 or limit is None \
                or not 1 <= int(limit) <= 5000:
            self._p8_bad("assistant_triage_purge")
        with self._p7_lock:
            cutoff = _ts(p["p_now"]) - timedelta(days=int(days))
            held = {str(r["user_id"]) for r in self.tables.get("assistant_profiles", []) if r.get("legal_hold")}
            fresh_runs = {str(t.get("run_id")) for t in self._p8_items() if _ts(t["created_at"]) >= cutoff}
            doomed = sorted((r for r in self._p8_runs() if r.get("status") != "claimed"
                             and _ts(r["created_at"]) < cutoff and str(r["user_id"]) not in held
                             and str(r["id"]) not in fresh_runs), key=lambda r: _ts(r["created_at"]))[: int(limit)]
            gone = {str(r["id"]) for r in doomed}
            self.tables["assistant_triage_runs"] = [r for r in self._p8_runs() if str(r["id"]) not in gone]
            for t in self._p8_items():          # run_id references the run on delete set null
                if str(t.get("run_id")) in gone:
                    t["run_id"] = None
            return {"locked": True, "assistant_triage_runs": len(gone)}

    # ── B6 the company triage switch ──

    def _p8_set_enabled(self, p: dict) -> dict:
        """0196 B6 in Python: one guarded update of the company row; a flip
        to on sets the epoch from the database clock (FakeDB.now) and clears
        the latch in the same update; no flip writes nothing."""
        user, enabled = p.get("p_user_id"), p.get("p_enabled")
        if user is None or not isinstance(enabled, bool) or str(user) == COMPANY_SPEND_ID:
            self._p8_bad("assistant_triage_set_enabled")
        with self._p7_lock:
            row = self._p8_settings()
            if not row:
                raise AssertionError("assistant_triage_set_enabled: the settings row is missing")
            # The column is NOT NULL DEFAULT false (A2): a row without the
            # key reads as off, as SQL would see it.
            changed = bool(row.get("triage_enabled")) is not enabled
            if changed:
                patch: dict = {"triage_enabled": enabled, "updated_by": str(user)}
                if enabled:
                    patch.update(triage_enabled_at=self.now_iso(), triage_fuse_tripped_at=None)
                self._p8_update_settings(row, patch)
            return {"triage_enabled": row.get("triage_enabled"), "triage_enabled_at": row.get("triage_enabled_at"),
                    "triage_fuse_tripped_at": row.get("triage_fuse_tripped_at"),
                    "updated_at": row.get("updated_at"), "changed": changed}


def triage_item_row(**over) -> dict:
    """A pending assistant_triage_items row (0196 A4)."""
    stamp = P7_T0.isoformat()
    base = {
        "id": str(uuid.uuid4()), "user_id": USER, "inbox_id": INBOX, "event_type": "bell.drawing_changed",
        "project_id": PROJECT, "source_notification_id": None, "status": "pending", "skip_reason": None,
        "run_id": None, "attempts": 0, "infra_retries": 0, "next_attempt_at": None, "cap_deferred_at": None,
        "decision": None, "reason": None, "action": None, "severity_hint": None, "escalation_event_id": None,
        "decided_at": None, "created_at": stamp, "enqueued_at": stamp, "updated_at": stamp,
    }
    base.update(over)
    return base


def triage_run_row(**over) -> dict:
    """A claimed assistant_triage_runs row (0196 A3)."""
    stamp = P7_T0.isoformat()
    base = {
        "id": str(uuid.uuid4()), "user_id": USER, "status": "claimed", "claimed_by": "w1", "claimed_at": stamp,
        "lease_until": (P7_T0 + timedelta(seconds=420)).isoformat(), "item_count": 1, "model": None,
        "reservation_id": None, "input_tokens": None, "cached_tokens": None, "output_tokens": None,
        "cost_usd": None, "error_kind": None, "finished_at": None, "created_at": stamp,
    }
    base.update(over)
    return base


class FakeDB(_Phase7Mixin, _Phase8Mixin):
    def __init__(self, now: datetime = NOW, **tables):
        self.tables = {k: list(v) for k, v in tables.items()}
        self.now = now
        self._spend_lock = threading.RLock()
        self.calls: list[tuple[str, str]] = []
        self.rpc_calls: list[tuple[str, dict]] = []
        self.or_exprs: list[str] = []
        # Phase 3: rows another transaction holds (skip locked), the claim's
        # statement lock, the transaction-local pin flag of 0176 B4, the
        # trigger clock, and one-shot RPC failures.
        self.locked_schedules: set[str] = set()
        self.locked_runs: set[str] = set()
        self._claim_lock = threading.RLock()
        self._pin_flag = False
        self._clock_n = 0
        self.rpc_errors: dict[str, Exception] = {}
        self._p7_init()

    def now_iso(self) -> str:
        return self.now.isoformat()

    def table(self, name):
        return _Query(self, name)

    def _insert(self, table: str, row: dict) -> dict:
        if row.get("id") is not None and any(str(r.get("id")) == str(row["id"])
                                             for r in self.tables.get(table, [])):
            raise UniqueViolation(f"{table}_pkey")
        row.setdefault("id", str(uuid.uuid4()))
        row.setdefault("created_at", (self.now + timedelta(microseconds=len(self.tables.get(table, [])))).isoformat())
        if table == "assistant_files":
            self._insert_staged_file(row)
        if table == "assistant_tasks":
            for col in ("title", "instruction", "project_id", "conversation_id", "message_id", "split_job_id",
                        "result", "failure_reason", "created_by", "finished_at", "approval_id", "run_id",
                        "spend_reservation_id", "result_message_id"):
                row.setdefault(col, None)
            row.setdefault("status", "pending")
            row.setdefault("payload", {})
            row.setdefault("updated_at", row["created_at"])
            if row.get("approval_id") is not None and any(
                    str(r.get("approval_id")) == str(row["approval_id"]) for r in self.tables.get(table, [])):
                raise UniqueViolation("assistant_tasks_approval_uq")
        if table == "assistant_spend_reservations":
            for col in ("run_id", "task_id", "approval_id", "actual_usd", "settled_at"):
                row.setdefault(col, None)
            row.setdefault("status", "active")
            if row.get("task_id") is not None and row.get("kind") == "split_task" and any(
                    str(r.get("task_id")) == str(row["task_id"]) and r.get("kind") == "split_task"
                    for r in self.tables.get(table, [])):
                raise UniqueViolation("assistant_spend_reservations_task_uq")
            if row.get("kind") in AE_HOLD_KINDS:
                # 0180 A8: one AI Estimating hold per task and per approval.
                for col, index in (("task_id", "assistant_spend_reservations_task_ae_uq"),
                                   ("approval_id", "assistant_spend_reservations_approval_ae_uq")):
                    if row.get(col) is not None and any(
                            str(r.get(col)) == str(row[col]) and r.get("kind") in AE_HOLD_KINDS
                            for r in self.tables.get(table, [])):
                        raise UniqueViolation(index)
        if table == "assistant_messages":
            row.setdefault("status", "done")
            row.setdefault("blocks", [])
            row.setdefault("tool_calls", [])
            row.setdefault("tainted", False)
            row.setdefault("mailbox_tainted", False)
            row.setdefault("server_data", False)
            self._message_taint(row, None)
            if row["status"] in ("pending", "running"):
                for r in self.tables.get(table, []):
                    if r.get("conversation_id") == row.get("conversation_id") and r.get("status") in ("pending", "running"):
                        raise UniqueViolation("assistant_messages_one_active_uq")
        if table == "assistant_conversations":
            row.setdefault("surface", "panel")
            row.setdefault("mode", "normal")
            for col in ("title", "project_id", "tainted_at", "mailbox_tainted_at", "last_message_at", "archived_at"):
                row.setdefault(col, None)
        if table == "assistant_profiles":
            row.setdefault("display_name", None)
            row.setdefault("enabled", True)
            row.setdefault("timezone", "America/Los_Angeles")
            row.setdefault("daily_cost_cap_usd", 5)
        if table == "assistant_approvals":
            row.setdefault("status", "pending")
            row.setdefault("preview", {})
            row.setdefault("precondition", {})
            row.setdefault("updated_at", row["created_at"])
            for col in APPROVAL_NULLABLE:
                row.setdefault(col, None)
            self._check_approval(row, exclude=None)
        if table == "assistant_tool_results":
            row.setdefault("taint_corpus", None)
            # memory_ids (0176 B1) is stored only when the writer passes it;
            # a missing key reads as null.
        if table in self.INSERT_HOOKS:
            getattr(self, self.INSERT_HOOKS[table])(row)
        if table == "assistant_tool_calls":
            rows = self.tables.setdefault(table, [])
            same = [r for r in rows if r.get("idempotency_key") == row.get("idempotency_key")]
            if any(r.get("phase") == row.get("phase") for r in same):
                raise UniqueViolation("assistant_tool_calls_key_phase_uq")
            if row.get("phase") != "begun":
                begun = next((r for r in same if r.get("phase") == "begun"), None)
                if begun is None:
                    raise Exception("no_begun_row")
                if begun.get("lease_owner") != row.get("lease_owner"):
                    raise Exception("lease_lost")
                if any(r.get("phase") in ("completed", "failed") for r in same):
                    raise Exception("already_finished")
        self.tables.setdefault(table, []).append(row)
        return dict(row)

    # ── assistant_files rules (0170 caps trigger, 0174 columns) ──

    STAGING_CAP_BYTES = 2147483648

    def _insert_staged_file(self, row: dict) -> None:
        for col in ("conversation_id", "message_id", "consumed_at", "consumed_project_id", "consumed_task_id",
                    "purged_at", "page_count"):
            row.setdefault(col, None)
        row.setdefault("mime", "application/pdf")
        row.setdefault("expires_at", (self.now + timedelta(days=7)).isoformat())
        rows = self.tables.get("assistant_files", [])
        if any(r.get("storage_path") == row.get("storage_path") for r in rows):
            raise UniqueViolation("assistant_files_storage_path_key")
        if row.get("message_id") is not None:
            if sum(1 for r in rows if str(r.get("message_id")) == str(row["message_id"])) + 1 > 10:
                raise CheckViolation("file_cap_message: at most 10 files per message (check_violation)")
        live = sum(int(r.get("size_bytes") or 0) for r in rows
                   if str(r.get("user_id")) == str(row.get("user_id")) and r.get("consumed_at") is None
                   and _ts(r["expires_at"]) > self.now)
        if live + int(row.get("size_bytes") or 0) > self.STAGING_CAP_BYTES:
            raise CheckViolation("file_cap_user: at most 2 GB of staged files (check_violation)")

    # ── 0174 spend rpcs ──

    def _pacific_day_start(self) -> datetime:
        local = self.now.astimezone(ZoneInfo("America/Los_Angeles"))
        return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    def _spend_state(self, p: dict) -> dict:
        """assistant_spend_state (0174 section F) in Python."""
        user = str(p["p_user_id"])
        prof = next((r for r in self.tables.get("assistant_profiles", []) if str(r.get("user_id")) == user), None)
        sett = next((r for r in self.tables.get("assistant_settings", []) if str(r.get("id")) == "1"), None)
        user_cap = _D((prof or {}).get("daily_cost_cap_usd"), p["p_user_cap_default"])
        global_cap = _D((sett or {}).get("global_daily_cap_usd"), p["p_global_cap_default"])
        since = self._pacific_day_start()
        log = self.tables.get("llm_call_log", [])
        today = [r for r in log if r.get("created_at") and _ts(r["created_at"]) >= since]
        user_spent = sum((_D(r.get("cost_usd")) for r in today if str(r.get("user_id")) == user), Decimal("0"))
        global_spent = sum((_D(r.get("cost_usd")) for r in today
                            if r.get("feature") in ("assistant", "assistant_extract") or r.get("assistant_run_id")),
                           Decimal("0"))
        # 0180 A8: today's closed actuals of the AI Estimating holds, settled
        # OR released (a canceled or failed run still spent money).
        for res in self.tables.get("assistant_spend_reservations", []):
            if (res.get("kind") in AE_HOLD_KINDS and res.get("status") in ("settled", "released")
                    and res.get("actual_usd") is not None and res.get("settled_at")
                    and _ts(res["settled_at"]) >= since):
                user_spent += _D(res["actual_usd"]) if str(res.get("user_id")) == user else Decimal("0")
                global_spent += _D(res["actual_usd"])
        user_held = global_held = Decimal("0")
        for res in self.tables.get("assistant_spend_reservations", []):
            if res.get("status") != "active" or _ts(res["expires_at"]) <= self.now:
                continue
            logged = sum((_D(r.get("cost_usd")) for r in log
                          if str(r.get("spend_reservation_id")) == str(res["id"])), Decimal("0"))
            held = max(_D(res.get("amount_usd")) - logged, Decimal("0"))
            global_held += held
            if str(res.get("user_id")) == user:
                user_held += held
        return {"user_cap": float(user_cap), "user_spent": float(user_spent), "user_held": float(user_held),
                "global_cap": float(global_cap), "global_spent": float(global_spent),
                "global_held": float(global_held)}

    def _spend_reserve(self, p: dict) -> dict:
        """assistant_spend_reserve (0174 section F) in Python; the advisory
        lock is a process lock here."""
        amount = None if p.get("p_amount") is None else _D(p["p_amount"])
        ttl = p.get("p_ttl_seconds")
        if (p.get("p_kind") not in RESERVATION_KINDS or amount is None or amount < 0
                or ttl is None or int(ttl) < 30 or int(ttl) > 43200):
            raise CheckViolation("assistant_spend_reserve: bad arguments (check_violation)")
        # 0196 B2: a triage hold is the company's, and the company id holds nothing else.
        if (p.get("p_kind") == "triage") != (str(p.get("p_user_id")) == COMPANY_SPEND_ID):
            raise CheckViolation("assistant_spend_reserve: bad arguments (check_violation)")
        with self._spend_lock:
            state = self._spend_state(p)
            if (p.get("p_kind") != "triage"
                    and _D(state["user_spent"]) + _D(state["user_held"]) + amount >= _D(state["user_cap"])):
                return {"ok": False, "reason": "user_cap", "id": None, "state": state}
            if _D(state["global_spent"]) + _D(state["global_held"]) + amount >= _D(state["global_cap"]):
                return {"ok": False, "reason": "global_cap", "id": None, "state": state}
            row = self._insert("assistant_spend_reservations", {
                "user_id": str(p["p_user_id"]), "kind": p["p_kind"], "run_id": p.get("p_run_id"),
                "task_id": p.get("p_task_id"), "approval_id": p.get("p_approval_id"),
                "amount_usd": float(amount), "expires_at": (self.now + timedelta(seconds=int(ttl))).isoformat(),
            })
            return {"ok": True, "reason": None, "id": row["id"], "state": state}

    # ── phase 3 (0176) ──

    INSERT_HOOKS = {
        "assistant_schedules": "_insert_schedule",
        "assistant_schedule_runs": "_insert_run",
        "assistant_memories": "_insert_memory",
        "assistant_memory_events": "_insert_memory_event",
        "assistant_memory_revisions": "_insert_revision",
        "assistant_sweeps": "_insert_sweep",
        "assistant_evidence_access": "_insert_evidence_access",
        "assistant_reports": "_insert_report",
        "plans_reader_notes": "_insert_plans_note",
        **P7_INSERT_HOOKS,
    }
    UPDATE_HOOKS = {
        "assistant_messages": "_update_message",
        "assistant_schedules": "_update_schedule",
        "assistant_schedule_runs": "_update_run",
        "assistant_memories": "_update_memory",
        "assistant_memory_revisions": "_update_revision",
        "assistant_reports": "_update_report",
        "assistant_settings": "_p8_update_settings",
        **P7_UPDATE_HOOKS,
    }
    PHASE4_RPCS = {
        "assistant_approval_decide_with_reason": "_decide_with_reason",
        "assistant_memory_search": "_memory_search",
        "search_plans_notes": "_search_plans_notes",
    }
    PHASE3_RPCS = {
        "claim_due_assistant_schedules": "_claim_due",
        "assistant_schedule_run_channel": "_run_channel",
        "assistant_memory_apply": "_memory_apply",
        "assistant_redact_memory_items": "_redact_rpc",
        "assistant_memory_similar": "_memory_similar",
        "assistant_memory_touch": "_memory_touch",
    }

    # assistant_messages (0170 assistant_message_taint)

    def _message_taint(self, new: dict, old: dict | None) -> None:
        """The 0170 trigger: taint is sticky on a message, mailbox taint
        implies taint, and a tainted message marks its conversation."""
        if old is not None:
            new["tainted"] = bool(old.get("tainted")) or bool(new.get("tainted"))
            new["mailbox_tainted"] = bool(old.get("mailbox_tainted")) or bool(new.get("mailbox_tainted"))
        if new.get("mailbox_tainted"):
            new["tainted"] = True
        if not new.get("tainted"):
            return
        for conv in self.tables.get("assistant_conversations", []):
            if str(conv.get("id")) != str(new.get("conversation_id")):
                continue
            if conv.get("tainted_at") is None:
                conv["tainted_at"] = self.now_iso()
            if new.get("mailbox_tainted") and conv.get("mailbox_tainted_at") is None:
                conv["mailbox_tainted_at"] = self.now_iso()

    def _update_message(self, row: dict, patch: dict) -> dict:
        new = {**row, **patch}
        # "before update of tainted, mailbox_tainted": only when the SET names them.
        if {"tainted", "mailbox_tainted"} & set(patch):
            self._message_taint(new, row)
        row.update(new)
        return row

    def tick(self) -> str:
        """now() of the next statement: the database clock plus a strictly
        growing microsecond count, so two updated_at stamps never tie."""
        self._clock_n += 1
        return (self.now + timedelta(microseconds=self._clock_n)).isoformat()

    def _statement(self, body):
        """Run one statement all or nothing (a trigger refusal undoes the
        rows the statement already changed)."""
        snapshot = {name: [(row, copy.deepcopy(row)) for row in rows] for name, rows in self.tables.items()}
        try:
            return body()
        except Exception:
            # Restore in place, so a caller's references to the lists and
            # the rows stay live.
            for name in [n for n in self.tables if n not in snapshot]:
                del self.tables[name]
            for name, pairs in snapshot.items():
                for row, saved in pairs:
                    row.clear()
                    row.update(saved)
                self.tables[name][:] = [row for row, _ in pairs]
            raise

    def _find(self, table: str, **eq) -> dict | None:
        return next((r for r in self.tables.get(table, [])
                     if all(str(r.get(k)) == str(v) for k, v in eq.items())), None)

    # assistant_schedules

    SCHEDULE_DEFAULTS = {
        "params": {}, "timezone": "America/Los_Angeles", "status": "active", "paused_reason": None,
        "sub_app": None, "delivery": "chat_bell", "only_if_changed": False, "last_result_hash": None,
        "project_id": None, "conversation_id": None, "next_run_at": None, "last_run_at": None,
        "consecutive_failures": 0, "unread_deliveries": 0, "ended_at": None, "dtstart": None,
        "after_next_run_at": None, "approval_id": None, "paused_at": None, "ended_reason": None,
        "last_delivered_at": None, "last_seen_at": None,
    }
    SCHEDULE_STATUSES = ("active", "paused", "paused_feature_off", "ended")
    RECIPES = ("due_today", "due_this_week", "missing_quotes", "labor_not_entered",
               "estimator_vs_bid", "quote_readiness")  # 0180 widens the recipe check
    DELIVERIES = ("chat", "chat_bell", "chat_bell_email")

    def _schedule_caps(self, new: dict, old: dict | None) -> None:
        """assistant_schedules_caps (0176 A2) in Python."""
        rule = str(new.get("rrule") or "").upper()
        if old is None or new.get("rrule") != old.get("rrule"):
            freq = re.search(r"FREQ=([A-Z]+)", rule)
            if freq is None or freq.group(1) not in ("HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
                raise CheckViolation("schedule_interval: FREQ must be HOURLY or slower (minimum interval 1 hour)")
            if re.search(r"BYSECOND=", rule) or re.search(r"(^|[^A-Z])RDATE", rule):
                raise CheckViolation("schedule_interval: BYSECOND and RDATE are not allowed (minimum interval 1 hour)")
            minutes = re.search(r"BYMINUTE=([0-9,]+)", rule)
            if minutes is not None and "," in minutes.group(1):
                raise CheckViolation("schedule_interval: one BYMINUTE value at most (minimum interval 1 hour)")
            if re.search(r"(COUNT|UNTIL|DTSTART|EXDATE|EXRULE|BYSETPOS|BYWEEKNO|BYYEARDAY|BYEASTER)", rule):
                raise CheckViolation("schedule_rule: this rule part is not supported")
        newly_active = new.get("status") == "active" and (
            old is None or old.get("status") != "active" or str(old.get("user_id")) != str(new.get("user_id")))
        if newly_active:
            others = [s for s in self.tables.get("assistant_schedules", [])
                      if str(s.get("user_id")) == str(new.get("user_id")) and s.get("status") == "active"
                      and str(s.get("id")) != str(new.get("id"))]
            if len(others) + 1 > 10:
                raise CheckViolation("schedule_cap_active: at most 10 active schedules")

    def _schedule_checks(self, row: dict) -> None:
        if not 1 <= len(str(row.get("title") or "")) <= 200:
            raise CheckViolation("assistant_schedules_title_check (check_violation)")
        if row.get("recipe") not in self.RECIPES:
            raise CheckViolation("assistant_schedules_recipe_check (check_violation)")
        if not 10 <= len(str(row.get("rrule") or "")) <= 500:
            raise CheckViolation("assistant_schedules_rrule_check (check_violation)")
        if row.get("status") not in self.SCHEDULE_STATUSES:
            raise CheckViolation("assistant_schedules_status_check (check_violation)")
        if row.get("delivery") not in self.DELIVERIES:
            raise CheckViolation("assistant_schedules_delivery_check (check_violation)")
        if row.get("timezone") != "America/Los_Angeles":
            raise CheckViolation("assistant_schedules_timezone_chk (check_violation)")
        if not isinstance(row.get("params"), dict):
            raise CheckViolation("assistant_schedules_params_chk (check_violation)")
        if row.get("status") == "active" and (row.get("next_run_at") is None or row.get("dtstart") is None):
            raise CheckViolation("assistant_schedules_active_next_chk (check_violation)")
        if (row.get("status") == "ended") != (row.get("ended_at") is not None):
            raise CheckViolation("assistant_schedules_ended_chk (check_violation)")
        if row.get("approval_id") is not None and any(
                str(s.get("approval_id")) == str(row["approval_id"]) and str(s.get("id")) != str(row.get("id"))
                for s in self.tables.get("assistant_schedules", [])):
            raise UniqueViolation("assistant_schedules_approval_uq")

    def _insert_schedule(self, row: dict) -> None:
        for col, value in self.SCHEDULE_DEFAULTS.items():
            row.setdefault(col, copy.deepcopy(value))
        row.setdefault("updated_at", self.tick())
        self._schedule_caps(row, None)
        self._schedule_checks(row)

    def _update_schedule(self, row: dict, patch: dict) -> dict:
        new = {**row, **patch, "updated_at": self.tick()}
        if {"rrule", "status", "user_id"} & set(patch):
            self._schedule_caps(new, row)
        self._schedule_checks(new)
        row.update(new)
        return row

    # assistant_schedule_runs

    RUN_STATUSES = ("claimed", "running", "delivered", "no_change", "skipped", "failed")
    RUN_IDENTITY = ("schedule_id", "user_id", "scheduled_for", "kind", "user_label")

    @staticmethod
    def _run_rank(status) -> int:
        return {"claimed": 0, "running": 1}.get(status, 2)

    def _insert_run(self, row: dict) -> None:
        for col in ("claimed_by", "started_at", "finished_at", "result_hash", "delivered", "message_id",
                    "error_kind", "error", "stats"):
            row.setdefault(col, None)
        row.setdefault("status", "claimed")
        row.setdefault("kind", "scheduled")
        if not row.get("user_label"):
            raise CheckViolation("assistant_schedule_runs.user_label is not null (not_null_violation)")
        if row["status"] not in self.RUN_STATUSES or row["kind"] not in ("scheduled", "run_now"):
            raise CheckViolation("assistant_schedule_runs status or kind check (check_violation)")
        when = _ts(row["scheduled_for"])
        if any(str(r.get("schedule_id")) == str(row.get("schedule_id")) and _ts(r["scheduled_for"]) == when
               for r in self.tables.get("assistant_schedule_runs", [])):
            raise UniqueViolation("assistant_schedule_runs_occurrence_uq")

        def pending_run_now(r) -> bool:
            return r.get("kind") == "run_now" and r.get("status") == "claimed" and r.get("claimed_by") is None

        if pending_run_now(row) and any(str(r.get("schedule_id")) == str(row.get("schedule_id"))
                                        and pending_run_now(r)
                                        for r in self.tables.get("assistant_schedule_runs", [])):
            raise UniqueViolation("assistant_schedule_runs_one_pending_run_now_uq")

    def _update_run(self, row: dict, patch: dict) -> dict:
        """assistant_schedule_run_forward (0176 A3) in Python."""
        new = {**row, **patch}
        for col in self.RUN_IDENTITY:
            if str(new.get(col)) != str(row.get(col)):
                raise TriggerRefused("assistant_schedule_runs: identity columns are immutable")
        old_rank, new_rank = self._run_rank(row.get("status")), self._run_rank(new.get("status"))
        if new_rank < old_rank or (old_rank == 2 and new.get("status") != row.get("status")):
            raise TriggerRefused(f"assistant_schedule_runs: status moves forward only "
                                 f"({row.get('status')} to {new.get('status')})")
        if new.get("status") not in self.RUN_STATUSES:
            raise CheckViolation("assistant_schedule_runs_status_check (check_violation)")
        row.update(new)
        return row

    def _profile_label(self, user_id) -> str:
        prof = self._find("profiles", id=user_id) or {}
        name = str(prof.get("full_name") or "").strip()
        return name or prof.get("email") or str(user_id)

    def _claim_due(self, p: dict) -> list[dict]:
        """claim_due_assistant_schedules (0176 A4) in Python. Every test the
        statement makes reads the rows as they were when it began (the
        runs it inserts are invisible to its own overlap checks), rows in
        `locked_schedules` and `locked_runs` are skipped (another
        transaction holds them), and the claim lock serializes two callers
        the way the row locks do."""
        with self._claim_lock:
            return self._statement(lambda: self._claim_due_body(p))

    def _claim_due_body(self, p: dict) -> list[dict]:
        now = self.now
        worker = p.get("p_worker")
        limit = max(1, min(int(p.get("p_limit") or 20), 50))
        catchup = max(int(p["p_catchup_seconds"] if p.get("p_catchup_seconds") is not None else 10800), 0)
        runs = self.tables.setdefault("assistant_schedule_runs", [])
        before = [dict(r) for r in runs]
        recent = now - timedelta(minutes=30)

        def open_in(rows, schedule_id, *, taken_only=False, exclude=None) -> bool:
            return any(str(r.get("schedule_id")) == str(schedule_id) and r.get("status") in ("claimed", "running")
                       and _ts(r["created_at"]) > recent and (not taken_only or r.get("claimed_by") is not None)
                       and str(r.get("id")) != str(exclude) for r in rows)

        due = sorted((s for s in self.tables.get("assistant_schedules", [])
                      if s.get("status") == "active" and s.get("next_run_at") is not None
                      and _ts(s["next_run_at"]) <= now and s.get("after_next_run_at") is not None
                      and str(s.get("id")) not in self.locked_schedules),
                     key=lambda s: _ts(s["next_run_at"]))[:limit]
        out: list[dict] = []
        for sched in due:
            scheduled_for = sched["next_run_at"]
            next_slot = sched["after_next_run_at"]
            self._update_schedule(sched, {"next_run_at": sched["after_next_run_at"], "after_next_run_at": None,
                                          "last_run_at": scheduled_for})
            if _ts(scheduled_for) < now - timedelta(seconds=catchup):
                skip = "missed"
            elif _ts(next_slot) <= now:
                skip = "missed"   # superseded: a newer occurrence is already due
            elif open_in(before, sched["id"]):
                skip = "overlap"
            else:
                skip = None
            if any(str(r.get("schedule_id")) == str(sched["id"]) and _ts(r["scheduled_for"]) == _ts(scheduled_for)
                   for r in runs):
                continue  # on conflict (schedule_id, scheduled_for) do nothing
            out.append(self._insert("assistant_schedule_runs", {
                "schedule_id": sched["id"], "user_id": sched["user_id"],
                "user_label": self._profile_label(sched["user_id"]), "scheduled_for": scheduled_for,
                "kind": "scheduled", "status": "claimed" if skip is None else "skipped", "claimed_by": worker,
                "started_at": now.isoformat(), "finished_at": None if skip is None else now.isoformat(),
                "error_kind": skip,
            }))
        def older_pending(r) -> bool:
            """An older unclaimed Run now row of the same schedule (A4 guard)."""
            return any(str(o.get("schedule_id")) == str(r.get("schedule_id")) and str(o.get("id")) != str(r.get("id"))
                       and o.get("kind") == "run_now" and o.get("status") in ("claimed", "running")
                       and o.get("claimed_by") is None and _ts(o["created_at"]) > recent
                       and (_ts(o["created_at"]), str(o.get("id"))) < (_ts(r["created_at"]), str(r.get("id")))
                       for o in before)

        manual = sorted((r for r in before
                         if r.get("kind") == "run_now" and r.get("status") == "claimed" and r.get("claimed_by") is None
                         and _ts(r["created_at"]) > now - timedelta(minutes=10)
                         and str(r.get("id")) not in self.locked_runs
                         and not open_in(before, r["schedule_id"], taken_only=True, exclude=r["id"])
                         and not older_pending(r)),
                        key=lambda r: _ts(r["created_at"]))[:limit]
        for snap in manual:
            row = self._find("assistant_schedule_runs", id=snap["id"])
            out.append(dict(self._update_run(row, {"claimed_by": worker, "started_at": now.isoformat()})))
        return out

    def _run_channel(self, p: dict) -> bool:
        """assistant_schedule_run_channel (0176 A5) in Python."""
        channel, action = p.get("p_channel"), p.get("p_action")
        if channel not in ("chat", "bell", "email") or action not in ("claim", "done", "failed"):
            raise CheckViolation("assistant_schedule_run_channel: bad arguments (check_violation)")
        row = next((r for r in self.tables.get("assistant_schedule_runs", [])
                    if str(r.get("id")) == str(p.get("p_run_id")) and str(r.get("user_id")) == str(p.get("p_user_id"))
                    and r.get("status") == "running"), None)
        if row is None:
            return False
        delivered = dict(row.get("delivered") or {})
        if action == "claim":
            if channel in delivered:
                return False
            delivered[channel] = "claimed"
        else:
            if delivered.get(channel) != "claimed":
                return False
            value = p.get("p_value")
            delivered[channel] = (True if value is None else value) if action == "done" else False
        self._update_run(row, {"delivered": delivered})
        return True

    # assistant_memories, events, revisions

    MEMORY_KINDS = ("preference", "about_me", "instruction", "project_note", "reference")
    MEMORY_SOURCES = ("user_stated", "user_confirmed", "user_settings", "inferred")
    MEMORY_EVENTS = ("created", "updated", "approved", "rejected", "pinned", "unpinned", "archived", "restored",
                     "revision_proposed", "revision_approved", "revision_rejected", "confirmed", "moved")
    MEMORY_CONTENT = ("kind", "title", "body", "project_id", "status", "pinned", "source", "origin_tainted")
    PENDING_CAP = 50

    def _memory_caps(self, new: dict, old: dict | None) -> None:
        """assistant_memories_caps (0170 8g) in Python."""
        if new.get("status") != "active":
            return
        was_active = was_pinned = False
        old_chars = 0
        if old is not None and str(old.get("user_id")) == str(new.get("user_id")):
            was_active = old.get("status") == "active"
            was_pinned = was_active and bool(old.get("pinned"))
            old_chars = len(old.get("title") or "") + len(old.get("body") or "")
        others = [m for m in self.tables.get("assistant_memories", [])
                  if str(m.get("user_id")) == str(new.get("user_id")) and m.get("status") == "active"
                  and str(m.get("id")) != str(new.get("id"))]
        if not was_active and len(others) + 1 > 300:
            raise CheckViolation("memory_cap_active: at most 300 active memories")
        chars = len(new.get("title") or "") + len(new.get("body") or "")
        if new.get("pinned") and not was_pinned and sum(1 for m in others if m.get("pinned")) + 1 > 40:
            raise CheckViolation("memory_cap_pinned: at most 40 pinned memories")
        if new.get("pinned") and (not was_pinned or chars > old_chars):
            pinned_chars = sum(len(m.get("title") or "") + len(m.get("body") or "") for m in others if m.get("pinned"))
            if pinned_chars + chars > 6000:
                raise CheckViolation("memory_cap_pinned_chars: pinned memories are limited to 6000 characters")

    def _memory_checks(self, row: dict) -> None:
        if row.get("kind") not in self.MEMORY_KINDS:
            raise CheckViolation("assistant_memories_kind_check (check_violation)")
        if not 1 <= len(str(row.get("title") or "")) <= 200 or len(str(row.get("body") or "")) > 4000:
            raise CheckViolation("assistant_memories title or body length (check_violation)")
        if row.get("status") not in ("active", "pending_review", "archived"):
            raise CheckViolation("assistant_memories_status_check (check_violation)")
        if row.get("source") not in self.MEMORY_SOURCES:
            raise CheckViolation("assistant_memories_source_check (check_violation)")
        if row.get("kind") == "project_note" and row.get("project_id") is None:
            raise CheckViolation("assistant_memories_project_note_chk (check_violation)")
        key = row.get("idempotency_key")
        if key is not None and any(str(m.get("user_id")) == str(row.get("user_id")) and m.get("idempotency_key") == key
                                   and str(m.get("id")) != str(row.get("id"))
                                   for m in self.tables.get("assistant_memories", [])):
            raise UniqueViolation("assistant_memories_idem_uq")

    def _insert_memory(self, row: dict) -> None:
        for col in ("project_id", "conversation_id", "message_id", "last_used_at", "archived_at",
                    "idempotency_key"):
            row.setdefault(col, None)
        row.setdefault("body", "")
        row.setdefault("status", "pending_review")
        row.setdefault("pinned", False)
        row.setdefault("origin_tainted", False)
        row.setdefault("use_count", 0)
        row.setdefault("updated_at", self.tick())
        self._memory_caps(row, None)
        if row.get("pinned"):
            raise CheckViolation("memory_pin_user_only: a memory is pinned only by the user")
        self._memory_checks(row)

    def _update_memory(self, row: dict, patch: dict) -> dict:
        """The 0170 caps, the 0176 pin guard and the content-only updated_at
        trigger (each fires only when its columns are in the SET list, as
        `before update of` does), then the checks."""
        new = {**row, **patch}
        if {"status", "pinned", "title", "body", "user_id"} & set(patch):
            self._memory_caps(new, row)
        if "pinned" in patch and new.get("pinned") and not row.get("pinned") and not self._pin_flag:
            raise CheckViolation("memory_pin_user_only: a memory is pinned only by the user")
        if set(self.MEMORY_CONTENT) & set(patch):
            new["updated_at"] = self.tick()
        self._memory_checks(new)
        row.update(new)
        return row

    def _insert_memory_event(self, row: dict) -> None:
        row.setdefault("payload", {})
        row.setdefault("conversation_id", None)
        row.setdefault("message_id", None)
        if row.get("event") not in self.MEMORY_EVENTS:
            raise CheckViolation("assistant_memory_events_event_chk (check_violation)")
        if row.get("actor") not in ("user", "assistant", "system"):
            raise CheckViolation("assistant_memory_events_actor_check (check_violation)")

    def _revision_checks(self, row: dict) -> None:
        if (row.get("decision") is None) != (row.get("decided_at") is None):
            raise CheckViolation("assistant_memory_revisions_decided_chk (check_violation)")
        for other in self.tables.get("assistant_memory_revisions", []):
            if str(other.get("id")) == str(row.get("id")):
                continue
            if (row.get("decided_at") is None and other.get("decided_at") is None
                    and str(other.get("memory_id")) == str(row.get("memory_id"))):
                raise UniqueViolation("assistant_memory_revisions_one_pending_uq")
            key = row.get("idempotency_key")
            if key is not None and other.get("idempotency_key") == key and str(other.get("user_id")) == str(row.get("user_id")):
                raise UniqueViolation("assistant_memory_revisions_idem_uq")

    def _insert_revision(self, row: dict) -> None:
        for col in ("conversation_id", "message_id", "decision", "decided_at", "base_updated_at", "idempotency_key"):
            row.setdefault(col, None)
        row.setdefault("body", "")
        row.setdefault("origin_tainted", False)
        self._revision_checks(row)

    def _update_revision(self, row: dict, patch: dict) -> dict:
        new = {**row, **patch}
        self._revision_checks(new)
        row.update(new)
        return row

    def _cascade_memories(self, ids: set[str]) -> None:
        """on delete cascade from assistant_memories (the events'
        allow_cascade path, 0170 8b)."""
        for child in ("assistant_memory_events", "assistant_memory_revisions"):
            self.tables[child] = [r for r in self.tables.get(child, []) if str(r.get("memory_id")) not in ids]

    def _insert_sweep(self, row: dict) -> None:
        row.pop("id", None)
        row.pop("created_at", None)
        row.setdefault("started_at", self.now_iso())
        row.setdefault("finished_at", None)
        row.setdefault("result", None)
        if row.get("step") not in ("nightly", "orphans", "report_purge"):  # 0180 B5
            raise CheckViolation("assistant_sweeps_step_check (check_violation)")
        if any(r.get("step") == row["step"] and str(r.get("sweep_date")) == str(row.get("sweep_date"))
               for r in self.tables.get("assistant_sweeps", [])):
            raise UniqueViolation("assistant_sweeps_pkey")

    @staticmethod
    def _tokens(text) -> set[str]:
        return set(re.findall(r"\w+", str(text or "").lower()))

    def _memory_similar(self, p: dict) -> list[dict]:
        """assistant_memory_similar (0176 B5) with a token Jaccard score
        standing in for pg_trgm's similarity()."""
        want = self._tokens(p.get("p_title"))
        threshold = max(float(p["p_threshold"]) if p.get("p_threshold") is not None else 0.5, 0.3)
        limit = max(1, min(int(p.get("p_limit") or 5), 10))
        scored = []
        for m in self.tables.get("assistant_memories", []):
            if str(m.get("user_id")) != str(p.get("p_user_id")) or m.get("status") == "archived":
                continue
            have = self._tokens(m.get("title"))
            score = len(want & have) / len(want | have) if want | have else 0.0
            if score > threshold:
                scored.append((score, str(m.get("updated_at") or ""), m))
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        return [{"id": m["id"], "title": m["title"], "status": m["status"], "kind": m["kind"],
                 "similarity": round(score, 4)} for score, _, m in scored[:limit]]

    def _memory_touch(self, p: dict) -> int:
        """assistant_memory_touch (0176 B7): last_used_at and use_count, and
        no trigger moves updated_at."""
        ids = {str(i) for i in p.get("p_ids") or []}
        n = 0
        for m in self.tables.get("assistant_memories", []):
            if str(m.get("user_id")) == str(p.get("p_user_id")) and str(m.get("id")) in ids and m.get("status") == "active":
                m["last_used_at"] = self.tick()
                m["use_count"] = int(m.get("use_count") or 0) + 1
                n += 1
        return n

    def _redact_rpc(self, p: dict) -> list:
        return redact_memory_items(p.get("p_items") or [], str(p.get("p_id") or ""))

    ASSISTANT_OPS = ("create", "revise_pending", "propose_revision", "forget")
    USER_OPS = ("create", "edit", "approve", "confirm", "reject", "archive", "restore", "pin", "unpin",
                "approve_revision", "reject_revision", "delete")
    SYSTEM_OPS = ("archive", "move", "delete")

    def _memory_apply(self, p: dict) -> dict:
        """assistant_memory_apply (0176 B6) in Python, one transaction."""
        try:
            return self._statement(lambda: self._memory_apply_body(p))
        finally:
            self._pin_flag = False

    def _memory_apply_body(self, p: dict) -> dict:
        op, actor, user = p.get("p_op"), p.get("p_actor"), str(p.get("p_user_id"))
        allowed = {"assistant": self.ASSISTANT_OPS, "user": self.USER_OPS, "system": self.SYSTEM_OPS}
        if op not in allowed.get(actor, ()):
            raise CheckViolation(f"assistant_memory_apply: op {op} is not allowed for actor {actor} (check_violation)")
        f = dict(p.get("p_fields") or {})
        key = p.get("p_idempotency_key")
        conv, msg = p.get("p_conversation_id"), p.get("p_message_id")
        memories = self.tables.setdefault("assistant_memories", [])
        revisions = self.tables.setdefault("assistant_memory_revisions", [])

        def snap(row):
            return {k: copy.deepcopy(v) for k, v in row.items() if k != "tsv"}

        rev = None
        payload: dict = {}
        if op == "create":
            if key is not None:
                existing = next((m for m in memories if str(m.get("user_id")) == user
                                 and m.get("idempotency_key") == key), None)
                if existing is not None:
                    return {"ok": True, "replay": True, "memory": snap(existing)}
            status, source = f.get("status") or "", f.get("source") or ""
            if ((actor == "assistant" and not (status in ("active", "pending_review")
                                               and source in ("user_stated", "inferred")))
                    or (actor == "user" and not (status == "active" and source == "user_settings"))):
                raise CheckViolation(f"assistant_memory_apply: bad status or source for {actor} (check_violation)")
            if status == "pending_review":
                pending = sum(1 for m in memories if str(m.get("user_id")) == user and m.get("status") == "pending_review")
                if pending + 1 > self.PENDING_CAP:
                    raise CheckViolation("memory_cap_pending: at most 50 notes waiting for review")
            row = self._insert("assistant_memories", {
                "user_id": user, "kind": f.get("kind"), "title": f.get("title"), "body": f.get("body") or "",
                "project_id": f.get("project_id") or None, "status": status, "source": source,
                "origin_tainted": bool(f.get("origin_tainted") or False), "conversation_id": conv,
                "message_id": msg, "idempotency_key": key,
            })
            row = self._find("assistant_memories", id=row["id"])
            event = "created"
            payload = {"status": row["status"], "source": row["source"], "origin_tainted": row["origin_tainted"]}
        else:
            row = next((m for m in memories if str(m.get("id")) == str(p.get("p_memory_id"))
                        and str(m.get("user_id")) == user), None)
            if row is None:
                return {"ok": False, "code": "memory_not_found"}
            expected = p.get("p_expected_updated_at")
            if expected is not None and _ts(row["updated_at"]) != _ts(expected):
                return {"ok": False, "code": "memory_stale", "memory": snap(row)}
            before = row.get("updated_at")
            status = row.get("status")
            if op == "edit":
                if status == "archived":
                    return {"ok": False, "code": "memory_archived"}
                patch = {k: f[k] for k in ("kind", "title", "body") if f.get(k) is not None}
                if "project_id" in f:
                    patch["project_id"] = f.get("project_id") or None
                self._update_memory(row, {**patch, "source": "user_settings"})
                event, payload = "updated", {"fields": sorted(f)}
            elif op == "revise_pending":
                if status != "pending_review":
                    return {"ok": False, "code": "memory_wrong_status"}
                patch = {k: f[k] for k in ("title", "body") if f.get(k) is not None}
                patch["origin_tainted"] = bool(row.get("origin_tainted")) or bool(f.get("origin_tainted") or False)
                self._update_memory(row, patch)
                event = "updated"
            elif op in ("approve", "confirm"):
                if status != "pending_review":
                    return {"ok": False, "code": "memory_wrong_status"}
                self._update_memory(row, {"status": "active", "source": "user_confirmed"})
                event = "approved" if op == "approve" else "confirmed"
            elif op == "reject":
                if status != "pending_review":
                    return {"ok": False, "code": "memory_wrong_status"}
                self._update_memory(row, {"status": "archived", "archived_at": self.now_iso()})
                event = "rejected"
            elif op in ("archive", "forget"):
                if status == "archived":
                    return {"ok": True, "replay": True, "memory": snap(row)}
                if op == "forget" and row.get("pinned"):
                    return {"ok": False, "code": "memory_pinned_user_only"}
                self._update_memory(row, {"status": "archived", "archived_at": self.now_iso(), "pinned": False})
                event, payload = "archived", {"by": op}
            elif op == "restore":
                if status != "archived":
                    return {"ok": False, "code": "memory_wrong_status"}
                self._update_memory(row, {"status": "active", "archived_at": None})
                event = "restored"
            elif op in ("pin", "unpin"):
                if status != "active":
                    return {"ok": False, "code": "memory_wrong_status"}
                self._pin_flag = op == "pin"
                self._update_memory(row, {"pinned": op == "pin"})
                event = "pinned" if op == "pin" else "unpinned"
            elif op == "propose_revision":
                if status != "active":
                    return {"ok": False, "code": "memory_wrong_status"}
                if row.get("pinned"):
                    return {"ok": False, "code": "memory_pinned_user_only"}
                if key is not None:
                    existing = next((r for r in revisions if str(r.get("user_id")) == user
                                     and r.get("idempotency_key") == key), None)
                    if existing is not None:
                        return {"ok": True, "replay": True, "memory": snap(row), "revision": dict(existing)}
                for old in [r for r in revisions if str(r.get("memory_id")) == str(row["id"])
                            and r.get("decided_at") is None]:
                    self._update_revision(old, {"decision": "rejected", "decided_at": self.now_iso()})
                    self._insert("assistant_memory_events", {
                        "memory_id": row["id"], "user_id": user, "event": "revision_rejected", "actor": "system",
                        "payload": {"revision_id": old["id"], "superseded": True}})
                rev = self._insert("assistant_memory_revisions", {
                    "memory_id": row["id"], "user_id": user, "title": f.get("title") or row["title"],
                    "body": f["body"] if f.get("body") is not None else row["body"],
                    "origin_tainted": bool(f.get("origin_tainted") or False), "conversation_id": conv,
                    "message_id": msg, "base_updated_at": row["updated_at"], "idempotency_key": key,
                })
                event, payload = "revision_proposed", {"revision_id": rev["id"]}
            elif op in ("approve_revision", "reject_revision"):
                target = next((r for r in revisions if str(r.get("id")) == str(f.get("revision_id") or "")
                               and str(r.get("memory_id")) == str(row["id"]) and r.get("decided_at") is None), None)
                if target is None:
                    return {"ok": False, "code": "revision_not_found"}
                if op == "approve_revision":
                    if status != "active":
                        return {"ok": False, "code": "memory_wrong_status"}
                    if target.get("base_updated_at") is None or _ts(target["base_updated_at"]) != _ts(row["updated_at"]):
                        return {"ok": False, "code": "memory_stale", "memory": snap(row)}
                    self._update_memory(row, {"title": target["title"], "body": target["body"],
                                              "origin_tainted": bool(row.get("origin_tainted"))
                                              or bool(target.get("origin_tainted"))})
                    self._update_revision(target, {"decision": "approved", "decided_at": self.now_iso()})
                    event = "revision_approved"
                else:
                    self._update_revision(target, {"decision": "rejected", "decided_at": self.now_iso()})
                    event = "revision_rejected"
                rev = dict(target)
                payload = {"revision_id": target["id"]}
            elif op == "move":
                payload = {"from": row.get("project_id"), "to": f.get("project_id")}
                self._update_memory(row, {"project_id": f.get("project_id") or None})
                event = "moved"
            else:  # delete
                return self._memory_delete(user, row)
            if op in ("archive", "forget", "restore", "pin", "unpin", "move"):
                # 0176 B6: a content-free op re-bases an open revision that was current.
                for r in revisions:
                    if (str(r.get("memory_id")) == str(row["id"]) and r.get("decided_at") is None
                            and r.get("base_updated_at") is not None and _ts(r["base_updated_at"]) == _ts(before)):
                        self._update_revision(r, {"base_updated_at": row["updated_at"]})
        self._insert("assistant_memory_events", {
            "memory_id": row["id"], "user_id": user, "event": event, "actor": actor, "payload": payload,
            "conversation_id": conv, "message_id": msg})
        return {"ok": True, "replay": False, "memory": snap(row), "revision": dict(rev) if rev else None}

    def _memory_delete(self, user: str, row: dict) -> dict:
        """Op 'delete' (0176 B6): legal hold first, then the copies, then
        the row (events and revisions by cascade)."""
        held = self._find("assistant_profiles", user_id=user)
        if held is not None and held.get("legal_hold"):
            return {"ok": False, "code": "legal_hold"}
        mid = str(row["id"])
        linked = {row.get("message_id")}
        linked |= {e.get("message_id") for e in self.tables.get("assistant_memory_events", [])
                   if str(e.get("memory_id")) == mid}
        linked |= {r.get("message_id") for r in self.tables.get("assistant_memory_revisions", [])
                   if str(r.get("memory_id")) == mid}
        linked |= {t.get("message_id") for t in self.tables.get("assistant_tool_results", [])
                   if str(t.get("user_id")) == user and mid in [str(i) for i in t.get("memory_ids") or []]}
        linked = {str(m) for m in linked if m}
        for m in self.tables.get("assistant_messages", []):
            if str(m.get("user_id")) != user:
                continue
            used = [str(i) for i in m.get("used_memory_ids") or []]
            if mid in used:
                m["used_memory_ids"] = [i for i in used if i != mid]
            blocks = m.get("blocks")
            if isinstance(blocks, list) and (str(m.get("id")) in linked or mid in json.dumps(blocks, default=str)):
                m["blocks"] = [
                    {**b, "items": redact_memory_items(b["items"], mid)}
                    if isinstance(b, dict) and b.get("type") == "transcript" and isinstance(b.get("items"), list)
                    else b
                    for b in blocks
                ]
        for t in self.tables.get("assistant_tool_results", []):
            if (str(t.get("user_id")) == user and str(t.get("tool_name") or "").startswith("memory_")
                    and (mid in [str(i) for i in t.get("memory_ids") or []]
                         or mid in json.dumps(t.get("payload"), default=str))):
                t.update({"payload": {"redacted": True}, "payload_sha256": None, "taint_corpus": None})
        self._cascade_memories({mid})
        self.tables["assistant_memories"] = [m for m in self.tables["assistant_memories"] if str(m.get("id")) != mid]
        return {"ok": True, "deleted": True, "memory_id": mid}

    # ── assistant_approvals rules ──

    def _check_approval(self, row: dict, exclude: dict | None) -> None:
        """The expired_reason check and the one-revert partial unique index."""
        if row.get("status") == "expired" and not row.get("expired_reason"):
            raise TriggerRefused("assistant_approvals_expired_reason_chk (check_violation)")
        if row.get("reverts_approval_id") and row.get("status") in REVERT_LIVE:
            for other in self.tables.get("assistant_approvals", []):
                if other is exclude or other.get("id") == row.get("id"):
                    continue
                if (str(other.get("reverts_approval_id")) == str(row["reverts_approval_id"])
                        and other.get("status") in REVERT_LIVE):
                    raise UniqueViolation("assistant_approvals_one_revert_uq")

    def _update_approval(self, row: dict, patch: dict) -> None:
        """assistant_approval_immutable (0172) plus the checks, in place."""
        new = {**row, **patch}
        for col in APPROVAL_IMMUTABLE:
            if col in patch and new.get(col) != row.get(col):
                raise TriggerRefused("assistant_approvals: the card content and its links are immutable")
        if row.get("superseded_by") is not None and new.get("superseded_by") != row.get("superseded_by"):
            raise TriggerRefused("assistant_approvals: superseded_by is set once")
        # 0180 A5: the reason is set once, and is 10 to 500 characters.
        if row.get("decision_reason") is not None and new.get("decision_reason") != row.get("decision_reason"):
            raise TriggerRefused("assistant_approvals.decision_reason is set once")
        reason = new.get("decision_reason")
        if reason is not None and not 10 <= len(str(reason).strip()) <= 500:
            raise CheckViolation("assistant_approvals_decision_reason_chk (check_violation)")
        if new.get("status") != row.get("status"):
            old_rank = APPROVAL_RANK.get(row.get("status"), 3)
            new_rank = APPROVAL_RANK.get(new.get("status"), 3)
            if new_rank <= old_rank:
                raise TriggerRefused(f"assistant_approvals: status cannot move from {row.get('status')} "
                                     f"to {new.get('status')}")
        self._check_approval(new, exclude=row)
        row.update(patch)

    def _decide(self, p: dict) -> dict:
        """assistant_approval_decide (0172 section C) in Python; jsonb
        equality is dict equality."""
        decision = p["p_decision"]
        if decision not in ("approve", "reject"):
            raise TriggerRefused(f"assistant_approval_decide: bad decision {decision} (check_violation)")
        rows = self.tables.setdefault("assistant_approvals", [])
        row = next((r for r in rows if str(r.get("id")) == str(p["p_approval_id"])
                    and str(r.get("user_id")) == str(p["p_user_id"])), None)
        if (row is not None and row.get("status") == "pending" and _ts(row["expires_at"]) > self.now
                and (decision == "reject" or (row.get("payload_sha256") == p.get("p_payload_sha256")
                                              and row.get("precondition") == p.get("p_precondition")))):
            self._update_approval(row, {
                "status": "approved" if decision == "approve" else "rejected",
                "decided_by": p["p_user_id"], "decided_at": self.now_iso(),
                "decided_session_aal": p.get("p_session_aal"),
            })
            return {"ok": True, "reason": None, "row": dict(row)}
        if row is None:
            return {"ok": False, "reason": "not_found", "row": None}
        if row.get("status") == "pending" and _ts(row["expires_at"]) <= self.now:
            self._update_approval(row, {"status": "expired", "expired_reason": "timed_out"})
            return {"ok": False, "reason": "expired", "row": dict(row)}
        if row.get("status") == "expired" and row.get("superseded_by") is not None:
            return {"ok": False, "reason": "superseded", "row": dict(row)}
        if row.get("status") == "expired":
            return {"ok": False, "reason": "expired", "row": dict(row)}
        if row.get("status") != "pending":
            return {"ok": False, "reason": "not_pending", "row": dict(row)}
        return {"ok": False, "reason": "stale", "row": dict(row)}

    def _supersede(self, p: dict) -> dict:
        """assistant_approval_supersede (0172 section B) in Python: expire
        the executing row, insert the new one, all or nothing."""
        new = dict(p["p_new"] or {})
        if not new.get("id") or str(new.get("user_id")) != str(p["p_user_id"]):
            raise TriggerRefused("assistant_approval_supersede: bad new row (check_violation)")
        snapshot = copy.deepcopy(self.tables)
        rows = self.tables.setdefault("assistant_approvals", [])
        old = next((r for r in rows if str(r.get("id")) == str(p["p_old_id"])
                    and str(r.get("user_id")) == str(p["p_user_id"]) and r.get("status") == "executing"), None)
        if old is None:
            return {"ok": False, "reason": "not_executing", "row": None}
        try:
            self._update_approval(old, {"status": "expired", "expired_reason": "changed_since_preview",
                                        "superseded_by": new["id"]})
            inserted = self._insert("assistant_approvals", {
                **{col: new.get(col) for col in SUPERSEDE_COLUMNS}, "status": "pending",
            })
        except Exception:
            self.tables = snapshot
            raise
        return {"ok": True, "reason": None, "row": inserted}

    # ── phase 4 (0180) ──

    def _decide_with_reason(self, p: dict) -> dict:
        """assistant_approval_decide_with_reason (0180 A5) in Python: the
        approve CAS plus the reason in one update; anything else is answered
        by the 0172 classification (the real function delegates with a hash
        that cannot match, so the delegation never approves; "!" is not hex
        and cannot collide with a stored sha256 here either)."""
        reason = str(p.get("p_reason") or "").strip()
        if not 10 <= len(reason) <= 500:
            return {"ok": False, "reason": "reason_invalid", "row": None}
        rows = self.tables.setdefault("assistant_approvals", [])
        row = next((r for r in rows if str(r.get("id")) == str(p["p_approval_id"])
                    and str(r.get("user_id")) == str(p["p_user_id"])), None)
        if (row is not None and row.get("status") == "pending" and _ts(row["expires_at"]) > self.now
                and row.get("payload_sha256") == p.get("p_payload_sha256")
                and row.get("precondition") == p.get("p_precondition")):
            self._update_approval(row, {
                "status": "approved", "decided_by": p["p_user_id"], "decided_at": self.now_iso(),
                "decided_session_aal": p.get("p_session_aal"), "decision_reason": reason,
            })
            return {"ok": True, "reason": None, "row": dict(row)}
        return self._decide({**p, "p_decision": "approve", "p_payload_sha256": "!"})

    def _insert_evidence_access(self, row: dict) -> None:
        for col in ("subject_user_id", "subject_label", "project_id", "xlsx_report_id", "conversation_id",
                    "run_id"):
            row.setdefault(col, None)
        row.setdefault("filters", {})
        row.setdefault("row_count", 0)
        row.setdefault("truncated", False)
        if row.get("subject_kind") not in ("user", "estimator"):
            raise CheckViolation("assistant_evidence_access_subject_kind_check (check_violation)")
        if row.get("tool") not in ("person_evidence", "estimator_timeline"):
            raise CheckViolation("assistant_evidence_access_tool_check (check_violation)")
        if row.get("view") not in EVIDENCE_VIEWS:
            raise CheckViolation("assistant_evidence_access_view_check (check_violation)")
        if not isinstance(row["filters"], dict):
            raise CheckViolation("assistant_evidence_access_filters_check (check_violation)")
        if not 10 <= len(str(row.get("reason") or "").strip()) <= 500:
            raise CheckViolation("assistant_evidence_access_reason_check (check_violation)")
        if row.get("approval_id") is None:
            raise CheckViolation("assistant_evidence_access.approval_id is not null (not_null_violation)")
        if any(str(r.get("approval_id")) == str(row["approval_id"])
               for r in self.tables.get("assistant_evidence_access", [])):
            raise UniqueViolation("assistant_evidence_access_approval_uq")

    def _insert_report(self, row: dict) -> None:
        for col in ("conversation_id", "run_id", "approval_id", "purged_at"):
            row.setdefault(col, None)
        row.setdefault("params", {})
        row.setdefault("row_count", 0)
        row.setdefault("truncated", False)
        row.setdefault("bytes", 0)
        row.setdefault("status", "ready")
        if row.get("spec") not in REPORT_SPECS:
            raise CheckViolation("assistant_reports_spec_check (check_violation)")
        if row.get("status") not in ("ready", "purged"):
            raise CheckViolation("assistant_reports_status_check (check_violation)")
        if not 1 <= len(str(row.get("title") or "")) <= 200:
            raise CheckViolation("assistant_reports_title_check (check_violation)")
        if not REPORT_FILENAME.match(str(row.get("filename") or "")):
            raise CheckViolation("assistant_reports_filename_check (check_violation)")
        if row.get("expires_at") is None:
            raise CheckViolation("assistant_reports.expires_at is not null (not_null_violation)")
        expected = f"assistant-reports/{row.get('user_id')}/{row['id']}.xlsx"
        if row.get("storage_path") != expected:
            raise CheckViolation("assistant_reports_path_chk (check_violation)")
        if (row["status"] == "purged") != (row["purged_at"] is not None):
            raise CheckViolation("assistant_reports_purged_chk (check_violation)")
        if row["approval_id"] is not None and any(
                str(r.get("approval_id")) == str(row["approval_id"])
                for r in self.tables.get("assistant_reports", [])):
            raise UniqueViolation("assistant_reports_approval_uq")

    def _update_report(self, row: dict, patch: dict) -> dict:
        new = {**row, **patch}
        for col in ("id", "user_id", "storage_path"):
            if col in patch and new.get(col) != row.get(col):
                raise CheckViolation("assistant_reports_path_chk (check_violation)")
        if (new.get("status") == "purged") != (new.get("purged_at") is not None):
            raise CheckViolation("assistant_reports_purged_chk (check_violation)")
        row.update(patch)
        return row

    def _insert_plans_note(self, row: dict) -> None:
        for col in ("document_id", "page", "sheet_number", "sheet_title", "title", "quote", "section", "severity"):
            row.setdefault(col, None)
        row.setdefault("category", "")
        if row.get("source_scope") not in ("category", "main", "brief_deliverable"):
            raise CheckViolation("plans_reader_notes_source_scope_check (check_violation)")
        if row.get("kind") not in NOTE_KINDS:
            raise CheckViolation("plans_reader_notes_kind_check (check_violation)")
        if not 1 <= len(str(row.get("body") or "")) <= 4000:
            raise CheckViolation("plans_reader_notes_body_check (check_violation)")
        if not 1 <= len(str(row.get("source_key") or "")) <= 200:
            raise CheckViolation("plans_reader_notes_source_key_check (check_violation)")
        if int(row.get("source_version") or 0) < 1:
            raise CheckViolation("plans_reader_notes_source_version_check (check_violation)")
        if row.get("page") is not None and int(row["page"]) < 1:
            raise CheckViolation("plans_reader_notes_page_check (check_violation)")
        if any(all(str(r.get(k)) == str(row.get(k)) for k in NOTE_KEY)
               for r in self.tables.get("plans_reader_notes", [])):
            raise UniqueViolation("plans_reader_notes_key_uq")

    def _memory_search(self, p: dict) -> list[dict]:
        """assistant_memory_search (0180 A6): the person's own notes by token
        overlap with the title and body (the stand-in for the tsvector and
        trigram rank), archived never."""
        user = str(p["p_user_id"])
        statuses = p.get("p_statuses") or ["active", "pending_review"]
        want = self._tokens(p.get("p_q"))
        out = []
        for m in self.tables.get("assistant_memories", []):
            if str(m.get("user_id")) != user or m.get("status") == "archived" or m.get("status") not in statuses:
                continue
            if p.get("p_kind") and m.get("kind") != p["p_kind"]:
                continue
            title, body = self._tokens(m.get("title")), self._tokens(m.get("body"))
            hits = len(want & title) * 2 + len(want & body)
            if not want or not hits:
                continue
            out.append({
                "id": m["id"], "kind": m.get("kind"), "status": m.get("status"), "title": m.get("title"),
                "body": m.get("body"), "project_id": m.get("project_id"), "pinned": bool(m.get("pinned")),
                "origin_tainted": bool(m.get("origin_tainted")), "updated_at": m.get("updated_at"),
                "rank": float(hits),
            })
        out.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
        out.sort(key=lambda r: -r["rank"])
        return out[: max(1, min(25, int(p.get("p_limit") or 10)))]

    def _search_plans_notes(self, p: dict) -> list[dict]:
        """search_plans_notes (0180 A2): a case-insensitive substring match
        on the body and the sheet number, filtered like the RPC."""
        needle = str(p.get("p_q") or "").lower().strip()
        run_ids = {str(r) for r in (p.get("p_run_ids") or [])}
        kinds = p.get("p_kinds")
        out = []
        for n in self.tables.get("plans_reader_notes", []):
            if str(n.get("run_id")) not in run_ids:
                continue
            if kinds is not None and n.get("kind") not in kinds:
                continue
            if p.get("p_category") is not None and n.get("category") != p["p_category"]:
                continue
            hay = f"{n.get('body') or ''} {n.get('sheet_number') or ''}".lower()
            if not needle or needle not in hay:
                continue
            out.append({**{k: v for k, v in n.items() if k not in ("created_at", "tsv")}, "rank": 1.0})
        out.sort(key=lambda r: (-r["rank"], str(r.get("run_id")), r.get("page") is None, r.get("page") or 0))
        return out[: max(1, min(50, int(p.get("p_limit") or 30)))]

    # Money arguments of the 0174 rpcs: always strings on the wire (3.8.1;
    # repo._money), never a float or a Decimal.
    SPEND_MONEY_ARGS = ("p_amount", "p_user_cap_default", "p_global_cap_default")

    def rpc(self, name, params):
        self.rpc_calls.append((name, dict(params)))
        if name in self.rpc_errors:
            raise self.rpc_errors.pop(name)
        if name == "assistant_browser_revoke_owner":
            owner, count = params["p_user_id"], 0
            for row in self.tables.setdefault("assistant_credentials", []):
                if row.get("user_id") == owner and row.get("status") != "revoked":
                    row.update(status="revoked", version=row["version"] + 1,
                               ciphertext=None, wrapped_dek=None, nonce=None, key_id=None)
                    count += 1
            self.tables["assistant_browser_sessions"] = [
                row for row in self.tables.get("assistant_browser_sessions", []) if row.get("user_id") != owner]
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=count))
        if name in P7_RPCS:
            data = getattr(self, P7_RPCS[name])(dict(params))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))
        if name in P8_RPCS:
            data = getattr(self, P8_RPCS[name])(dict(params))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))
        if name in self.PHASE3_RPCS:
            data = getattr(self, self.PHASE3_RPCS[name])(dict(params))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))
        if name in self.PHASE4_RPCS:
            data = getattr(self, self.PHASE4_RPCS[name])(dict(params))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))
        if name in ("assistant_spend_state", "assistant_spend_reserve"):
            for key in self.SPEND_MONEY_ARGS:
                if key in params:
                    assert isinstance(params[key], str), f"{name}: {key} must travel as a string"
        if name == "assistant_spend_state":
            data = self._spend_state(dict(params))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))
        if name == "assistant_spend_reserve":
            data = self._spend_reserve(dict(params))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))
        if name == "assistant_approval_decide":
            data = self._decide(dict(params))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))
        if name == "assistant_approval_supersede":
            data = self._supersede(dict(params))
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))
        if name != "assistant_tool_call_begin":
            raise AssertionError(f"unexpected rpc {name}")
        key = params["p_idempotency_key"]
        rows = [r for r in self.tables.setdefault("assistant_tool_calls", []) if r.get("idempotency_key") == key]
        if rows:
            data = {"inserted": False, "rows": [dict(r) for r in rows]}
        else:
            row = {k[2:]: v for k, v in params.items()}
            row["phase"] = "begun"
            data = {"inserted": True, "rows": [self._insert("assistant_tool_calls", row)]}
        # The real client's rpc() is a builder: .execute() returns the data.
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))


def redact_memory_items(items: list, memory_id: str) -> list:
    """assistant_redact_memory_items (0176 B5b) in Python: every memory_*
    function_call and its function_call_output (by call_id), and any item
    naming the full id, lose their text."""
    calls = {i.get("call_id") for i in items if isinstance(i, dict) and i.get("type") == "function_call"
             and str(i.get("name") or "").startswith("memory_") and i.get("call_id") is not None}
    out = []
    for item in items:
        obj = item if isinstance(item, dict) else {}
        named = bool(memory_id) and memory_id in json.dumps(item, default=str)
        hit = obj.get("call_id") is not None and obj.get("call_id") in calls
        if obj.get("type") == "function_call" and (hit or named):
            out.append({"type": "function_call", "call_id": obj.get("call_id"), "name": obj.get("name"),
                        "arguments": '{"redacted":true}'})
        elif obj.get("type") == "function_call_output" and (hit or named):
            out.append({"type": "function_call_output", "call_id": obj.get("call_id"),
                        "output": '{"redacted":true}'})
        elif named:
            out.append({"type": obj.get("type"), "redacted": True})
        else:
            out.append(item)
    return out


def profile(**over) -> dict:
    base = {
        "id": USER, "full_name": "Tom Moore", "email": "tom@g3.test", "role": "executive",
        "is_active": True, "is_dev": False, "mfa_enrolled": True,
        "invite_accepted_at": "2026-01-01T00:00:00+00:00", "rfp_mailboxes": [],
    }
    base.update(over)
    return base


def delegation(**over) -> dict:
    base = {"id": "del-1", "user_id": USER, "created_session_aal": "aal2",
            "created_at": "2026-10-01T00:00:00+00:00", "expires_at": None, "revoked_at": None}
    base.update(over)
    return base


def settings_row(**over) -> dict:
    base = {"id": 1, "enabled": True, "model": None, "global_daily_cap_usd": 50,
            "disabled_tools": [], "holidays": [], "updated_at": NOW.isoformat()}
    base.update(over)
    return base


def conversation(**over) -> dict:
    base = {"id": CONV, "user_id": USER, "title": None, "surface": "panel", "mode": "normal",
            "project_id": None, "tainted_at": None, "mailbox_tainted_at": None,
            "last_message_at": None, "archived_at": None,
            "created_at": (NOW - timedelta(minutes=5)).isoformat(), "updated_at": NOW.isoformat()}
    base.update(over)
    return base


def message(**over) -> dict:
    base = {"id": MSG_USER, "conversation_id": CONV, "user_id": USER, "role": "user",
            "content": "What is due today?", "blocks": [], "tool_calls": [], "context": None,
            "status": "done", "error_kind": None, "created_at": (NOW - timedelta(seconds=2)).isoformat()}
    base.update(over)
    return base


def pending_answer(**over) -> dict:
    base = message(id=MSG_ASSISTANT, role="assistant", content="", status="pending",
                   created_at=(NOW - timedelta(seconds=1)).isoformat())
    base.update(over)
    return base


def approval(**over) -> dict:
    """A pending approval row as 0170 plus 0172 store it."""
    base = {
        "id": APPROVAL_ID, "user_id": USER, "user_label": "Tom Moore", "conversation_id": CONV,
        "message_id": MSG_ASSISTANT, "tool_call_id": None, "project_id": PROJECT,
        "action_kind": "probe_write", "payload": {"v": 1, "args": {}}, "payload_sha256": "0" * 64,
        "preview": {"v": 1, "title": "Probe", "rows": []}, "precondition": {"v": 1}, "status": "pending",
        "expired_reason": None, "decided_by": None, "decided_at": None, "decided_session_aal": None,
        "execute_job_id": None, "executed_at": None, "result": None, "email_log_id": None,
        "expires_at": (NOW + timedelta(hours=1)).isoformat(), "superseded_by": None,
        "reverts_approval_id": None, "decision_reason": None,
        "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
    }
    base.update(over)
    return base


STAGED = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
STAGED2 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"
TASK = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
SPLIT_JOB = "dddddddd-dddd-4ddd-8ddd-ddddddddddd1"
HOLD = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1"
OTHER_USER = "33333333-3333-4333-8333-333333333333"


def staged_file(**over) -> dict:
    """A live, unlinked, unclaimed assistant_files row (0170 plus 0174)."""
    fid = over.get("id", STAGED)
    base = {
        "id": fid, "user_id": USER, "conversation_id": None, "message_id": None, "filename": "A-101.pdf",
        "storage_path": f"assistant-files/{USER}/{fid}-A-101.pdf", "size_bytes": 1234,
        "sha256": "a" * 64, "mime": "application/pdf", "page_count": 3, "consumed_at": None,
        "consumed_project_id": None, "consumed_task_id": None,
        "expires_at": (NOW + timedelta(days=7)).isoformat(), "purged_at": None,
        "created_at": (NOW - timedelta(minutes=1)).isoformat(),
    }
    base.update(over)
    return base


def task(**over) -> dict:
    """A split task waiting on its splitter job (0170 plus 0174)."""
    base = {
        "id": TASK, "user_id": USER, "kind": "split_into_project", "status": "waiting_external",
        "title": "Split 2 files into 26.9.7130", "instruction": None, "project_id": PROJECT,
        "conversation_id": CONV, "message_id": MSG_ASSISTANT, "split_job_id": SPLIT_JOB,
        "payload": {"file_ids": [STAGED, STAGED2], "pages": 5, "estimate_usd": "0.40", "max_usd": "0.40"},
        "result": None, "failure_reason": None, "created_by": USER, "finished_at": None,
        "created_at": (NOW - timedelta(minutes=2)).isoformat(), "updated_at": (NOW - timedelta(minutes=2)).isoformat(),
        "approval_id": APPROVAL_ID, "run_id": RUN, "spend_reservation_id": HOLD, "result_message_id": None,
    }
    base.update(over)
    return base


def reservation(**over) -> dict:
    """An active hold (0174 assistant_spend_reservations)."""
    base = {
        "id": HOLD, "user_id": USER, "kind": "split_task", "run_id": RUN, "task_id": TASK,
        "approval_id": APPROVAL_ID, "amount_usd": 0.4, "status": "active", "actual_usd": None,
        "created_at": NOW.isoformat(), "expires_at": (NOW + timedelta(hours=6)).isoformat(), "settled_at": None,
    }
    base.update(over)
    return base


REPORT = "12121212-1212-4212-8212-121212121211"
EVIDENCE = "13131313-1313-4313-8313-131313131311"
PLANS_RUN = "14141414-1414-4414-8414-141414141411"
PLANS_NOTE = "15151515-1515-4515-8515-151515151511"


def evidence_access(**over) -> dict:
    """An assistant_evidence_access row (0180 A3)."""
    base = {
        "id": EVIDENCE, "reader_user_id": USER, "reader_label": "Tom Moore", "reader_role": "executive",
        "subject_kind": "user", "subject_user_id": OTHER_USER, "subject_label": "Sam Estimator",
        "project_id": PROJECT, "tool": "person_evidence", "view": "activity", "filters": {},
        "row_count": 3, "truncated": False, "xlsx_report_id": None,
        "reason": "Checking why the bid went out late", "approval_id": APPROVAL_ID,
        "conversation_id": CONV, "run_id": RUN, "created_at": NOW.isoformat(),
    }
    base.update(over)
    return base


def report(**over) -> dict:
    """A ready assistant_reports row (0180 A4); the storage path follows the
    id and the owner unless given."""
    rid = over.get("id", REPORT)
    owner = over.get("user_id", USER)
    base = {
        "id": rid, "user_id": owner, "spec": "bid_invitations", "params": {}, "title": "Bid Invitations: week",
        "filename": "Bid Invitations week.xlsx", "storage_path": f"assistant-reports/{owner}/{rid}.xlsx",
        "row_count": 12, "truncated": False, "bytes": 4096, "conversation_id": CONV, "run_id": RUN,
        "approval_id": None, "status": "ready", "expires_at": (NOW + timedelta(days=7)).isoformat(),
        "purged_at": None, "created_at": NOW.isoformat(),
    }
    base.update(over)
    return base


def plans_note(**over) -> dict:
    """A plans_reader_notes row (0180 A1)."""
    base = {
        "id": PLANS_NOTE, "run_id": PLANS_RUN, "source_scope": "category", "category": "gear",
        "source_version": 1, "source_key": "notes[0]", "kind": "page_note", "document_id": None, "page": 14,
        "sheet_number": "E-201", "sheet_title": "Panel schedules", "title": None,
        "body": "Panel 1HD-21 is fed from the main switchboard", "quote": None, "section": None,
        "severity": None, "created_at": NOW.isoformat(),
    }
    base.update(over)
    return base


SCHEDULE = "ffffffff-ffff-4fff-8fff-fffffffffff1"
SCHEDULE2 = "ffffffff-ffff-4fff-8fff-fffffffffff2"
SCHEDULE_RUN = "abababab-abab-4bab-8bab-abababababa1"
MEMORY = "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdc1"
MEMORY2 = "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdc2"
WEEKDAYS_7AM = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=7;BYMINUTE=0"


def schedule(**over) -> dict:
    """An active weekday due_today routine with both occurrence slots
    filled (0170 plus 0176)."""
    base = {
        "id": SCHEDULE, "user_id": USER, "title": "Bids due today, weekdays at 7:00 AM", "recipe": "due_today",
        "params": {}, "rrule": WEEKDAYS_7AM, "timezone": "America/Los_Angeles", "status": "active",
        "paused_reason": None, "sub_app": "bidding", "delivery": "chat_bell", "only_if_changed": False,
        "last_result_hash": None, "project_id": None, "conversation_id": None,
        "next_run_at": (NOW + timedelta(hours=1)).isoformat(), "last_run_at": None,
        "consecutive_failures": 0, "unread_deliveries": 0, "ended_at": None,
        "created_at": (NOW - timedelta(days=1)).isoformat(), "updated_at": (NOW - timedelta(days=1)).isoformat(),
        "dtstart": (NOW - timedelta(days=1)).isoformat(), "after_next_run_at": (NOW + timedelta(days=1)).isoformat(),
        "approval_id": None, "paused_at": None, "ended_reason": None, "last_delivered_at": None,
        "last_seen_at": None,
    }
    base.update(over)
    return base


def schedule_run(**over) -> dict:
    """A scheduled run a worker claimed (0170 plus 0176)."""
    base = {
        "id": SCHEDULE_RUN, "schedule_id": SCHEDULE, "user_id": USER, "user_label": "Tom Moore",
        "scheduled_for": (NOW - timedelta(minutes=1)).isoformat(), "kind": "scheduled", "status": "claimed",
        "claimed_by": "worker-a", "started_at": NOW.isoformat(), "finished_at": None, "result_hash": None,
        "delivered": None, "message_id": None, "error_kind": None, "error": None, "stats": None,
        "created_at": NOW.isoformat(),
    }
    base.update(over)
    return base


def memory_row(**over) -> dict:
    """An active, user-stated preference (0170 plus 0176)."""
    base = {
        "id": MEMORY, "user_id": USER, "kind": "preference", "title": "Sort bids by due time", "body": "",
        "project_id": None, "status": "active", "source": "user_stated", "pinned": False,
        "origin_tainted": False, "conversation_id": None, "message_id": None, "last_used_at": None,
        "use_count": 0, "archived_at": None, "idempotency_key": None,
        "created_at": (NOW - timedelta(days=2)).isoformat(), "updated_at": (NOW - timedelta(days=2)).isoformat(),
    }
    base.update(over)
    return base


class FakeStorage:
    """The storage functions staging and split-drop call, over a dict of
    objects. `install(monkeypatch)` patches app.services.storage."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.fail_delete = False

    def upload_file(self, path, content, content_type, *, upsert=False):
        if path in self.objects and not upsert:
            raise RuntimeError("The resource already exists (409 Duplicate)")
        self.objects[path] = bytes(content)

    def delete_file(self, path):
        if self.fail_delete:
            raise RuntimeError("storage down")
        self.deleted.append(path)
        self.objects.pop(path, None)

    def copy_file(self, from_path, to_path):
        if from_path not in self.objects:
            raise RuntimeError("Object not found")
        self.objects[to_path] = self.objects[from_path]

    def install(self, monkeypatch) -> "FakeStorage":
        from app.services import storage

        for name in ("upload_file", "delete_file", "copy_file"):
            monkeypatch.setattr(storage, name, getattr(self, name))
        return self


# ── Phase 1 probe write tools over an in-memory BDR ──────────────────────────
#
# Three registry specs that behave like the phase 1 write tools without
# touching the served app: probe_set (a labor-like PUT with a revert),
# probe_restore (its hidden revert spec) and probe_create (a create with
# an idempotency key and a recovery lookup). ProbeBDR answers their
# requests; install it as every Executor's transport with `install_probes`.

LABOR_PUT = ("PUT", "/projects/{project_id}/labor")
LABOR_GET = ("GET", "/projects/{project_id}/labor")
PROJECT_GET = ("GET", "/projects/{project_id}")
CREATE_POST = ("POST", "/projects")
SIMILAR_POST = ("POST", "/projects/similar")
CREATED_GET = ("GET", "/projects/assistant-created/{key}")
EXPECTED_HEADER = "X-Assistant-Expected-Updated-At"
IDEMPOTENCY_HEADER = "X-Assistant-Idempotency-Key"
CREATED_PROJECT = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


class ProbeBDR:
    """The routes behind the probe tools. `responses[route]` overrides one
    route: a (status, payload) pair, or a callable(req) returning one (or
    None to fall through to the default behaviour)."""

    def __init__(self):
        self.labor: dict | None = {"amount": "100.00", "notes": None, "updated_at": "2026-10-22T10:00:00+00:00"}
        self.project = {"id": PROJECT, "number": "26.9.7130", "name": "ZZ TEST Probe", "closed": False,
                        "phase": "open"}
        self.similar: list[dict] = []
        self.created: dict | None = None
        self.sent: list[dict] = []
        self.responses: dict = {}
        self._clock = 0

    def human_save(self, **values) -> None:
        """A human save between the preview and the execution."""
        self._clock += 1
        self.labor = {**(self.labor or {"amount": None, "notes": None}), **values,
                      "updated_at": f"2026-10-22T11:00:{self._clock:02d}+00:00"}

    def send(self, req, token):
        self.sent.append({"route": req.route, "path": req.path, "json": req.json,
                          "headers": dict(req.headers or {}), "token": token})
        override = self.responses.get(req.route)
        if override is not None:
            out = override(req) if callable(override) else override
            if out is not None:
                return out
        if req.route == LABOR_GET:
            return 200, (dict(self.labor) if self.labor else None)
        if req.route == PROJECT_GET:
            return 200, dict(self.project)
        if req.route == LABOR_PUT:
            expected = dict(req.headers or {}).get(EXPECTED_HEADER)
            current = (self.labor or {}).get("updated_at") or "none"
            if expected is not None and expected != current:
                return 409, {"detail": {"code": "labor_changed", "message": "The labor numbers changed."}}
            self.human_save(**(req.json or {}))
            return 200, dict(self.labor)
        if req.route == SIMILAR_POST:
            return 200, list(self.similar)
        if req.route == CREATE_POST:
            self.created = {"id": CREATED_PROJECT, "number": "26.10.9001", "name": (req.json or {}).get("name"),
                            "key": dict(req.headers or {}).get(IDEMPOTENCY_HEADER)}
            return 201, dict(self.created)
        if req.route == CREATED_GET:
            if self.created and req.path.endswith(str(self.created.get("key"))):
                return 200, {k: self.created[k] for k in ("id", "number", "name")}
            return 404, {"detail": "Not Found"}
        return 404, {"detail": "Not Found"}


def _probe_specs():
    """The three probe specs (built lazily so importing this module never
    loads the registry)."""
    from app.assistant import registry
    from app.assistant.registry import (
        Proposal,
        ProposalRefused,
        RevertNotPossible,
        ToolRequest,
        ToolSpec,
        WriteSpec,
        nullable,
        schema,
    )
    from app.assistant.tools._common import ToolArgumentError
    from app.core.roles import WRITER_ROLES

    def labor_reads(args):
        pid = args["project_id"]
        return [ToolRequest(route=LABOR_GET, path=f"/projects/{pid}/labor"),
                ToolRequest(route=PROJECT_GET, path=f"/projects/{pid}")]

    def labor_snapshot(args, reads):
        labor, project = (reads[0] if reads else None), (reads[1] if len(reads) > 1 else {}) or {}
        target = {"exists": labor is not None, "amount": (labor or {}).get("amount"),
                  "notes": (labor or {}).get("notes"), "updated_at": (labor or {}).get("updated_at")}
        return target, {"closed": bool(project.get("closed")), "phase": project.get("phase") or "open"}

    def labor_card(title, args, reads, merged, target, gate):
        project = reads[1] or {}
        rows = [{"field": k, "label": k.title(), "before": str(target.get(k)), "after": str(merged.get(k)),
                 "changed": merged.get(k) != target.get(k)} for k in ("amount", "notes")]
        preview = {"v": 1, "title": f"{title} on {project.get('number')}", "tool_label": "Probe",
                   "project": {"id": project.get("id"), "number": project.get("number"), "name": project.get("name")},
                   "rows": rows, "warnings": []}
        req = ToolRequest(route=LABOR_PUT, path=f"/projects/{args['project_id']}/labor", json=merged)
        return Proposal(request=req, project_id=args["project_id"], target=target, gate=gate, preview=preview,
                        summary=f"{title}: amount {merged.get('amount')}.")

    def set_build(args, reads, ctx):
        target, gate = labor_snapshot(args, reads)
        if gate["closed"]:
            raise ProposalRefused("This project is abandoned, declined or marked failed to bid.")
        amount, notes = args.get("amount"), args.get("notes")
        if amount is not None and not str(amount).replace(".", "", 1).isdigit():
            raise ToolArgumentError("amount must be a money string")
        if amount is None and notes is None:
            raise ProposalRefused("Nothing to change.")
        merged = {"amount": amount if amount is not None else target["amount"],
                  "notes": notes if notes is not None else target["notes"]}
        if merged == {"amount": target["amount"], "notes": target["notes"]}:
            raise ProposalRefused("The labor numbers already have these values.")
        return labor_card("Set probe", args, reads, merged, target, gate)

    def restore_build(args, reads, ctx):
        target, gate = labor_snapshot(args, reads)
        merged = {"amount": args.get("amount"), "notes": args.get("notes")}
        if merged == {"amount": target["amount"], "notes": target["notes"]}:
            raise ProposalRefused("The labor numbers already have these values.")
        return labor_card("Restore probe", args, reads, merged, target, gate)

    def revert_args(orig, before, after):
        bt = before["target"]
        if not bt.get("exists"):
            raise RevertNotPossible("There was no labor row before this change.")
        return {"project_id": orig["project_id"], "amount": bt["amount"], "notes": bt["notes"]}

    def exec_headers(args, reads):
        return {EXPECTED_HEADER: (reads[0] or {}).get("updated_at") or "none"}

    def create_reads(args):
        return [ToolRequest(route=SIMILAR_POST, path="/projects/similar", json={"name": args["name"]})]

    def create_snapshot(args, reads):
        return {"similar_ids": sorted(str(r.get("id")) for r in (reads[0] or []))}, {"closed": False}

    def create_build(args, reads, ctx):
        target, gate = create_snapshot(args, reads)
        req = ToolRequest(route=CREATE_POST, path="/projects", json={"name": args["name"]},
                          headers={IDEMPOTENCY_HEADER: ctx.approval_id})
        preview = {"v": 1, "title": "Create project", "tool_label": "New project",
                   "rows": [{"field": "name", "label": "Name", "before": "Empty", "after": args["name"],
                             "changed": True}]}
        return Proposal(request=req, project_id=None, target=target, gate=gate, preview=preview,
                        summary=f"New project {args['name']}.")

    common = dict(namespace="bidding", sub_app="bidding", effect="write_internal", confirm=True,
                  response_trimmer=lambda p, a, c: p, roles=WRITER_ROLES, description="probe write",
                  requests=lambda a: [])
    labor_schema = schema({"project_id": {"type": "string"}, "amount": nullable({"type": "string"}),
                           "notes": nullable({"type": "string"}), "url": nullable({"type": "string"})})
    probe_set = ToolSpec(
        name="probe_set", route=LABOR_PUT, extra_routes=(LABOR_GET, PROJECT_GET), schema=labor_schema,
        progress_label="Setting probe labor", **common,
        write=WriteSpec(preview_requests=labor_reads, build=set_build, snapshot=labor_snapshot,
                        bound_text_fields=("notes",), bound_url_fields=("url",), revert_tool="probe_restore",
                        revert_args=revert_args, exec_headers=exec_headers,
                        result_summary=lambda p: {"amount": (p or {}).get("amount")},
                        writes_tables=("labor_reviews",)),
    )
    probe_restore = ToolSpec(
        name="probe_restore", route=LABOR_PUT, extra_routes=(LABOR_GET, PROJECT_GET), schema=labor_schema,
        progress_label="Restoring probe labor", offered=False, **common,
        write=WriteSpec(preview_requests=labor_reads, build=restore_build, snapshot=labor_snapshot,
                        exec_headers=exec_headers, result_summary=lambda p: {"amount": (p or {}).get("amount")}),
    )
    probe_create = ToolSpec(
        name="probe_create", route=CREATE_POST, extra_routes=(SIMILAR_POST, CREATED_GET),
        schema=schema({"name": {"type": "string"}}), progress_label="Creating a probe project", **common,
        write=WriteSpec(
            preview_requests=create_reads, build=create_build, snapshot=create_snapshot, creates="project",
            bound_text_fields=("name",),
            created_ref=lambda a, p: {"kind": "project", "id": p["id"], "label": p.get("number"),
                                      "href": f"/projects/{p['id']}"},
            result_summary=lambda p: {k: (p or {}).get(k) for k in ("id", "number", "name")},
            recover_request=lambda a, key: ToolRequest(route=CREATED_GET, path=f"/projects/assistant-created/{key}"),
        ),
    )
    return {s.name: registry.validate(s) for s in (probe_set, probe_restore, probe_create)}


def install_probes(monkeypatch, bdr: ProbeBDR | None = None) -> tuple[ProbeBDR, dict]:
    """Register the probe specs for one test and route every Executor's
    requests to `bdr` (the shim is never reached). Returns (bdr, specs)."""
    from app.assistant import executor as executor_mod
    from app.assistant import registry

    bdr = bdr or ProbeBDR()
    specs = _probe_specs()
    registry.all_tools()  # load the real modules first
    for spec in specs.values():
        monkeypatch.setitem(registry._REGISTRY, spec.name, spec)
    monkeypatch.setattr(executor_mod.Executor, "_send_via_shim", lambda self, req, token: bdr.send(req, token))
    return bdr, specs


# ── Phase 7 (0190) row builders ──────────────────────────────────────────────

EVENT = "e1e1e1e1-e1e1-4e1e-8e1e-e1e1e1e1e1e1"
INBOX = "b1b1b1b1-b1b1-4b1b-8b1b-b1b1b1b1b1b1"
INBOX2 = "b2b2b2b2-b2b2-4b2b-8b2b-b2b2b2b2b2b2"
BELL = "a1a1a1a1-a1a1-4a1a-8a1a-a1a1a1a1a1a1"
BELL2 = "a2a2a2a2-a2a2-4a2a-8a2a-a2a2a2a2a2a2"
DIGEST = "d1d1d1d1-d1d1-4d1d-8d1d-d1d1d1d1d1d1"
SEND = "5e5e5e5e-5e5e-45e5-85e5-5e5e5e5e5e51"
P7_OTHER = "33333333-3333-4333-8333-333333333333"
P7_T0 = datetime(2026, 10, 30, 14, 0, 0, tzinfo=timezone.utc)  # a Friday, 7:00 AM Pacific


def event_row(**over) -> dict:
    """A pending outbox event (0170 plus 0190 A2)."""
    base = {
        "id": EVENT, "event_type": "todo.due", "dedupe_key": "p7:todo.due:t1:2026-10-30", "project_id": None,
        "subject_user_id": USER, "payload": {}, "occurred_at": P7_T0.isoformat(), "processed_at": None,
        "created_at": P7_T0.isoformat(), "status": "pending", "source": "sweeper", "claimed_by": None,
        "claimed_at": None, "attempts": 0, "next_attempt_at": None, "last_error": None, "routed": {},
    }
    base.update(over)
    return base


def inbox_row(**over) -> dict:
    """A router-owned immediate row, due and undelivered (0170 plus 0190 A5)."""
    base = {
        "id": INBOX, "user_id": USER, "dedupe_key": "p7:todo.due:t1:2026-10-30", "event_id": EVENT,
        "severity": "high", "title": "A to-do is due today", "why": "You own it.", "payload": {},
        "project_id": None, "delivered_at": None, "read_at": None, "feedback": None, "feedback_at": None,
        "created_at": P7_T0.isoformat(), "event_type": "todo.due", "delivery": "immediate",
        "precedence": None, "deliver_after": P7_T0.isoformat(), "digest_id": None, "notification_id": None,
        "source_notification_id": None, "dismissed_at": None, "dismissed_reason": None, "snooze_count": 0,
    }
    base.update(over)
    return base


def subscription_row(**over) -> dict:
    """A person override (user_id set, role null); pass role= and user_id=None for a role default."""
    base = {
        "id": str(uuid.uuid4()), "role": None, "user_id": USER, "event_type": "todo.due", "enabled": True,
        "severity": "normal", "created_at": P7_T0.isoformat(), "updated_at": P7_T0.isoformat(),
        "source": "user", "updated_by": USER,
    }
    base.update(over)
    return base


def digest_row(**over) -> dict:
    base = {
        "id": DIGEST, "user_id": USER, "digest_date": "2026-10-30", "slot": "am", "slot_at": P7_T0.isoformat(),
        "since_at": (P7_T0 - timedelta(days=1)).isoformat(), "item_count": 0, "record_count": 0,
        "notification_id": None, "claimed_by": "worker-a", "created_at": P7_T0.isoformat(),
    }
    base.update(over)
    return base


def send_row(**over) -> dict:
    """A pending Teams ledger row, due now."""
    base = {
        "id": SEND, "user_id": USER, "target_kind": "inbox", "target_id": INBOX, "channel": "teams",
        "status": "pending", "not_before": P7_T0.isoformat(), "claimed_by": None, "claimed_at": None,
        "attempts": 0, "next_attempt_at": None, "sent_at": None, "error_kind": None, "email_log_id": None,
        "created_at": P7_T0.isoformat(), "updated_at": P7_T0.isoformat(),
    }
    base.update(over)
    return base


def bell_row(**over) -> dict:
    """A notifications row (BDR's own bell, or one of the router's)."""
    base = {
        "id": BELL, "user_id": USER, "project_id": None, "type": "quote.received", "message": "A quote arrived.",
        "metadata": {}, "read_at": None, "dismissed_at": None, "created_at": P7_T0.isoformat(),
    }
    base.update(over)
    return base


def cursor_row(**over) -> dict:
    base = {"name": "notifications", "cursor_at": P7_T0.isoformat(), "cursor_id": None, "updated_by": None,
            "updated_at": P7_T0.isoformat()}
    base.update(over)
    return base


def slot_row(**over) -> dict:
    base = {"step": "digest_am", "slot_date": "2026-10-30", "claimed_by": "worker-a",
            "claimed_at": P7_T0.isoformat(), "finished_at": None, "result": {"attempts": 1}}
    base.update(over)
    return base


# ── Phase 7 fake delivery helpers ────────────────────────────────────────────
#
# The router sends only through app/services/assistant_delivery.py, which
# uses graph_email.send_mail, teams_alerts._deliver and email_branding. These
# fakes patch those helpers on the real modules (install_delivery_fakes),
# record every call and raise scripted errors. A script is a list consumed one
# call at a time: None lets the call succeed, an exception raises it before
# anything is sent, and ("after", exc) records the message as delivered and
# then raises (Graph accepted it, the email_log write failed). The error
# factories build each class of plan 8.5 step 6; the *_CLASSES dicts give the
# class the plan assigns (transient: nothing can have been delivered, retry;
# uncertain: delivery unknown, stop; permanent: do not retry).

GRAPH_ERROR_CLASSES = {
    "connect_graph": "transient", "connect_login": "transient", "http_429": "transient", "http_503": "transient",
    "token": "transient", "redirect_refused": "transient",
    "supabase_connect": "uncertain", "http_500": "uncertain", "http_502": "uncertain", "http_504": "uncertain",
    "read_timeout": "uncertain", "write_error": "uncertain",
    "http_400": "permanent", "http_403": "permanent", "http_404": "permanent",
}
TEAMS_ERROR_TEXTS = {
    "resolve_429": "Look up the person: Microsoft Graph refused the call (HTTP 429)",
    "resolve_503": "Install BDR Alerts: Microsoft Graph refused the call (HTTP 503)",
    "graph_unreachable": "Look up the person: could not reach Microsoft Graph (ConnectError)",
    "chat_not_open": "BDR Alerts is installed but Teams has not opened its chat yet",
    "bot_signin": "The bot could not sign in: invalid_client",
    "teams_connect": "Could not reach Teams (ConnectError)",
    "teams_connect_timeout": "Could not reach Teams (ConnectTimeout)",
    "teams_429": "Teams returned HTTP 429",
    "teams_503": "Teams returned HTTP 503",
    "teams_read_timeout": "Could not reach Teams (ReadTimeout)",
    "teams_500": "Teams returned HTTP 500",
    "teams_502": "Teams returned HTTP 502",
    "teams_504": "Teams returned HTTP 504",
    "no_account": "No Microsoft 365 account found for someone@g3.test",
    "install_403": ("Install BDR Alerts: BDR Alerts is not installed for you yet and Teams will not let BDR "
                    "install it. If an admin installed it for everyone, it can take a few hours to arrive "
                    "(signing out of Teams and back in can speed it up) (HTTP 403)"),
    "bot_401": "Teams rejected the bot's sign-in (check the Azure Bot app id)",
    "refused_new_chat": "Teams refused the new BDR Alerts chat",
    "refused_chat": "Teams refused the BDR Alerts chat",
    "teams_400": "Teams returned HTTP 400",
}
TEAMS_ERROR_CLASSES = {
    "resolve_429": "transient", "resolve_503": "transient", "graph_unreachable": "transient",
    "chat_not_open": "transient", "bot_signin": "transient", "teams_connect": "transient",
    "teams_connect_timeout": "transient", "teams_429": "transient", "teams_503": "transient",
    "teams_read_timeout": "uncertain", "teams_500": "uncertain", "teams_502": "uncertain", "teams_504": "uncertain",
    "no_account": "permanent", "install_403": "permanent", "bot_401": "permanent",
    "refused_new_chat": "permanent", "refused_chat": "permanent", "teams_400": "permanent",
}
GRAPH_HOST = "graph.microsoft.com"
LOGIN_HOST = "login.microsoftonline.com"
SUPABASE_HOST = "bpidntbyvoooqvaispup.supabase.co"


def graph_error(kind: str) -> Exception:
    """The exception graph_email.send_mail raises for each class of plan 8.5
    step 6 (the request carries the host the classifier reads)."""
    import httpx

    def request(host: str) -> "httpx.Request":
        return httpx.Request("POST", f"https://{host}/v1.0/users/bids/sendMail")

    def status(code: int, host: str = GRAPH_HOST) -> Exception:
        req = request(host)
        return httpx.HTTPStatusError(f"HTTP {code}", request=req, response=httpx.Response(code, request=req))

    if kind == "connect_graph":
        return httpx.ConnectError("connection refused", request=request(GRAPH_HOST))
    if kind == "connect_login":
        return httpx.ConnectTimeout("login timed out", request=request(LOGIN_HOST))
    if kind == "supabase_connect":
        return httpx.ConnectError("email_log write failed", request=request(SUPABASE_HOST))
    if kind == "read_timeout":
        return httpx.ReadTimeout("read timed out", request=request(GRAPH_HOST))
    if kind == "write_error":
        return httpx.WriteError("write failed", request=request(GRAPH_HOST))
    if kind == "token":
        return RuntimeError("Graph token error: invalid_client")
    if kind == "redirect_refused":
        from app.services.rfp_test import RedirectRefused

        return RedirectRefused("a test session is active and the redirect address is empty")
    if kind.startswith("http_") and kind in GRAPH_ERROR_CLASSES:
        return status(int(kind[5:]))
    raise KeyError(kind)


def teams_error(kind: str) -> Exception:
    """The TeamsAlertError teams_alerts._deliver raises, with the helper's own
    wording for each class of plan 8.5 step 6."""
    from app.services.teams_alerts import TeamsAlertError

    return TeamsAlertError(TEAMS_ERROR_TEXTS[kind])


class _Scripted:
    """The shared script: one entry consumed per call."""

    def __init__(self):
        self.calls: list[dict] = []
        self.script: list = []

    def fail_next(self, *steps) -> "_Scripted":
        """Queue steps: None, an exception, or ("after", exception)."""
        self.script.extend(steps)
        return self

    def _step(self, call: dict) -> None:
        self.calls.append(call)
        step = self.script.pop(0) if self.script else None
        call["delivered"] = False
        if step is None:
            call["delivered"] = True
            return
        if isinstance(step, tuple) and step[0] == "after":
            call["delivered"] = True
            raise step[1]
        raise step


class FakeGraphEmail(_Scripted):
    """graph_email.send_mail and graph_email._active_test_session."""

    def __init__(self):
        super().__init__()
        self.test_session = None
        self.session_checks = 0

    def send_mail(self, *, to, subject, body_html, cc=None, attachments=None, inline_images=None,
                  project_id=None, rfq_id=None, sent_by=None, importance=None, actor_kind=None) -> dict:
        call = {"to": list(to), "subject": subject, "body_html": body_html, "cc": cc, "attachments": attachments,
                "inline_images": inline_images, "project_id": project_id, "rfq_id": rfq_id, "sent_by": sent_by,
                "importance": importance, "actor_kind": actor_kind}
        self._step(call)
        call["log_id"] = str(uuid.uuid4())
        return {"id": call["log_id"], "status": "sent", "subject": subject, "to_addresses": list(to)}

    def _active_test_session(self, sb=None):
        self.session_checks += 1
        return self.test_session

    @property
    def sent(self) -> list[dict]:
        return [c for c in self.calls if c["delivered"]]


class FakeTeamsAlerts(_Scripted):
    """teams_alerts.is_configured, get_state, build_card, _deliver and status."""

    def __init__(self):
        super().__init__()
        self.configured = True
        self.states: dict[str, dict] = {}
        self.cards: list[dict] = []

    def is_configured(self) -> bool:
        return self.configured

    def get_state(self, user_id) -> dict:
        return dict(self.states.get(str(user_id), {}))

    def status(self, user_id) -> dict:
        state = self.get_state(user_id)
        return {"available": self.configured, "enabled": state.get("enabled") is not False,
                "connected": bool(state.get("conversation_id")), "last_sent_at": state.get("last_sent_at"),
                "last_error": state.get("last_error"), "last_error_at": state.get("last_error_at")}

    def build_card(self, *, heading, message, project_label, cta_label, cta_url, lines=None) -> dict:
        card = {"heading": heading, "message": message, "project_label": project_label, "cta_label": cta_label,
                "cta_url": cta_url, "lines": list(lines or [])}
        self.cards.append(card)
        return card

    def deliver(self, user_id, email, card, summary, state) -> dict:
        self._step({"user_id": str(user_id), "email": email, "card": card, "summary": summary, "state": dict(state)})
        return {**state, "last_sent_at": "sent", "last_error": None}


class FakeEmailBranding:
    """email_branding.render_notification_email and logo_bytes."""

    def __init__(self):
        self.renders: list[dict] = []

    def render_notification_email(self, *, recipient_name, heading, message, cta_label, cta_url,
                                  project_label=None, linkify=True, link_hosts=None) -> str:
        self.renders.append({"recipient_name": recipient_name, "heading": heading, "message": message,
                             "cta_label": cta_label, "cta_url": cta_url, "project_label": project_label,
                             "linkify": linkify})
        who = "Hi there," if recipient_name is None else f"Hi {recipient_name},"
        return f"<html><h1>{heading}</h1><p>{who}</p><p>{message}</p><a href=\"{cta_url}\">{cta_label}</a></html>"

    def logo_bytes(self) -> bytes:
        return b"LOGO"


def install_delivery_fakes(monkeypatch) -> SimpleNamespace:
    """Patch the real helper modules with the three fakes. Returns
    SimpleNamespace(graph=, teams=, branding=)."""
    from app.services import email_branding, graph_email, teams_alerts

    graph, teams, branding = FakeGraphEmail(), FakeTeamsAlerts(), FakeEmailBranding()
    monkeypatch.setattr(graph_email, "send_mail", graph.send_mail)
    monkeypatch.setattr(graph_email, "_active_test_session", graph._active_test_session)
    monkeypatch.setattr(teams_alerts, "is_configured", teams.is_configured)
    monkeypatch.setattr(teams_alerts, "get_state", teams.get_state)
    monkeypatch.setattr(teams_alerts, "status", teams.status)
    monkeypatch.setattr(teams_alerts, "build_card", teams.build_card)
    monkeypatch.setattr(teams_alerts, "_deliver", teams.deliver)
    monkeypatch.setattr(email_branding, "render_notification_email", branding.render_notification_email)
    monkeypatch.setattr(email_branding, "logo_bytes", branding.logo_bytes)
    return SimpleNamespace(graph=graph, teams=teams, branding=branding)

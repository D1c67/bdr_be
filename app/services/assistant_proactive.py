"""Phase 7 proactive router: emitters, sweeper, router loop
(docs/ASSISTANT_PHASE7_PLAN.md sections 4 to 9).

Both uvicorn workers run `polling_loop` from the main.py lifespan when
ASSISTANT_ENABLED and ASSISTANT_PROACTIVE_ENABLED are on (6.1). Correctness
lives in Postgres (claim RPC, cursor fences, slot ledger, inbox unique
constraint), so there is no leader election (6.5). The router calls no model.
Plain `def` throughout except `polling_loop`: each tick runs in a thread
(asyncio.to_thread). Assistant tables only through app/assistant/repo.py.

One tick (`poll_once`), each step in its own try so one failure never stops
the others:
  1. the lag guard: a tail cursor older than
     ASSISTANT_PROACTIVE_CURSOR_MAX_LAG_MINUTES is reseeded to now minus
     2 minutes, logging `gap_skipped` only when rows were skipped (4.1);
  2. the two table tails, `notifications` (bell record rows) and `audit_log`
     (failed to bid), keyset (clock, id) with a 2-minute overlap (4.1). They
     run and advance even while the company switch is off;
  3. with the company switch on (assistant_settings.enabled and
     proactive_enabled): the approval-expiring scan, the per-slot sweeper
     (5.2, 5.3), routing of claimed events (6.2), release of due immediate
     rows with the quiet-hours fold and un-fold (8.4), and digest delivery
     for open slots after the slot's finished_at (8.2). With it off, every
     claimed event is finished skipped `switched_off` and nothing is
     delivered;
  4. the daily retention purge after 02:00 PT (5.1);
  5. `assistant_delivery.send_due(now, worker)`, the router's only sending
     path (8.5), which settles its own rows `channel_off` while a switch is off.

The router never sends anything itself: bells and send-ledger rows are written
inside the 0190 RPCs, and the ledger is consumed by assistant_delivery. It
reads neither the quotes table nor the 6:03 digest's ledger (owner questions 8
and 17). Titles, whys and payloads come from proactive.render (ids, enums,
counts, Pacific times, project numbers); no free text and no amount leaves a
function here.

Phase 8 (docs/ASSISTANT_PHASE8_PLAN.md 5.6, 9): the router-owned kind
`assistant.triage_escalation` reaches only its subject; its severity is the
model's hint, lowered only by the person's own `feedback_auto` row; its
template facts are computed here from live state; it is withdrawn at route
time (`triage_off`), at delivery (`superseded`) and at Teams send time when the
company triage switch is off, the fuse latch is set or it was emitted before
the last switch-on; a held, non-urgent one released on a non-workday rings
in-app only. The router digest email honours the person's digest email
switch. `person_eligible` and `missing_quote_categories` are shared with the
triage service, which imports them; this module imports no triage module.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from app.assistant import access, proactive, repo
from app.core import features, supabase_client
from app.core.config import get_settings
from app.core.roles import INTERNAL_ROLES, Role
from app.services import assistant_delivery, due_digest, notification_routing
from app.services.due_reminder_prefs import effective_prefs

logger = logging.getLogger(__name__)

PT = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc

# One id per process, as assistant_scheduler.WORKER_ID (6.1).
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

# ── Tails (4.1) ──────────────────────────────────────────────────────────────
# Exactly two tails (the quotes tail was dropped, owner question 17). Each
# names its clock column; both default to transaction start and both ids are
# uuids. The audit tail reads one action only.
TAILS: dict[str, dict[str, Any]] = {
    "notifications": {
        "table": "notifications",
        "clock": "created_at",
        "columns": "id, user_id, project_id, rfq_id, type, metadata, created_at",
        "action": None,
        "source": "bell_tail",
    },
    "audit_log": {
        "table": "audit_log",
        "clock": "created_at",
        "columns": "id, action, entity, entity_id, payload, created_at",
        "action": proactive.FAILED_TO_BID,
        "source": "audit_tail",
    },
}
TAIL_PAGE = 500
TAIL_OVERLAP = timedelta(minutes=2)
# Bell metadata ids the record payload may carry (4.2 allowlist); `message`
# and every other metadata key stay behind.
BELL_METADATA_KEYS = (
    "stage", "round", "entry_id", "gc_id", "request_id", "revision_id", "approval_id", "rfq_message_id",
)

# ── Routing (6.2, 6.4) ───────────────────────────────────────────────────────
EVENT_LEASE_SECONDS = 600
RETRY_SECONDS = (60, 300, 1800, 7200)      # the event backoff; the claim dead-letters at 5
CLAIM_ROUNDS = 10                          # claim batches per tick at most
RELEASE_LIMIT = 200
# Precedence rules whose rows never get a router Teams message: render allows
# Teams only for push and note outcomes, so a merged row (R4b, R6, R17: the
# reminder, routine or nudge already told the person) rings in-app only.
NO_TEAMS_RULES = frozenset(r["id"] for r in proactive.PRECEDENCE if not {"push", "note"} & set(r["outcomes"]))

# ── Sweeper (5.2, 5.3) ───────────────────────────────────────────────────────
SLOT_MAX_FAILURES = 2
SLOT_GIVE_UP_AFTER = timedelta(minutes=90)
APPROVAL_WARN_BEFORE = timedelta(minutes=60)
APPROVAL_MIN_LIFETIME = timedelta(hours=2)  # only the 4 h cards (approvals.TTL_BY_EFFECT)
# The scan reads only cards expiring at or after now minus this. A card warns
# only while now < expires_at (5.4), so anything older can never qualify;
# without the bound, a backlog of long-expired cards still pending (the
# approvals sweep runs on its own clock) fills the 200-row page ordered by
# expires_at and the cards about to expire are never read. The five minutes
# sit below the rule's lower edge, never above it: the filter only ever reads
# a little more than the rule needs, so the check in scan_approvals stays the
# one rule (a card that just expired is read and skipped there).
APPROVAL_SCAN_SLACK = timedelta(minutes=5)
# The rfqs readiness read: the two ids the bid event needs and the embedded
# quote ids, which only test whether a quote exists (no amount, no vendor).
RFQ_READINESS_COLUMNS = "project_id, material_category_id, quotes(id)"
TODO_OVERDUE_DAYS = 7
_IDS_CHUNK = 200

# ── Purge (5.1) ──────────────────────────────────────────────────────────────
PURGE_AFTER = time(2, 0)
PURGE_LIMIT = 200
PURGE_ROUNDS = 10

# Project states that retire router rows (6.4, R24). Failed to bid and
# abandon both set abandoned_at; bid.due_not_ready also retires on submitted.
CLOSED_STAGES = ("declined", "bid_outcome", "pm_only", "cp_only")
BID_CLOSED_STAGES = (*CLOSED_STAGES, "submitted")
_PROJECT_COLUMNS = "id, number, internal_bid_at, current_stage, abandoned_at, failed_to_bid_at"
_PAGE = 1000


class _Unroutable(Exception):
    """An event the router finishes `failed` instead of retrying (an unknown
    kind, a malformed payload). `kind` is the error kind stored."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


# ── Small helpers ────────────────────────────────────────────────────────────


def _utc(value: Any) -> datetime | None:
    """An aware UTC datetime from a datetime or an ISO string, else None."""
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


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _pt_day(value: datetime) -> date:
    return value.astimezone(PT).date()


def _rows(resp: Any) -> list[dict]:
    return [r for r in (getattr(resp, "data", None) or []) if isinstance(r, dict)]


def _page(build) -> list[dict]:
    """Drain an ordered query past PostgREST's max-rows cap; `build(lo, hi)`
    returns a fresh builder for that inclusive range."""
    out: list[dict] = []
    lo = 0
    while True:
        rows = _rows(build(lo, lo + _PAGE - 1).execute())
        out.extend(rows)
        if len(rows) < _PAGE:
            return out
        lo += _PAGE


def _chunks(values: list[str], size: int = _IDS_CHUNK) -> list[list[str]]:
    return [values[i:i + size] for i in range(0, len(values), size)]


def _retry_seconds(attempts: Any) -> int:
    n = max(1, int(attempts or 1))
    return RETRY_SECONDS[min(n, len(RETRY_SECONDS)) - 1]


def _slot_times(settings: Any) -> dict[str, str]:
    raw = [p.strip() for p in str(getattr(settings, "assistant_proactive_digest_times", "") or "").split(",")]
    raw = [p for p in raw if p]
    if len(raw) != 2:
        return {"am": "07:00", "pm": "13:00"}
    am, pm = sorted(raw, key=lambda p: tuple(int(x) for x in p.split(":")))
    return {"am": am, "pm": pm}


def _allowlist(settings: Any) -> frozenset[str]:
    raw = str(getattr(settings, "assistant_proactive_user_allowlist", "") or "")
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())


def _switch_on(settings_row: Mapping[str, Any] | None) -> bool:
    """The company switches (6.1): assistant_settings.enabled and
    proactive_enabled. A missing row fails closed."""
    return bool(settings_row and settings_row.get("enabled") and settings_row.get("proactive_enabled"))


def _env_on(settings: Any) -> bool:
    return bool(getattr(settings, "assistant_enabled", False)
                and getattr(settings, "assistant_proactive_enabled", False))


def person_eligible(person: Mapping[str, Any] | None, *, settings: Any, settings_row: Mapping[str, Any] | None,
                    assistant_profile: Any) -> bool:
    """The router's person test (6.2 step 4), shared with the phase 8 triage
    service (plan 5.3 step 1) so it is not copied: an active profile with an
    internal role and a working assistant (`access.assistant_enabled_for`
    over the company row and the person's assistant_profiles row, a missing
    row counting as on). No delegation test (D7; phase 8 P15) and no per-kind
    sub-app step (the caller adds that when it has a kind). Pure over its
    arguments; the caller reads the rows. `assistant_profile` is the row (or
    None), or a no-argument callable that returns it, called only after the
    role and active checks pass so an ineligible person costs no read."""
    if not person:
        return False
    try:
        role = Role(person.get("role"))
    except ValueError:
        return False
    if role not in INTERNAL_ROLES or not person.get("is_active"):
        return False
    if callable(assistant_profile):
        assistant_profile = assistant_profile()
    user = SimpleNamespace(is_active=True, role=role)
    env = bool(getattr(settings, "assistant_enabled", False))
    return bool(access.assistant_enabled_for(user, settings_row, assistant_profile, env=env))


def _card_sub_app(action_kind: str | None) -> str | None:
    """The sub-app of the tool an approval card names (the registry spec, as
    approvals_job checks it), or None when the tool has none or is gone."""
    if not action_kind:
        return None
    from app.assistant import registry

    spec = registry.get(str(action_kind))
    return getattr(spec, "sub_app", None) if spec is not None else None


# ── Per-tick context (reads cached for one tick only, 6.5) ──────────────────


class _Context:
    """The settings, the company row and the per-tick caches the router
    reads (6.2 step 4: one profiles read, one settings read). Nothing here
    outlives the tick."""

    def __init__(self, now: datetime, *, settings: Any = None, settings_row: Mapping[str, Any] | None = None,
                 sb: Any = None):
        self.now = now
        self.settings = settings if settings is not None else get_settings()
        self.sb = sb if sb is not None else supabase_client.get_supabase()
        self.settings_row = dict(settings_row) if settings_row is not None else repo.settings_row(None)
        s = self.settings
        self.slot_times = _slot_times(s)
        self.quiet_start = str(getattr(s, "assistant_proactive_quiet_start", "") or "18:00")
        self.quiet_end = str(getattr(s, "assistant_proactive_quiet_end", "") or "06:00")
        self.catchup = int(getattr(s, "assistant_proactive_digest_catchup_hours", 3) or 3)
        self.allow = _allowlist(s)
        self.external = bool(getattr(s, "assistant_proactive_external_enabled", False))
        self.holidays = list((self.settings_row or {}).get("holidays") or [])
        self._profiles: dict[str, dict] | None = None
        self._assistant_profiles: dict[str, dict | None] = {}
        self._role_rows: dict[str, dict[str, dict]] = {}
        self._user_rows: dict[tuple[str, str], dict | None] = {}
        self._projects: dict[str, dict | None] = {}
        self._mutes: dict[str, frozenset[str]] = {}
        self._teams: dict[str, Any] | None = None
        self._bells: dict[str, dict | None] = {}
        self._cards: dict[tuple[str, str], dict | None] = {}
        self._todos: dict[str, dict | None] = {}
        self._sub_apps: dict[str, str | None] = {}
        self._reminders: dict[str, list[dict]] = {}
        self._prefs: dict[str, Any] = {}
        self._auto_send: dict[str, Any] = {}
        self._auto_send_row: dict | None | bool = False
        self._call_in: dict[str, bool] = {}
        self._parts: dict[str, dict] = {}

    @property
    def switch_on(self) -> bool:
        return _switch_on(self.settings_row)

    # people

    def profiles(self) -> dict[str, dict]:
        if self._profiles is None:
            rows = _page(lambda lo, hi: self.sb.table("profiles").select("id, role, is_active, email")
                         .order("id").range(lo, hi))
            self._profiles = {str(r["id"]): r for r in rows if r.get("id")}
        return self._profiles

    def person(self, user_id: Any) -> dict | None:
        return self.profiles().get(str(user_id)) if user_id else None

    def audience(self, event_type: str) -> list[dict]:
        """Active profiles whose role is in the kind's audience: for a router
        kind the code defaults plus admin role rows switched on, minus admin
        rows switched off; for a record kind its fixed audience (the 0177
        event audience for bells, which the router never widens)."""
        entry = proactive.EVENT_TYPES[event_type]
        roles = {r.value for r in entry["audience"]}
        if entry["kind"] == "router":
            for role, row in self.role_rows(event_type).items():
                if row.get("enabled"):
                    roles.add(role)
                else:
                    roles.discard(role)
        return [p for p in self.profiles().values() if p.get("is_active") and p.get("role") in roles]

    def allowed(self, person: Mapping[str, Any]) -> bool:
        """ASSISTANT_PROACTIVE_USER_ALLOWLIST (6.2 step 3): ids or emails."""
        if not self.allow:
            return True
        return (str(person.get("id") or "").lower() in self.allow
                or str(person.get("email") or "").strip().lower() in self.allow)

    def assistant_profile(self, user_id: str) -> dict | None:
        if user_id not in self._assistant_profiles:
            self._assistant_profiles[user_id] = repo.profile_row(user_id)
        return self._assistant_profiles[user_id]

    def sub_app(self, action_kind: str | None) -> str | None:
        key = str(action_kind or "")
        if key not in self._sub_apps:
            try:
                self._sub_apps[key] = _card_sub_app(key)
            except Exception:  # noqa: BLE001 - a registry problem never blocks routing
                logger.warning("assistant proactive: no sub-app for an approval card", exc_info=True)
                self._sub_apps[key] = None
        return self._sub_apps[key]

    def eligible(self, person: Mapping[str, Any], event_type: str, payload: Mapping[str, Any]) -> bool:
        """6.2 step 4: an active internal person with a working assistant (a
        missing assistant_profiles row counts as on) and the kind's sub-app on
        for the role. No delegation test (D7). The person test is
        `person_eligible`, shared with the phase 8 triage service."""
        if not person_eligible(person, settings=self.settings, settings_row=self.settings_row,
                               assistant_profile=lambda: self.assistant_profile(str(person["id"]))):
            return False
        role = Role(person.get("role"))
        sub_app = proactive.EVENT_TYPES[event_type]["sub_app"]
        if sub_app == "card":
            sub_app = self.sub_app(payload.get("action_kind"))
        if sub_app in (None, "none"):
            return True
        return bool(features.enabled_map(role).get(sub_app, False))

    # subscriptions and mutes

    def role_rows(self, event_type: str) -> dict[str, dict]:
        if event_type not in self._role_rows:
            rows = repo.role_subscriptions(None, event_type=event_type)
            self._role_rows[event_type] = {str(r["role"]): r for r in rows if r.get("role")}
        return self._role_rows[event_type]

    def user_row(self, user_id: str, event_type: str) -> dict | None:
        key = (user_id, event_type)
        if key not in self._user_rows:
            rows = repo.user_subscriptions(user_id, event_type=event_type)
            self._user_rows[key] = rows[0] if rows else None
        return self._user_rows[key]

    def subscription(self, person: Mapping[str, Any], event_type: str) -> dict[str, Any]:
        """User row over admin role row over code default (6.2 step 5)."""
        role = str(person.get("role") or "")
        return proactive.effective_subscription(
            event_type, role, self.user_row(str(person["id"]), event_type), self.role_rows(event_type).get(role),
        )

    def muted(self, project_id: Any) -> frozenset[str]:
        if not project_id:
            return frozenset()
        key = str(project_id)
        if key not in self._mutes:
            self._mutes[key] = frozenset(repo.project_muted_user_ids(None, key))
        return self._mutes[key]

    def teams_on(self, user_id: str) -> bool:
        """The person's Teams switch (R23): `teams_alert_users.enabled` false
        means off; a missing row means on (owner question 35)."""
        if self._teams is None:
            rows = _page(lambda lo, hi: self.sb.table("teams_alert_users").select("user_id, enabled")
                         .order("user_id").range(lo, hi))
            self._teams = {str(r["user_id"]): r.get("enabled") for r in rows if r.get("user_id")}
        return self._teams.get(str(user_id)) is not False

    def digest_email_on(self, user_id: str) -> bool:
        """The person's assistant digest email switch (phase 8, plan 9 step
        2): `assistant_profiles.digest_email_enabled`, read from the cached
        assistant profile; a missing row means on."""
        return assistant_delivery.digest_email_on(self.assistant_profile(str(user_id)))

    def triage_withdrawn(self, payload: Mapping[str, Any] | None) -> bool:
        """Phase 8 (plan 5.6, P8R-3): a triage escalation is withdrawn while
        the company triage switch is off or the fuse latch is set, or when it
        was emitted before the last switch-on. Read from this tick's company
        row; the Teams send applies the same test at send time."""
        return assistant_delivery.escalation_withdrawn(self.settings_row, dict(payload or {}))

    # domain rows

    def project(self, project_id: Any) -> dict | None:
        if not project_id:
            return None
        key = str(project_id)
        if key not in self._projects:
            rows = _rows(self.sb.table("projects").select(_PROJECT_COLUMNS).eq("id", key).limit(1).execute())
            self._projects[key] = rows[0] if rows else None
        return self._projects[key]

    def prefetch_bells(self, events: Iterable[Mapping[str, Any]]) -> None:
        wanted = [str((e.get("payload") or {}).get("notification_id")) for e in events
                  if str(e.get("event_type") or "").startswith("bell.")
                  and (e.get("payload") or {}).get("notification_id")]
        wanted = [w for w in dict.fromkeys(wanted) if w not in self._bells]
        for chunk in _chunks(wanted):
            for row in _rows(self.sb.table("notifications")
                             .select("id, user_id, rfq_id, type, metadata, created_at, read_at, dismissed_at")
                             .in_("id", chunk).execute()):
                self._bells[str(row["id"])] = row
        for w in wanted:
            self._bells.setdefault(w, None)

    def bell(self, notification_id: Any) -> dict | None:
        key = str(notification_id or "")
        if key and key not in self._bells:
            self.prefetch_bells([{"event_type": "bell.x", "payload": {"notification_id": key}}])
        return self._bells.get(key)

    def card(self, user_id: str, approval_id: Any) -> dict | None:
        key = (user_id, str(approval_id or ""))
        if key not in self._cards:
            self._cards[key] = repo.get_approval(user_id, key[1]) if key[1] else None
        return self._cards[key]

    def todo(self, todo_id: Any) -> dict | None:
        key = str(todo_id or "")
        if key and key not in self._todos:
            rows = _rows(self.sb.table("todos").select("id, user_id, is_done, due_date, due_at, last_nudged_at")
                         .eq("id", key).limit(1).execute())
            self._todos[key] = rows[0] if rows else None
        return self._todos.get(key)

    # precedence facts (section 7)

    def reminders(self, project_id: str, since: datetime) -> list[dict]:
        """due_reminder_log rows for the project since `since` (R4, R4b)."""
        if project_id not in self._reminders:
            self._reminders[project_id] = _page(
                lambda lo, hi: self.sb.table("due_reminder_log")
                .select("user_id, kind, offset_key, due_at_snapshot, created_at")
                .eq("project_id", project_id).gte("created_at", _iso(since))
                .order("created_at").range(lo, hi)
            )
        return self._reminders[project_id]

    def reminder_1h_expected(self, person: Mapping[str, Any]) -> bool:
        """True only when effective_prefs gives this person the internal_bid
        reminder at offset 1h (R4)."""
        uid = str(person["id"])
        if uid not in self._prefs:
            rows = _rows(self.sb.table("notification_prefs").select("user_id, prefs").eq("user_id", uid)
                         .limit(1).execute())
            try:
                eff = effective_prefs(Role(person.get("role")), rows[0].get("prefs") if rows else None)
                self._prefs[uid] = bool(eff.internal_bid.enabled and "1h" in eff.internal_bid.offsets)
            except ValueError:
                self._prefs[uid] = False
        return self._prefs[uid]

    def auto_send_note(self, project_id: str) -> dict | None:
        """The R10 fact: {enabled, silent_reason, send_time} when auto send is
        on, else None. Read only; any failure means no note."""
        if self._auto_send_row is False:
            rows = _rows(self.sb.table("auto_send_settings").select("id, enabled").eq("id", 1).limit(1).execute())
            self._auto_send_row = rows[0] if rows else None
        row = self._auto_send_row
        if not isinstance(row, dict) or not row.get("enabled"):
            return None
        if project_id not in self._auto_send:
            try:
                from app.services import auto_send, external_submission

                project = auto_send._project(self.sb, project_id)
                state, _head = external_submission._state_and_send_out_head(self.sb, project_id)
                silent = auto_send._silent_reason(self.sb, project or {}, state, auto_send._runs(self.sb, project_id))
                self._auto_send[project_id] = {
                    "enabled": True, "silent_reason": silent if project else "missing",
                    "send_time": str(getattr(self.settings, "auto_send_send_time", "17:05") or "17:05"),
                }
            except Exception:  # noqa: BLE001 - a note is optional; never block the line
                logger.warning("assistant proactive: auto send state unreadable", exc_info=True)
                self._auto_send[project_id] = None
        return self._auto_send[project_id]

    def parts_payload(self, project: Mapping[str, Any]) -> dict[str, Any]:
        """A `bid.due_not_ready`-shaped parts payload for one project, read
        live (due_digest.readiness_rows plus missing_quote_categories), for
        the phase 8 escalation clause "your part is still missing" (plan 5.6,
        19.1 FQ5). Amounts are read only to compute booleans; an unreadable
        project gives an empty payload, so no part is claimed missing."""
        pid = str(project["id"])
        if pid not in self._parts:
            checks = due_digest.readiness_rows(self.sb, [dict(project)])
            if not checks:
                self._parts[pid] = {}
            else:
                flags = checks[0].get("checks") or {}
                missing = {} if flags.get("has_quotes") else missing_quote_categories(self.sb, [pid])
                self._parts[pid] = {
                    "missing_quote_category_ids": list(missing.get(pid, [])),
                    "labor_missing": not flags.get("has_labor"),
                    "markups_missing": not flags.get("has_markups"),
                }
        return self._parts[pid]

    def call_in_open(self, project_id: str) -> bool:
        """R9: the project sits on the open pre-bid call list."""
        if project_id not in self._call_in:
            rows = _rows(self.sb.table("call_in_entries").select("id, round").eq("project_id", project_id)
                         .eq("round", "pre_bid").is_("closed_at", "null").limit(1).execute())
            self._call_in[project_id] = bool(rows)
        return self._call_in[project_id]

    # rechecks (6.4)

    def project_closed(self, project: Mapping[str, Any] | None, event_type: str) -> bool:
        if project is None:
            return False
        stages = BID_CLOSED_STAGES if event_type == proactive.BID_DUE else CLOSED_STAGES
        return bool(project.get("abandoned_at") or project.get("failed_to_bid_at")
                    or project.get("current_stage") in stages)

    def stale(self, user_id: str, event_type: str, payload: Mapping[str, Any],
              project: Mapping[str, Any] | None, now: datetime) -> bool:
        """A router row whose fact no longer holds (6.4): a due line past its
        deadline or for a moved date, a card no longer pending, a to-do done
        or deleted. (Guiding-principle call for the to-do: a reminder about a
        finished to-do repeats nothing the person needs.)"""
        if event_type == proactive.BID_DUE:
            due = _utc(payload.get("internal_bid_at"))
            current = _utc((project or {}).get("internal_bid_at"))
            return due is None or due <= now or current is None or current != due
        if event_type == proactive.APPROVAL_EXPIRING:
            card = self.card(user_id, payload.get("approval_id"))
            expires = _utc((card or {}).get("expires_at"))
            return not card or card.get("status") != "pending" or expires is None or expires <= now
        if event_type == proactive.TODO_DUE:
            todo = self.todo(payload.get("todo_id"))
            return not todo or bool(todo.get("is_done")) or str(todo.get("user_id")) != user_id
        return False

    def recheck(self, row: Mapping[str, Any], now: datetime) -> str | None:
        """The delivery-time recheck (6.4) of one undelivered row: the
        dismiss reason, or None when it may go. Eligibility and subscription
        for the person's current role (superseded), a project mute (muted), a
        closed project (project_closed), a stale fact (superseded). A phase 8
        triage escalation is also superseded when withdrawn (plan 5.6); its
        bell being read or the bid moving meanwhile does not drop it."""
        user_id = str(row["user_id"])
        event_type = str(row.get("event_type") or "")
        payload = row.get("payload") or {}
        if event_type not in proactive.EVENT_TYPES:
            return None
        # Phase 8: a held or digest-bound escalation is not released after a
        # fuse trip or a switch-off or flip of company triage (19.1 FQ9).
        if event_type == proactive.TRIAGE_ESCALATION and self.triage_withdrawn(payload):
            return "superseded"
        person = self.person(user_id)
        if person is None or not self.allowed(person) or not self.eligible(person, event_type, payload):
            return "superseded"
        if not self.subscription(person, event_type)["enabled"]:
            return "superseded"
        project_id = row.get("project_id")
        if project_id and user_id in self.muted(project_id):
            return "muted"
        if event_type in proactive.ROUTER_KINDS:
            project = self.project(project_id)
            if self.project_closed(project, event_type):
                return "project_closed"
            if self.stale(user_id, event_type, payload, project, now):
                return "superseded"
        return None


# ── Tails (4.1) ──────────────────────────────────────────────────────────────


def _tail_query(ctx: _Context, spec: Mapping[str, Any]):
    query = ctx.sb.table(spec["table"]).select(spec["columns"])
    if spec["action"]:
        query = query.eq("action", spec["action"])
    return query


def _tail_rows(ctx: _Context, spec: Mapping[str, Any], cursor_at: Any, cursor_id: str | None,
               ub: datetime) -> list[dict]:
    """One keyset page in (clock, id) order, at most TAIL_PAGE rows.

    A cursor without a tie-break id (a short page or a reseed placed it)
    reads from `cursor_at - 2 minutes`, the overlap for transactions that
    commit late. A cursor with an id (a full page placed it on the last row
    read) continues strictly after (cursor_at, cursor_id), so a page full of
    rows inside the overlap always moves forward and never livelocks; the
    next short page puts the overlap back."""
    clock = spec["clock"]
    if cursor_id is None:
        lo = (_utc(cursor_at) or ub) - TAIL_OVERLAP
        return _rows(_tail_query(ctx, spec).gte(clock, _iso(lo)).lte(clock, _iso(ub))
                     .order(clock).order("id").limit(TAIL_PAGE).execute())
    stamp = cursor_at if isinstance(cursor_at, str) else _iso(cursor_at)
    rows = _rows(_tail_query(ctx, spec).eq(clock, stamp).gt("id", str(cursor_id))
                 .order("id").limit(TAIL_PAGE).execute())
    if len(rows) < TAIL_PAGE:
        rows += _rows(_tail_query(ctx, spec).gt(clock, stamp).lte(clock, _iso(ub))
                      .order(clock).order("id").limit(TAIL_PAGE - len(rows)).execute())
    return rows


def _bell_event(row: Mapping[str, Any]) -> dict | None:
    """A bell record event (4.2), or None for a bell the router never
    mirrors (P19). The payload holds ids, enums and the email_key hash only;
    never `message` and never a raw Graph or internet message id."""
    ntype = str(row.get("type") or "")
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    routing_event = notification_routing.event_for(ntype, metadata)
    if not proactive.bell_recordable(ntype, routing_event):
        return None
    event_type = proactive.bell_event_type(routing_event)
    raw = {
        "notification_id": row.get("id"), "notification_type": ntype, "routing_event": routing_event,
        "rfq_id": row.get("rfq_id"),
        **{k: metadata[k] for k in BELL_METADATA_KEYS if k in metadata},
        "email_key": proactive.email_key(metadata),
    }
    payload, dropped = proactive.clean_payload_report(event_type, raw)
    if dropped:
        logger.debug("assistant proactive: bell payload dropped %s", sorted(dropped))
    return {
        "event_type": event_type,
        "dedupe_key": proactive.event_dedupe_key(event_type, payload),
        "project_id": row.get("project_id"),
        "subject_user_id": row.get("user_id"),
        "payload": payload,
        "occurred_at": row.get("created_at"),
        "source": "bell_tail",
    }


def _audit_event(row: Mapping[str, Any]) -> dict | None:
    """A failed-to-bid record event: the audit id and the reason enum only;
    the typed note is free text and stays behind."""
    body = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    payload = proactive.clean_payload(proactive.FAILED_TO_BID, {"audit_id": row.get("id"), "reason": body.get("reason")})
    if not payload.get("audit_id"):
        return None
    project_id = row.get("entity_id") if row.get("entity") == "project" else None
    return {
        "event_type": proactive.FAILED_TO_BID,
        "dedupe_key": proactive.event_dedupe_key(proactive.FAILED_TO_BID, payload),
        "project_id": project_id,
        "subject_user_id": None,
        "payload": payload,
        "occurred_at": row.get("created_at"),
        "source": "audit_tail",
    }


def run_tail(name: str, now: datetime, *, worker: str, ctx: _Context | None = None) -> dict[str, Any]:
    """One keyset pass of a table tail, `notifications` or `audit_log` (4.1):
    read the cursor, select past `(cursor - 2 minutes)` up to `now` in
    `(clock, id)` order (limit 500), insert events ignoring duplicates, then a
    fenced cursor advance: a full page moves it to the last row read, a short
    page (empty included) to `now - 2 minutes` with no tie-break id. The lag
    guard runs once per tick before the tails (`poll_once`)."""
    if name not in TAILS:
        raise ValueError(f"unknown tail: {name}")
    spec = TAILS[name]
    ctx = ctx or _Context(now)
    cursor = next((c for c in repo.router_cursors(None) if c.get("name") == name), None)
    if cursor is None:
        return {"read": 0, "events": 0, "advanced": False, "missing_cursor": True}
    rows = _tail_rows(ctx, spec, cursor["cursor_at"], cursor.get("cursor_id"), now)
    build = _bell_event if name == "notifications" else _audit_event
    events: list[dict] = []
    for row in rows:
        try:
            made = build(row)
        except (ValueError, KeyError):
            logger.warning("assistant proactive: tail %s row skipped (malformed)", name)
            continue
        if made is not None:
            events.append(made)
    inserted = repo.insert_events(None, events) if events else []
    if len(rows) >= TAIL_PAGE:
        last = rows[-1]
        new_at, new_id = _utc(last[spec["clock"]]) or now, str(last["id"])
    else:
        new_at, new_id = min(now - TAIL_OVERLAP, now), None
    advanced = repo.advance_router_cursor(
        None, name, expect_at=cursor["cursor_at"], expect_id=cursor.get("cursor_id"),
        new_at=new_at, new_id=new_id, updated_by=worker, now=now,
    )
    return {"read": len(rows), "events": len(inserted), "advanced": bool(advanced)}


def _count_range(ctx: _Context, spec: Mapping[str, Any], after: datetime, upto: datetime) -> int:
    """count(*) of tail rows with clock in (after, upto]: the rows a reseed
    skips. One head request (the count header); a client without it counts
    the returned rows."""
    clock = spec["clock"]
    query = ctx.sb.table(spec["table"]).select("id", count="exact", head=True)
    if spec["action"]:
        query = query.eq("action", spec["action"])
    resp = query.gt(clock, _iso(after)).lte(clock, _iso(upto)).execute()
    count = getattr(resp, "count", None)
    return int(count) if isinstance(count, int) else len(_rows(resp))


def reseed_tails(now: datetime, *, worker: str, names: Iterable[str] | None = None, reason: str = "lag",
                 ctx: _Context | None = None) -> dict[str, int]:
    """Reseed the named tail cursors (both by default) to `now - 2 minutes`
    with no tie-break id (4.1): the lag guard at tick start, and the company
    switch flip from off to on (the admin route may call this instead of the
    bare repo reseed so the skipped rows are counted). Logs `gap_skipped`
    with the count of rows actually skipped, only when it is above zero.
    Returns {cursor name: rows skipped}."""
    ctx = ctx or _Context(now)
    target = now - TAIL_OVERLAP
    chosen = list(names or TAILS)
    cursors = {c["name"]: c for c in repo.router_cursors(None) if c.get("name") in chosen}
    out: dict[str, int] = {}
    for name in chosen:
        cursor = cursors.get(name)
        at = _utc(cursor.get("cursor_at")) if cursor else None
        skipped = 0
        if at is not None and at < target:
            try:
                skipped = _count_range(ctx, TAILS[name], at, target)
            except Exception:  # noqa: BLE001 - the count is a log line; the reseed still runs
                logger.warning("assistant proactive: gap count failed for %s", name, exc_info=True)
        repo.reseed_router_cursors(None, to_at=target, updated_by=worker, names=[name], now=now)
        if skipped > 0:
            logger.warning("assistant proactive gap_skipped tail=%s rows=%d reason=%s", name, skipped, reason)
        out[name] = skipped
    return out


def _lag_guard(now: datetime, worker: str, ctx: _Context) -> dict[str, int]:
    """A cursor older than ASSISTANT_PROACTIVE_CURSOR_MAX_LAG_MINUTES is
    reseeded before the tails read (4.1): a long outage or the first start
    after a release replays nothing."""
    max_lag = timedelta(minutes=int(getattr(ctx.settings, "assistant_proactive_cursor_max_lag_minutes", 60) or 60))
    lagging = [c["name"] for c in repo.router_cursors(None)
               if c.get("name") in TAILS and (_utc(c.get("cursor_at")) or now) < now - max_lag]
    return reseed_tails(now, worker=worker, names=lagging, reason="lag", ctx=ctx) if lagging else {}


# ── Sweeper: approval scan (every tick) and slot events (5.2, 5.3) ──────────


def scan_approvals(now: datetime, *, ctx: _Context | None = None) -> int:
    """Pending assistant cards that expire within 60 minutes and lived at
    least 2 hours (only the 4 h cards): one `assistant.approval_expiring`
    event each, keyed on the card and its expires_at, so an extended card can
    warn again (5.4). Returns the events inserted."""
    ctx = ctx or _Context(now)
    events = []
    for card in repo.pending_approvals_due(None, limit=200, expires_after=now - APPROVAL_SCAN_SLACK):
        expires, created = _utc(card.get("expires_at")), _utc(card.get("created_at"))
        if expires is None or created is None or not card.get("user_id"):
            continue
        if not (now < expires <= now + APPROVAL_WARN_BEFORE) or expires - created < APPROVAL_MIN_LIFETIME:
            continue
        payload = proactive.clean_payload(proactive.APPROVAL_EXPIRING, {
            "approval_id": card.get("id"), "expires_at": _iso(expires), "action_kind": card.get("action_kind"),
        })
        try:
            key = proactive.event_dedupe_key(proactive.APPROVAL_EXPIRING, payload)
        except ValueError:
            continue
        events.append({
            "event_type": proactive.APPROVAL_EXPIRING, "dedupe_key": key, "project_id": card.get("project_id"),
            "subject_user_id": card.get("user_id"), "payload": payload, "occurred_at": now, "source": "sweeper",
        })
    return len(repo.insert_events(None, events)) if events else 0


def _missing_quote_categories(ctx: _Context, project_ids: list[str]) -> dict[str, list[str]]:
    """The router's call site: `missing_quote_categories` over the tick's
    client."""
    return missing_quote_categories(ctx.sb, project_ids)


def missing_quote_categories(sb: Any, project_ids: list[str]) -> dict[str, list[str]]:
    """Per project, the material category ids of RFQs with no vendor quote
    yet (ids only, no names). The embedded quote ids only test presence, as
    due_digest.readiness_rows does, and are dropped row by row: nothing from
    quotes leaves this function. A project with no RFQs is absent, so it has
    no quote part (5.3). Shared with the phase 8 triage service (plan 5.3,
    `your_part_missing`), so there is one copy."""
    out: dict[str, set[str]] = {}
    for chunk in _chunks([str(p) for p in project_ids]):
        rows = _page(lambda lo, hi, c=chunk: sb.table("rfqs").select(RFQ_READINESS_COLUMNS)
                     .in_("project_id", c).order("id").range(lo, hi))
        for r in rows:
            quoted = bool(r.get("quotes"))
            if not quoted and r.get("project_id") and r.get("material_category_id"):
                out.setdefault(str(r["project_id"]), set()).add(str(r["material_category_id"]))
    return {pid: sorted(ids) for pid, ids in out.items()}


def _bid_events(slot: str, day: date, now: datetime, ctx: _Context) -> list[dict]:
    """5.3: live bids due from the slot to the end of the next digest day
    whose deadline is still ahead, with at least one part missing. One event
    per project per slot; the router narrows to the roles whose part is
    missing. Amounts are read by readiness_rows only to compute booleans."""
    start, end = proactive.due_window(day, slot, ctx.holidays, slot_times=ctx.slot_times)
    projects = [p for p in due_digest.due_today_projects(ctx.sb, start.astimezone(UTC), end.astimezone(UTC))
                if (_utc(p.get("internal_bid_at")) or now) > now]
    if not projects:
        return []
    checks = due_digest.readiness_rows(ctx.sb, projects)
    missing = _missing_quote_categories(ctx, [str(p["id"]) for p in projects])
    events = []
    for item in checks:
        project, flags = item["project"], item["checks"]
        pid = str(project["id"])
        quote_ids = missing.get(pid, []) if not flags.get("has_quotes") else []
        payload = proactive.clean_payload(proactive.BID_DUE, {
            "internal_bid_at": _iso(_utc(project["internal_bid_at"])),  # type: ignore[arg-type]
            "slot": slot, "slot_date": day.isoformat(),
            "missing_quote_category_ids": quote_ids,
            "labor_missing": not flags.get("has_labor"),
            "markups_missing": not flags.get("has_markups"),
            "readiness": item.get("severity") if item.get("severity") in ("red", "yellow") else None,
        })
        if not proactive.parts_missing(payload):
            continue
        events.append({
            "event_type": proactive.BID_DUE,
            "dedupe_key": proactive.event_dedupe_key(proactive.BID_DUE, payload, pid),
            "project_id": pid, "subject_user_id": None, "payload": payload, "source": "sweeper",
        })
    return events


def _todo_events(day: date, ctx: _Context) -> list[dict]:
    """5.3 to-dos: not done, due today (the Pacific due_date, or due_at
    inside today) or overdue by at most 7 days; keyed on the due value, so a
    moved date re-arms (5.4)."""
    day_start = datetime.combine(day, time.min, tzinfo=PT)
    day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=PT)
    cols = "id, user_id, project_id, due_date, due_at, is_done"
    by_date = _page(lambda lo, hi: ctx.sb.table("todos").select(cols).eq("is_done", False)
                    .gte("due_date", (day - timedelta(days=TODO_OVERDUE_DAYS)).isoformat())
                    .lte("due_date", day.isoformat()).order("id").range(lo, hi))
    by_time = _page(lambda lo, hi: ctx.sb.table("todos").select(cols).eq("is_done", False)
                    .gte("due_at", _iso(day_start - timedelta(days=TODO_OVERDUE_DAYS)))
                    .lt("due_at", _iso(day_end)).order("id").range(lo, hi))
    seen: dict[str, dict] = {}
    for row in [*by_date, *by_time]:
        if row.get("id") and not row.get("is_done"):
            seen.setdefault(str(row["id"]), row)
    events = []
    for row in seen.values():
        due_at = _utc(row.get("due_at"))
        if due_at is not None:
            due_value, due_day = _iso(due_at), _pt_day(due_at)
        else:
            try:
                due_day = date.fromisoformat(str(row.get("due_date"))[:10])
            except ValueError:
                continue
            due_value = due_day.isoformat()
        if not (day - timedelta(days=TODO_OVERDUE_DAYS) <= due_day <= day) or not row.get("user_id"):
            continue
        payload = proactive.clean_payload(proactive.TODO_DUE, {
            "todo_id": row["id"], "due": due_value, "overdue": due_day < day,
        })
        try:
            key = proactive.event_dedupe_key(proactive.TODO_DUE, payload)
        except ValueError:
            continue
        events.append({
            "event_type": proactive.TODO_DUE, "dedupe_key": key, "project_id": row.get("project_id"),
            "subject_user_id": row["user_id"], "payload": payload, "source": "sweeper",
        })
    return events


def _slot_events(slot: str, day: date, slot_at: datetime, now: datetime, ctx: _Context) -> list[dict]:
    events = [*_bid_events(slot, day, now, ctx), *_todo_events(day, ctx)]
    for e in events:
        e["occurred_at"] = slot_at
    return events


def _settled(rows: Iterable[Mapping[str, Any]]) -> bool:
    return all(r.get("status") in ("processed", "skipped", "failed") for r in rows)


def run_slot_sweeper(slot: str, now: datetime, *, worker: str, ctx: _Context | None = None) -> dict[str, Any]:
    """The per-slot sweeper (5.2, 5.3): claim the slot ledger row, emit
    `bid.due_not_ready` and `todo.due` events already claimed, route them in
    the same tick and set `finished_at` once every slot event settled.

    The holder keeps the slot across ticks (a retried event keeps it open).
    A failed attempt (the emit raised or an event went to retry) bumps
    `result.failures` and records the exception class; after two, or once
    local time passes the slot instant plus 90 minutes, the slot finishes
    anyway with `result.error` so the digest goes out without its lines."""
    if slot not in proactive.SLOT_TIMES:
        raise ValueError(f"unknown digest slot {slot!r}")
    ctx = ctx or _Context(now)
    day = _pt_day(now)
    step = f"digest_{slot}"
    slot_at = proactive.slot_instant(day, slot, slot_times=ctx.slot_times).astimezone(UTC)
    held = repo.proactive_slot(None, step, day)
    if held is not None and held.get("finished_at") is not None:
        return {"slot": slot, "finished": True}
    if held is None or held.get("claimed_by") != worker:
        held = repo.claim_proactive_slot(None, step, day, worker, now)
        if held is None:
            return {"slot": slot, "claimed": False}
    result = dict(held.get("result") or {})
    counts: Counter = Counter()
    failed, error, settled = False, None, False
    try:
        events = _slot_events(slot, day, slot_at, now, ctx)
        keys = [e["dedupe_key"] for e in events]
        claimed_rows = [{**e, "status": "claimed", "claimed_by": worker, "claimed_at": now, "attempts": 1}
                        for e in events]
        counts["inserted"] += len(repo.insert_events(None, claimed_rows)) if claimed_rows else 0
        stored = repo.events_by_dedupe_keys(None, keys) if keys else []
        counts["events"] = len(stored)
        mine = [e for e in stored if e.get("status") == "claimed" and e.get("claimed_by") == worker]
        lease_floor = now - timedelta(seconds=EVENT_LEASE_SECONDS)
        reclaim = [str(e["id"]) for e in stored
                   if e.get("status") == "pending"
                   or (e.get("status") == "claimed" and e.get("claimed_by") != worker
                       and (_utc(e.get("claimed_at")) or now) < lease_floor)]
        for chunk in _chunks(reclaim):
            mine.extend(repo.events_claim(None, worker=worker, limit=len(chunk), lease_seconds=EVENT_LEASE_SECONDS,
                                          now=now, ids=chunk))
        ctx.prefetch_bells(mine)
        for event in mine:
            outcome = _route_and_finish(event, now, worker, ctx)
            counts[outcome] += 1
            if outcome in ("retry", "finish_error"):
                failed, error = True, error or "route_retry"
        settled = _settled(repo.events_by_dedupe_keys(None, keys)) if keys else True
    except Exception as exc:  # noqa: BLE001 - a failing sweeper never blocks the digest
        logger.warning("assistant proactive: slot %s sweep failed (%s)", step, exc.__class__.__name__)
        failed, error = True, exc.__class__.__name__
    if failed:
        result["failures"] = int(result.get("failures") or 0) + 1
        result["error"] = error
    for key in ("inserted", "processed", "retry", "failed"):
        if counts.get(key):
            result[key] = int(result.get(key) or 0) + int(counts[key])
    if "events" in counts:
        result["events"] = int(counts["events"])
    give_up = int(result.get("failures") or 0) >= SLOT_MAX_FAILURES or now >= slot_at + SLOT_GIVE_UP_AFTER
    if (settled and not failed) or give_up:
        done = repo.finish_proactive_slot(None, step, day, worker=worker, result=result, now=now)
        return {"slot": slot, "finished": bool(done), **dict(counts)}
    repo.record_proactive_slot(None, step, day, worker=worker, result=result)
    return {"slot": slot, "finished": False, **dict(counts)}


def _open_slots(now: datetime, ctx: _Context) -> list[tuple[str, date, datetime]]:
    """Digest slots open at `now`: a workday, from the slot instant to the end
    of its catch-up window (8.1)."""
    day = _pt_day(now)
    if not proactive.is_workday(day, ctx.holidays):
        return []
    out = []
    for slot in proactive.SLOT_ORDER:
        at = proactive.slot_instant(day, slot, slot_times=ctx.slot_times).astimezone(UTC)
        if at <= now < at + timedelta(hours=ctx.catchup):
            out.append((slot, day, at))
    return out


# ── Routing one event (6.2) ──────────────────────────────────────────────────


def _recipients(event: Mapping[str, Any], ctx: _Context) -> list[dict]:
    """6.2 step 2: the subject for one-person kinds and bells, else every
    active profile in the kind's audience. A subject who is gone yields
    nobody. A `subject_only` kind (the phase 8 triage escalation, owner
    question 7) never falls back to the audience: without a subject it
    reaches nobody."""
    subject = event.get("subject_user_id")
    if subject:
        person = ctx.person(subject)
        return [person] if person is not None else []
    if (proactive.EVENT_TYPES.get(str(event["event_type"])) or {}).get("subject_only"):
        return []
    return ctx.audience(str(event["event_type"]))


def _bid_facts(person: Mapping[str, Any], payload: Mapping[str, Any], project_id: str,
               slot_at: datetime | None, now: datetime, ctx: _Context) -> dict[str, Any]:
    """The R4, R4b, R6, R9 and R10 facts for one person and one due line."""
    uid = str(person["id"])
    facts: dict[str, Any] = {"reminder_1h_expected": ctx.reminder_1h_expected(person)}
    prev = None
    if slot_at is not None:
        prev = proactive.previous_slot(slot_at, ctx.holidays, slot_times=ctx.slot_times).astimezone(UTC)
        facts["prev_slot_at"] = prev
    since = min(x for x in (now - timedelta(minutes=60), prev) if x is not None)
    due = _utc(payload.get("internal_bid_at"))
    for log in ctx.reminders(project_id, since):
        if str(log.get("user_id")) != uid:
            continue
        created = _utc(log.get("created_at"))
        if created is None:
            continue
        kind, offset = log.get("kind"), log.get("offset_key")
        if kind == "internal_bid" and offset == "1h" and now - timedelta(minutes=60) <= created <= now:
            facts["reminder_1h_logged_at"] = max(created, facts.get("reminder_1h_logged_at") or created)
        if (kind in ("internal_bid", "due_from_vendors") and offset in ("1d", "2d") and prev is not None
                and created >= prev and (kind != "internal_bid" or _utc(log.get("due_at_snapshot")) == due)):
            facts["reminder_logged_at"] = max(created, facts.get("reminder_logged_at") or created)
    if prev is not None and slot_at is not None:
        prev_day = _pt_day(prev)
        prev_slot = next((s for s in proactive.SLOT_ORDER
                          if proactive.slot_instant(prev_day, s, slot_times=ctx.slot_times) == prev), None)
        if prev_slot is not None:
            try:
                key = proactive.event_dedupe_key(proactive.BID_DUE, {
                    **payload, "slot": prev_slot, "slot_date": prev_day.isoformat()}, project_id)
                before = repo.inbox_row_by_key(uid, key)
            except ValueError:
                before = None
            if before is not None:
                was = before.get("payload") or {}
                facts["missing_unchanged"] = (
                    sorted(was.get("missing_quote_category_ids") or []) == sorted(payload.get("missing_quote_category_ids") or [])
                    and bool(was.get("labor_missing")) == bool(payload.get("labor_missing"))
                    and bool(was.get("markups_missing")) == bool(payload.get("markups_missing"))
                )
    routines = []
    for sched in repo.list_schedules(uid, status="active"):
        delivered = _utc(sched.get("last_delivered_at"))
        routines.append({
            "recipe": sched.get("recipe"),
            "delivered_today": delivered is not None and _pt_day(delivered) == _pt_day(now),
            "next_run_at": sched.get("next_run_at"),
        })
    facts["routines"] = routines
    facts["auto_send"] = ctx.auto_send_note(project_id)
    facts["call_in_overlap"] = ctx.call_in_open(project_id)
    return facts


def _bell_key(event: Mapping[str, Any], payload: Mapping[str, Any], bell: Mapping[str, Any] | None) -> str:
    """The inbox key of a bell record row (4.2a): one row per vendor email
    for this person, or the 120 s fallback bucket, or the bell's own row.
    The live bell supplies the metadata (the conversation id never enters a
    payload); without it the payload's email_key still collapses."""
    nid = str(payload.get("notification_id") or "")
    routing_event = payload.get("routing_event")
    if bell is not None:
        return proactive.bell_inbox_key(
            nid, routing_event, bell.get("metadata") if isinstance(bell.get("metadata"), dict) else {},
            user_id=str(bell.get("user_id") or ""), rfq_id=bell.get("rfq_id"), created_at=bell.get("created_at"),
        )
    if payload.get("email_key"):
        return f"p7:bell:email:{payload['email_key']}"
    return f"p7:bell:{nid}"


def _later_slot_passed(payload: Mapping[str, Any], now: datetime, ctx: _Context) -> bool:
    """6.2 step 9 for a late `bid.due_not_ready` event (a failed or retried
    route, a worker outage): once a later slot instant has passed, that
    slot's sweeper owns the line, so the older event writes nothing and never
    supersedes the newer slot's row. Routing time only; the delivery recheck
    keeps a snoozed row."""
    try:
        at = proactive.slot_instant(date.fromisoformat(str(payload.get("slot_date"))[:10]), str(payload["slot"]),
                                    slot_times=ctx.slot_times)
        return proactive.next_slot(at, ctx.holidays, slot_times=ctx.slot_times) <= now
    except (KeyError, ValueError, TypeError):
        return False


def _escalation_facts(uid: str, role: str, payload: Mapping[str, Any], project: Mapping[str, Any] | None,
                      now: datetime, ctx: _Context) -> dict[str, Any]:
    """The template inputs of a phase 8 triage escalation for one person
    (plan 5.6, 19.1 FQ5; the facts listed in proactive's docstring), each
    computed here from live state and never taken from the payload: the
    project's live `internal_bid_at`; `role_owns_missing_part` over a freshly
    read parts payload, only for a `due_soon_part_missing` reason on a bid
    still ahead; and, for the unread reasons, the source bell's read state,
    found through the person's own record row named by `payload.inbox_id`
    (the payload carries no bell id). A fact that cannot be computed is left
    out, which gives the neutral wording; the escalation is delivered either
    way."""
    facts: dict[str, Any] = {}
    reason = payload.get("reason")
    due = _utc((project or {}).get("internal_bid_at"))
    if due is not None:
        facts["internal_bid_at"] = _iso(due)
    if reason == "due_soon_part_missing" and project and project.get("id") and due is not None and due > now:
        facts["role_owns_missing_part"] = proactive.role_owns_missing_part(role, ctx.parts_payload(project))
    if reason in ("unread_long", "due_soon_unread") and payload.get("inbox_id"):
        record = repo.inbox_row(uid, str(payload["inbox_id"]))
        nid = (record or {}).get("source_notification_id")
        state = repo.notification_states(uid, [nid]).get(str(nid)) if nid else None
        if state is not None:
            facts["source_bell_unread"] = state.get("read_at") is None and state.get("dismissed_at") is None
            bell = ctx.bell(nid)
            mine = bell is not None and str(bell.get("user_id") or "") == uid
            at = _utc((bell or {}).get("created_at")) if mine else _utc((record or {}).get("created_at"))
            if at is not None:
                facts["source_bell_at"] = _iso(at)
    return facts


def _refresh_escalation(row: Mapping[str, Any], now: datetime, ctx: _Context) -> Mapping[str, Any]:
    """The release-time re-render of a phase 8 triage escalation's title
    (plan 5.6: a factual clause appears only while the router's live facts
    make it true). A row held by quiet hours, folded into the AM digest or
    waiting for a digest goes out later than it was routed, so "waited
    unread", "your part is still missing" and "is due" are recomputed here
    from live state exactly as at route time; a clause that no longer holds
    falls back to the neutral wording, and a due time that passed meanwhile
    to the overdue one. A changed title is written back to the undelivered
    row, which the bell, the digest, its email and the Teams card all read.
    Any other kind is returned unchanged. A fact that cannot be read is left
    out (neutral wording); the escalation is delivered either way."""
    if row.get("event_type") != proactive.TRIAGE_ESCALATION:
        return row
    uid = str(row["user_id"])
    stored = row.get("payload") or {}
    payload = proactive.clean_payload(proactive.TRIAGE_ESCALATION, stored)
    project_id = str(row["project_id"]) if row.get("project_id") else None
    project = ctx.project(project_id)
    role = str((ctx.person(uid) or {}).get("role") or "")
    facts: dict[str, Any] = {
        "now": now, "holidays": ctx.holidays, "severity": row.get("severity"),
        "project_number": (project or {}).get("number") or stored.get("project_number"),
        "slot_times": ctx.slot_times, "quiet_start": ctx.quiet_start, "quiet_end": ctx.quiet_end,
    }
    try:
        facts.update(_escalation_facts(uid, role, payload, project, now, ctx))
    except Exception as exc:  # noqa: BLE001 - an unreadable fact means neutral wording
        logger.warning("assistant proactive: escalation facts unreadable (%s)", exc.__class__.__name__)
    try:
        title = proactive.render({"event_type": proactive.TRIAGE_ESCALATION, "payload": payload,
                                  "project_id": project_id}, role, facts)["title"]
    except (ValueError, KeyError):
        return row
    if not title or title == row.get("title"):
        return row
    repo.inbox_retitle(uid, str(row["id"]), title)
    return {**row, "title": title}


def route_event(event: dict[str, Any], now: datetime, *, ctx: _Context | None = None) -> dict[str, int]:
    """Route one claimed event (6.2): catalog, recipients, allowlist,
    eligibility, subscriptions, precedence, render, inbox upsert and delivery.
    Returns the counts-only `routed` dict that `assistant_event_finish` stores.
    Raises _Unroutable for an unknown kind or a malformed payload (finished
    `failed`) and lets any other exception through (finished `retry`)."""
    ctx = ctx or _Context(now)
    event_type = str(event.get("event_type") or "")
    entry = proactive.EVENT_TYPES.get(event_type)
    if entry is None:
        raise _Unroutable("unknown_type")
    payload = proactive.clean_payload(event_type, event.get("payload"))
    if event_type == proactive.TRIAGE_ESCALATION and ctx.triage_withdrawn(payload):
        # Phase 8 kill switch (plan 5.6, P8R-3): company triage off, the fuse
        # latched, or emitted before the last switch-on. Nobody is reached.
        return {"triage_off": 1}
    project_id = str(event["project_id"]) if event.get("project_id") else None
    project = ctx.project(project_id)
    is_bell = event_type.startswith("bell.")
    bell = ctx.bell(payload.get("notification_id")) if is_bell else None
    routed: Counter = Counter()
    for person in _recipients(event, ctx):
        uid = str(person["id"])
        role = str(person.get("role") or "")
        routed["recipients"] += 1
        if not ctx.allowed(person):
            routed["allowlist"] += 1
            continue
        if not ctx.eligible(person, event_type, payload):
            routed["ineligible"] += 1
            continue
        sub = ctx.subscription(person, event_type)
        if sub["source"] == "none":
            routed["audience"] += 1
            continue
        if not sub["enabled"]:
            routed["muted"] += 1
            continue
        if project_id and uid in ctx.muted(project_id):
            routed["project_muted"] += 1
            continue
        if event_type == proactive.BID_DUE and not proactive.role_owns_missing_part(role, payload):
            routed["not_owner"] += 1
            continue
        if (event_type in (proactive.BID_DUE, proactive.TODO_DUE)
                and not ctx.project_closed(project, event_type)
                and (ctx.stale(uid, event_type, payload, project, now)
                     or (event_type == proactive.BID_DUE and _later_slot_passed(payload, now, ctx)))):
            routed["superseded"] += 1
            continue
        facts: dict[str, Any] = {
            "now": now, "holidays": ctx.holidays, "severity": sub["severity"],
            "project_number": (project or {}).get("number"), "teams_enabled": ctx.teams_on(uid),
            "subscription_enabled": True, "project_muted": False,
            "project_closed": ctx.project_closed(project, event_type) if project_id else False,
            "slot_times": ctx.slot_times, "quiet_start": ctx.quiet_start, "quiet_end": ctx.quiet_end,
        }
        if event_type == proactive.TRIAGE_ESCALATION:
            # The model's hint is the severity; only the person's own
            # `feedback_auto` row lowers it, inside render (P8R-4, F6). A
            # role default, an admin row or a manual user severity never caps it.
            facts["severity"] = payload.get("severity_hint")
            facts["subscription_source"] = sub["source"]
            facts["subscription_severity"] = sub["severity"]
            facts.update(_escalation_facts(uid, role, payload, project, now, ctx))
        elif event_type == proactive.APPROVAL_EXPIRING:
            card = ctx.card(uid, payload.get("approval_id"))
            expires = _utc((card or {}).get("expires_at"))
            facts["card_pending"] = bool(card and card.get("status") == "pending" and expires and expires > now)
        elif event_type == proactive.TODO_DUE:
            todo = ctx.todo(payload.get("todo_id")) or {}
            nudged = _utc(todo.get("last_nudged_at"))
            facts["nudged_today"] = nudged is not None and _pt_day(nudged) == _pt_day(now)
            facts["slot_at"] = _utc(event.get("occurred_at"))
        elif event_type == proactive.BID_DUE and project_id:
            slot_day = payload.get("slot_date")
            slot_at = None
            if slot_day and payload.get("slot") in proactive.SLOT_TIMES:
                slot_at = proactive.slot_instant(date.fromisoformat(str(slot_day)[:10]), payload["slot"],
                                                 slot_times=ctx.slot_times).astimezone(UTC)
            facts["slot_at"] = slot_at
            facts.update(_bid_facts(person, payload, project_id, slot_at, now, ctx))
        inbox_key = None
        if is_bell:
            facts["bell_type"] = payload.get("notification_type")
            inbox_key = _bell_key(event, payload, bell)
            if inbox_key != f"p7:bell:{payload.get('notification_id')}":
                facts["bucket_key"] = inbox_key
        try:
            out = proactive.render({"event_type": event_type, "payload": payload, "project_id": project_id},
                                   role, facts)
        except (ValueError, KeyError) as exc:
            raise _Unroutable("malformed") from exc
        if out["delivery"] is None:
            routed[str(out.get("reason") or out["outcome"])] += 1
            routed["suppressed"] += 1
            continue
        key = inbox_key or out["dedupe_key"]
        row = {
            "dedupe_key": key, "event_id": event.get("id"), "severity": out["severity"], "title": out["title"],
            "why": out["why"], "payload": out["payload"], "project_id": project_id, "event_type": event_type,
            "delivery": out["delivery"], "precedence": out["precedence"], "deliver_after": out["deliver_after"],
            "created_at": now,
        }
        routed[out["delivery"]] += 1
        if is_bell and key != f"p7:bell:{payload.get('notification_id')}":
            # A11.11 has no severity or precedence argument: it reads both from
            # the payload (0191) into their columns and keeps them out of the
            # stored payload.
            linked = repo.inbox_link_bell(
                uid, key, notification_id=str(payload["notification_id"]),
                bell_at=(bell or {}).get("created_at") or event.get("occurred_at") or now,
                event_type=event_type, rank=int(out.get("rank") or 0), title=out["title"], why=out["why"],
                payload={**out["payload"], "severity": out["severity"], "precedence": out["precedence"]},
                project_id=project_id, now=now,
            )
            routed["linked" if linked.get("linked") else "written"] += 1
            continue
        if is_bell:
            row["source_notification_id"] = str(payload["notification_id"])
        if out["delivery"] == "record":
            row.pop("deliver_after", None)
        made = repo.insert_inbox_row(uid, row, now=now)
        routed["written" if made else "existing"] += 1
        if event_type == proactive.BID_DUE and out["delivery"] != "record" and project_id:
            routed["superseded_rows"] += repo.inbox_supersede_due_not_ready(uid, project_id, keep_dedupe_key=key,
                                                                             now=now)
        if out["delivery"] == "immediate":
            # Delivery follows row state, not this pass's insert (6.2 step 10).
            for due in repo.inbox_due_immediate(uid, now=now, dedupe_key=key):
                routed[_release_row(due, now, ctx)] += 1
            if out.get("held"):
                routed["held"] += 1
    return {k: int(v) for k, v in routed.items() if v}


def _route_and_finish(event: Mapping[str, Any], now: datetime, worker: str, ctx: _Context) -> str:
    """Route one claimed event and settle its claim: processed with the
    counts, failed for an unroutable event, retry (60 s, 5 min, 30 min, 2 h;
    the fifth attempt dead-letters) for anything else (6.4)."""
    event_id = str(event["id"])
    try:
        routed = route_event(dict(event), now, ctx=ctx)
        status, kwargs = "processed", {"routed": routed}
    except _Unroutable as exc:
        status, kwargs = "failed", {"error": exc.kind}
    except Exception as exc:  # noqa: BLE001 - one event's failure never stops the batch
        logger.warning("assistant proactive: routing %s failed (%s)", event_id, exc.__class__.__name__)
        status, kwargs = "retry", {"error": exc.__class__.__name__[:64],
                                   "retry_seconds": _retry_seconds(event.get("attempts"))}
    try:
        repo.event_finish(None, event_id, worker=worker, status=status, now=now, **kwargs)
    except Exception as exc:  # noqa: BLE001 - the lease returns the event to the claim
        logger.warning("assistant proactive: finishing %s failed (%s)", event_id, exc.__class__.__name__)
        return "finish_error"
    return status


def _route_step(now: datetime, worker: str, ctx: _Context) -> dict[str, int]:
    counts: Counter = Counter()
    batch = int(getattr(ctx.settings, "assistant_proactive_claim_batch", 50) or 50)
    for _ in range(CLAIM_ROUNDS):
        events = repo.events_claim(None, worker=worker, limit=batch, lease_seconds=EVENT_LEASE_SECONDS, now=now)
        if not events:
            break
        ctx.prefetch_bells(events)
        for event in events:
            counts[_route_and_finish(event, now, worker, ctx)] += 1
        if len(events) < batch:
            break
    return dict(counts)


def _skip_step(now: datetime, worker: str, ctx: _Context) -> dict[str, int]:
    """The switched-off tick (6.1): every claimed event, the tails' fresh
    ones included, is finished skipped with routed {skipped: switched_off}."""
    counts: Counter = Counter()
    batch = int(getattr(ctx.settings, "assistant_proactive_claim_batch", 50) or 50)
    for _ in range(CLAIM_ROUNDS):
        events = repo.events_claim(None, worker=worker, limit=batch, lease_seconds=EVENT_LEASE_SECONDS, now=now)
        if not events:
            break
        for event in events:
            try:
                repo.event_finish(None, str(event["id"]), worker=worker, status="skipped",
                                  routed={"skipped": "switched_off"}, now=now)
                counts["skipped"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("assistant proactive: skip %s failed (%s)", event.get("id"), exc.__class__.__name__)
                counts["finish_error"] += 1
        if len(events) < batch:
            break
    return dict(counts)


# ── Delivery: immediate rows, the fold and un-fold (8.4) ────────────────────


def _deliver(row: Mapping[str, Any], now: datetime, ctx: _Context) -> str:
    """assistant_inbox_deliver for one due immediate row: one bell and, for
    high and urgent rows with external sends on and the person's Teams switch
    not off, one Teams ledger row. A merged row (NO_TEAMS_RULES) gets none,
    as render decided. A non-urgent approval warning inside quiet hours gets
    its Teams row with not_before the next 06:00 PT (P31); an urgent one goes
    at once (P39). A phase 8 triage escalation held by quiet hours and
    released on a weekend or holiday (no digest to fold into) rings in-app
    only, with no Teams row (P27; P8R-5, F6); an urgent one is never held."""
    uid = str(row["user_id"])
    severity = row.get("severity")
    teams = (severity in ("high", "urgent") and row.get("precedence") not in NO_TEAMS_RULES
             and ctx.external and ctx.teams_on(uid))
    if (teams and row.get("event_type") == proactive.TRIAGE_ESCALATION and severity != "urgent"
            and proactive.is_held(row)
            and proactive.fold_params(now, ctx.holidays, catchup_hours=ctx.catchup,
                                      slot_times=ctx.slot_times) is None):
        teams = False
    not_before = None
    if (teams and row.get("event_type") == proactive.APPROVAL_EXPIRING and severity != "urgent"
            and proactive.in_quiet_hours(now, start=ctx.quiet_start, end=ctx.quiet_end)):
        not_before = proactive.next_quiet_end(now, end=ctx.quiet_end)
    number = (row.get("payload") or {}).get("project_number")
    result = repo.inbox_deliver(
        uid, str(row["id"]), bell_message=proactive.bell_message(str(row.get("title") or "")),
        bell_metadata=proactive.bell_metadata(str(row.get("event_type") or ""), number),
        teams=teams, teams_not_before=not_before, now=now,
    )
    # A lost fence is normal under two workers (6.4).
    return "delivered" if result.get("delivered") else "fence_lost"


def _fold_args(now: datetime, ctx: _Context) -> dict[str, Any]:
    params = proactive.fold_params(now, ctx.holidays, catchup_hours=ctx.catchup, slot_times=ctx.slot_times)
    if params is None:
        return {"am_date": None, "am_slot_at": None, "fold_until": None}
    am_date, am_slot_at, until = params
    return {"am_date": am_date, "am_slot_at": am_slot_at, "fold_until": until}


def _release_row(row: Mapping[str, Any], now: datetime, ctx: _Context) -> str:
    """One due immediate row: the delivery-time recheck (6.4), a phase 8
    escalation's title re-rendered from live facts (_refresh_escalation),
    then a held row (proactive.is_held) goes through assistant_inbox_fold, which folds it
    into the 7:00 AM digest on a workday before the catch-up window closes
    while the AM digest has not gone out; every other row, and a held row the
    fold answers `deliver` for, is delivered at once with its Teams row."""
    uid = str(row["user_id"])
    reason = ctx.recheck(row, now)
    if reason is not None:
        repo.inbox_dismiss(uid, [row["id"]], reason, now=now)
        return f"dismissed_{reason}"
    row = _refresh_escalation(row, now, ctx)
    if proactive.is_held(row):
        answer = repo.inbox_fold(uid, str(row["id"]), now=now, **_fold_args(now, ctx))
        if answer == "folded":
            return "folded"
        if answer != "deliver":
            return f"fold_{answer}"
    return _deliver(row, now, ctx)


def release_held(now: datetime, *, worker: str | None = None, ctx: _Context | None = None) -> dict[str, int]:
    """The per-tick release step (8.4): every immediate row with
    `delivered_at is null and dismissed_at is null and deliver_after <= now`
    (held or not, so it also delivers rows a crashed worker left behind),
    then the un-fold pass over folded rows: when the person's AM digest went
    out without the row or the AM window closed, A11.14 turns it back into an
    immediate row with its pre-fold precedence (0191) and it is delivered at
    once with its Teams message, unless that precedence is a merge rule
    (NO_TEAMS_RULES)."""
    ctx = ctx or _Context(now)
    counts: Counter = Counter()
    for row in repo.inbox_due_immediate(None, now=now, limit=RELEASE_LIMIT):
        try:
            counts[_release_row(row, now, ctx)] += 1
        except Exception as exc:  # noqa: BLE001 - one row never stops the step
            logger.warning("assistant proactive: release %s failed (%s)", row.get("id"), exc.__class__.__name__)
            counts["errors"] += 1
    args = _fold_args(now, ctx)
    for row in repo.inbox_folded_pending(None, limit=RELEASE_LIMIT):
        try:
            uid = str(row["user_id"])
            reason = ctx.recheck(row, now)
            if reason is not None:
                repo.inbox_dismiss(uid, [row["id"]], reason, now=now)
                counts[f"dismissed_{reason}"] += 1
                continue
            answer = repo.inbox_fold(uid, str(row["id"]), now=now, **args)
            if answer != "deliver":
                counts[f"fold_{answer}"] += 1
                continue
            counts["unfolded"] += 1
            fresh = repo.inbox_row(uid, str(row["id"]))
            if fresh is not None:
                counts[_deliver(_refresh_escalation(fresh, now, ctx), now, ctx)] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("assistant proactive: un-fold %s failed (%s)", row.get("id"), exc.__class__.__name__)
            counts["errors"] += 1
    return dict(counts)


# ── Digests (8.2) ────────────────────────────────────────────────────────────


def deliver_digests(now: datetime, *, worker: str, ctx: _Context | None = None) -> dict[str, int]:
    """Digest delivery for every open slot whose sweeper finished (8.2): the
    people with a pending digest row due at the slot instant, minus those
    who already have this slot's digest, each rechecked (6.4), then
    assistant_digest_deliver per person (one ledger row, one bell and, with
    external sends on, one email ledger row; an empty slot writes nothing)."""
    ctx = ctx or _Context(now)
    counts: Counter = Counter()
    for slot, day, slot_at in _open_slots(now, ctx):
        held = repo.proactive_slot(None, f"digest_{slot}", day)
        if held is None or held.get("finished_at") is None:
            counts["waiting"] += 1
            continue
        for uid in repo.inbox_pending_digest_users(None, slot_at=slot_at):
            try:
                if repo.digest_for_slot(uid, day, slot) is not None:
                    continue
                for row in repo.inbox_pending_digest(uid, slot_at=slot_at):
                    reason = ctx.recheck(row, now)
                    if reason is not None:
                        repo.inbox_dismiss(uid, [row["id"]], reason, now=now)
                        counts[f"dismissed_{reason}"] += 1
                    else:
                        # Phase 8: an escalation's clauses are true when it goes out.
                        _refresh_escalation(row, now, ctx)
                # Phase 8 (plan 9 step 2): a person who turned the assistant
                # digest email off gets the in-app digest and bell, no email row.
                result = repo.digest_deliver(uid, digest_date=day, slot=slot, slot_at=slot_at, worker=worker,
                                             email=ctx.external and ctx.digest_email_on(uid), now=now)
                counts["digests" if result.get("claimed") else "not_claimed"] += 1
            except Exception as exc:  # noqa: BLE001 - one person never stops the slot
                logger.warning("assistant proactive: digest for one person failed (%s)", exc.__class__.__name__)
                counts["errors"] += 1
    return dict(counts)


# ── Purge (5.1) ──────────────────────────────────────────────────────────────


def run_purge(now: datetime, *, worker: str, ctx: _Context | None = None) -> dict[str, int]:
    """Once per Pacific day after 02:00: the retention purge (B4: events and
    inbox 90 days, digests and slots 1 year, sends 90 days, legal hold
    skipped, closed-project mutes retired) through the `purge` slot."""
    local = now.astimezone(PT)
    if local.time() < PURGE_AFTER:
        return {}
    ctx = ctx or _Context(now)
    day = local.date()
    held = repo.proactive_slot(None, "purge", day)
    if held is not None and held.get("finished_at") is not None:
        return {}
    if held is None or held.get("claimed_by") != worker:
        if repo.claim_proactive_slot(None, "purge", day, worker, now) is None:
            return {}
    s = ctx.settings
    totals: Counter = Counter()
    for _ in range(PURGE_ROUNDS):
        res = repo.proactive_purge(
            None, inbox_days=int(s.assistant_proactive_inbox_retention_days),
            event_days=int(s.assistant_proactive_event_retention_days),
            digest_days=int(s.assistant_proactive_digest_retention_days),
            send_days=int(s.assistant_proactive_send_retention_days), limit=PURGE_LIMIT, now=now,
        )
        counts = {k: int(v) for k, v in (res or {}).items()
                  if k != "limit" and isinstance(v, int) and not isinstance(v, bool)}
        totals.update(counts)
        if not counts or max(counts.values()) < PURGE_LIMIT:
            break
    repo.finish_proactive_slot(None, "purge", day, worker=worker, result=dict(totals), now=now)
    return dict(totals)


# ── The tick and the loop ────────────────────────────────────────────────────


def _step(out: dict[str, Any], name: str, fn, *args, **kwargs) -> None:
    """Run one tick step in its own try (6.1); a failure is logged with its
    class only and recorded as an error count."""
    try:
        out[name] = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - one step never stops the others
        logger.warning("assistant proactive: step %s failed (%s)", name, exc.__class__.__name__, exc_info=True)
        out.setdefault("errors", []).append(name)


def poll_once(now: datetime | None = None, *, worker: str | None = None, tails: bool = True) -> dict:
    """One router tick. `now` defaults to the current time; `worker` defaults
    to WORKER_ID (the concurrency check passes distinct ids, 11.3);
    `tails=False` skips the two table tails and is for the dev tick script
    only (11.5). Each step runs in its own try so one failure never stops the
    others: the tails, the approval-expiring scan, routing, delivery of due
    immediate rows and held-row release, digest delivery for open slots, then
    `assistant_delivery.send_due(now, worker)` (5.1). Returns counts only."""
    now = (_utc(now) if now is not None else None) or datetime.now(UTC)
    worker = worker or WORKER_ID
    settings = get_settings()
    out: dict[str, Any] = {}
    if not _env_on(settings):
        return {"skipped": "env_off"}
    try:
        ctx = _Context(now, settings=settings)
    except Exception as exc:  # noqa: BLE001 - without the company row nothing routes this tick
        logger.warning("assistant proactive: tick context unreadable (%s)", exc.__class__.__name__)
        out["errors"] = ["context"]
        # The send step reads its own gates, so it still runs (6.1).
        _step(out, "sends", assistant_delivery.send_due, now, worker)
        return out
    out["switch_on"] = ctx.switch_on
    if tails:
        _step(out, "lag", _lag_guard, now, worker, ctx)
        for name in TAILS:
            _step(out, f"tail_{name}", run_tail, name, now, worker=worker, ctx=ctx)
    if ctx.switch_on:
        _step(out, "approvals", scan_approvals, now, ctx=ctx)
        for slot, _day, _at in _open_slots(now, ctx):
            _step(out, f"sweep_{slot}", run_slot_sweeper, slot, now, worker=worker, ctx=ctx)
        _step(out, "route", _route_step, now, worker, ctx)
        _step(out, "release", release_held, now, worker=worker, ctx=ctx)
        _step(out, "digests", deliver_digests, now, worker=worker, ctx=ctx)
    else:
        _step(out, "route", _skip_step, now, worker, ctx)
    _step(out, "purge", run_purge, now, worker=worker, ctx=ctx)
    _step(out, "sends", assistant_delivery.send_due, now, worker)
    return out


async def polling_loop() -> None:
    """while True: await asyncio.to_thread(poll_once), log any exception,
    then sleep settings.assistant_proactive_poll_seconds (the guarded shape of
    assistant_scheduler.polling_loop, 6.1). Cancellation stops the sleep."""
    interval = int(getattr(get_settings(), "assistant_proactive_poll_seconds", 30) or 30)
    while True:
        try:
            await asyncio.to_thread(poll_once)
        except Exception:  # noqa: BLE001 - the loop must survive any tick failure
            logger.exception("Assistant proactive tick failed")
        await asyncio.sleep(interval)

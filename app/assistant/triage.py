"""Phase 8 triage rules: the pure fact-pack builder, the reason, action and
severity enums, the per-kind action catalog, the answer schema, the fixed
system prompt, the pack renderer, the input-token estimate behind the spend
hold, the "Work on this" prompt templates and the format-only answer check
(docs/ASSISTANT_PHASE8_PLAN.md 5.3, 5.5, 7.1, WI-2).

Pure: no database, no model, no settings, no clock of its own: `build_pack`
takes a required `now` and never reads wall time. The model module and the triage service import this
file; it imports only other pure assistant modules.

Owner decisions that shape it (docs/PHASE8_OWNER_DECISIONS.md): the model
reads facts only (question 3 (a)), its choice is final (question 6 (c)) so
this module has no fact predicates and no reason table that second-guesses it,
and it may choose `urgent` (question 8). The only defence against hostile
text is that none can reach the pack: `assert_pack_clean` checks every field
of a built pack against its own closed vocabulary.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Any

from app.assistant import proactive
from app.assistant.rrules import PT
from app.assistant.rrules import _holiday_set as _holidays
from app.core.roles import INTERNAL_ROLES, Role

UTC = timezone.utc

# ── enums (final names; the router work item imports these) ───────────────────

DECISIONS = ("none", "suggest", "escalate")
SEVERITIES = ("normal", "high", "urgent")

INVALID_ANSWER = "invalid_answer"

# A reason is not a fact. It selects the escalation template and the Inbox
# label and is the audit record of why the model chose (plan 5.5, 19.1 FQ5).
NONE_REASONS = ("covered_by_app", "already_seen", "not_your_part", "info_only", "no_action_needed")
SUGGEST_REASONS = (
    "due_soon_part_missing", "waiting_on_you", "needs_decision", "follow_up_due", "changed_after_your_work",
)
ESCALATE_REASONS = (
    "due_soon_part_missing", "due_soon_unread", "waiting_on_you", "unread_long", "needs_decision",
    "changed_after_your_work",
)
# Every reason the stored decision may carry. `invalid_answer` is written by
# `validate` for a malformed item; the model is never offered it.
REASONS = (
    "covered_by_app", "already_seen", "not_your_part", "info_only", "no_action_needed",
    "due_soon_part_missing", "due_soon_unread", "waiting_on_you", "unread_long", "needs_decision",
    "follow_up_due", "changed_after_your_work", INVALID_ANSWER,
)
MODEL_REASONS = tuple(r for r in REASONS if r != INVALID_ANSWER)
REASONS_BY_DECISION: dict[str, tuple[str, ...]] = {
    "none": NONE_REASONS,
    "suggest": SUGGEST_REASONS,
    "escalate": ESCALATE_REASONS,
}

REASON_HELP: dict[str, str] = {
    "covered_by_app": "BDR's own notification already says what matters",
    "already_seen": "the person has already seen it",
    "not_your_part": "the item is not this person's part of the work",
    "info_only": "informational, nothing to do",
    "no_action_needed": "nothing needs doing",
    "due_soon_part_missing": "the bid is due soon and this person's part looks missing",
    "due_soon_unread": "the bid is due soon and the notice is unread",
    "waiting_on_you": "work is waiting on this person",
    "unread_long": "the notice has waited unread for a long time",
    "needs_decision": "a decision from this person is needed",
    "follow_up_due": "a follow up is due",
    "changed_after_your_work": "something changed after this person's work",
}

# ── action catalog (plan 5.5) ─────────────────────────────────────────────────

ACTIONS = (
    "open_receive_quotes", "review_verify", "review_scope_lines", "open_call_in_list",
    "review_changed_drawings", "review_revision", "review_approval_request", "follow_up_vendor",
    "create_followup_todo", "ask_assistant_summary",
)
ACTION_HELP: dict[str, str] = {
    "open_receive_quotes": "go through the vendor quotes on the bid",
    "review_verify": "check the numbers before Verify",
    "review_scope_lines": "review the scope lines",
    "open_call_in_list": "work the GC call in list",
    "review_changed_drawings": "check what the changed drawings affect",
    "review_revision": "review a proposal revision",
    "review_approval_request": "review an approval request",
    "follow_up_vendor": "follow up with vendors that have not replied",
    "create_followup_todo": "set up a follow-up to-do",
    "ask_assistant_summary": "ask for a short summary of where things stand",
}
# Actions that name work only a role with write access can do. A read-only
# role (Accountant) is never offered them (owner question 23 (a)).
WRITE_ACTIONS = frozenset(
    {
        "open_receive_quotes", "review_verify", "review_scope_lines", "open_call_in_list",
        "review_revision", "review_approval_request", "follow_up_vendor", "create_followup_todo",
        "review_changed_drawings",
    }
)
READ_ONLY_ROLES = frozenset({Role.ACCOUNTANT.value})

# The record kinds triage covers: every mirrored BDR bell kind. The one record
# kind with no bell, `project.failed_to_bid`, is never enqueued and never in a
# pack (plan P2).
TRIAGE_KINDS: tuple[str, ...] = tuple("bell." + k for k in proactive.BELL_EVENTS)
KIND_LABELS: dict[str, str] = {k: proactive.EVENT_TYPES[k]["label"] for k in TRIAGE_KINDS}

_ASK = "ask_assistant_summary"
_TODO = "create_followup_todo"
_KIND_ACTIONS: dict[str, tuple[str, ...]] = {
    "go_no_go": (_ASK,),
    "intake_needed": (_ASK, _TODO),
    "rfp_review": (_ASK,),
    "send_to_estimator": (_ASK, _TODO),
    "stage_handoff": ("open_receive_quotes", "review_scope_lines", _ASK, _TODO),
    "estimate_submitted": (_ASK,),
    "quote_received": ("open_receive_quotes", "follow_up_vendor", _ASK),
    "late_quote": ("open_receive_quotes", "follow_up_vendor", _ASK),
    "verify": ("review_verify", _ASK),
    "verified": (_ASK,),
    "call_in": ("open_call_in_list", _ASK),
    "scope_lines_review": ("review_scope_lines", _ASK),
    "auto_send_sent": (_ASK,),
    "auto_send_problem": (_ASK, _TODO),
    "proposal_send_failed": (_ASK, _TODO),
    "submitted": (_ASK,),
    "bid_outcome": (_ASK,),
    "project_abandoned": (_ASK,),
    "due": ("open_receive_quotes", _TODO, _ASK),
    "approval_requested": ("review_approval_request", _ASK),
    "revision_approval": ("review_approval_request", "review_revision", _ASK),
    "revision_round": ("review_revision", _ASK),
    "drawing_changed": ("review_changed_drawings", _ASK),
    "reverify_required": ("review_verify", "review_changed_drawings", _ASK),
    "personal": (_TODO, _ASK),
    "estimator_note": (_TODO, _ASK),
    "pm": (_ASK, _TODO),
}
KIND_ACTIONS: dict[str, tuple[str, ...]] = {"bell." + k: v for k, v in _KIND_ACTIONS.items()}

# Project stages (the `project_stage` enum), the only strings a stage fact may be.
STAGES = (
    "intake", "go_no_go", "to_estimator", "estimate_received", "rfqs", "receive_quotes", "select_vendors",
    "labor_numbers", "markup", "gc_pricing", "verify", "send_out", "submitted", "declined", "bid_outcome",
    "pm_only", "cp_only",
)
# A copy of `assistant_proactive.CLOSED_STAGES` (a pure module cannot import
# the service); a test pins the copy.
CLOSED_STAGES = ("declined", "bid_outcome", "pm_only", "cp_only")

KEY_MAX = 10  # keys t1 to t10: at most ten rows per call (plan P5)
BATCH_KEYS = tuple(f"t{i}" for i in range(1, KEY_MAX + 1))

_ROLE_VALUES = frozenset(r.value for r in INTERNAL_ROLES)
_PROJECT_NUMBER_RE = re.compile(r"\d{2}\.\d{1,2}\.\d{3,5}[A-Za-z]?")
_KEY_RE = re.compile(r"t[0-9]{1,2}")
_PT_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}-0[78]:00")
_WHEN_RE = re.compile(r"[A-Z][a-z]{2} [A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2} [AP]M PT")
_HHMM_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
_INT_BOUND = 10_000_000
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_BELL_FIELD_KEYS = ("stage", "round", "routing_event")

# ── the answer schema (plan 5.5) ──────────────────────────────────────────────

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "enum": list(BATCH_KEYS)},
                    "decision": {"type": "string", "enum": list(DECISIONS)},
                    "reason": {"type": "string", "enum": list(MODEL_REASONS)},
                    "action": {"type": ["string", "null"], "enum": [*ACTIONS, None]},
                    "severity": {"type": ["string", "null"], "enum": [*SEVERITIES, None]},
                },
                "required": ["key", "decision", "reason", "action", "severity"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}

# ── the system prompt (plan 7.1; fixed text) ──────────────────────────────────

SYSTEM_PROMPT = """You triage BDR notifications for one person at G3 Electrical. Each item is a notification BDR already
delivered to this person. You get facts only: kinds, times, counts and true or false values. You never
get names or message text.
For each item choose exactly one decision:
- none: BDR's own notification already covers it, or nothing needs doing. Give a reason.
- suggest: there is a clear next step this person could ask their assistant to help with. Pick one
  action from the allowed list for that kind. Pick the action for this person's own part: vendor
  quotes are the materials engineer's part, labor is the labor engineer's part, markups are the
  estimating admin's part. When the notice is unread, the bid is not yet submitted and an allowed
  action other than ask_assistant_summary is this person's next step, suggest it rather than none.
- escalate: the person should hear from their assistant because something BDR's notice did not
  say matters, for example the bid is due within hours and their part is still missing, or the notice
  has waited unread for a long time while work waits on them. Choose the severity:
  normal: in the next workday digest. high: now, with a Teams message, at any time outside quiet
  hours on any day, weekends and holidays included; inside quiet hours it waits until they end.
  urgent: now at any hour, even at night, on a weekend or a holiday. Use urgent only when waiting
  until the next morning would likely cause a missed bid or real harm.
  Urgent differs from high only inside quiet hours. Choose urgent only when all three are true:
  1. The time line says quiet hours: yes. Outside quiet hours high already reaches the person at
     once, so choose high, never urgent, however soon the bid is due.
  2. The project is open, its stage is not submitted, and internal_due_at is still ahead and before
     12 PM (noon) Pacific on the morning quiet hours end.
  3. your_part_missing is true for that item.
  High waits until quiet hours end, so a due time still ahead that falls inside tonight's quiet
  hours, with the part missing, is urgent: high would reach the person after the bid is due.
  Otherwise use high or normal. A short hours_to_due outside quiet hours, a long unread wait, a
  retried row or several items on one project never make an item urgent by themselves.
  Decide each item on its own kind. Items on the same project share its due time and part missing
  facts: escalate at most one of them for those facts, preferably the deadline reminder (bell.due),
  and decide the others on what their own kind adds. When escalated_project_last_2h is true the
  person was already reached about that project; escalate again only for something new. A bid
  submitted notice is never a reason to escalate.
  Prefer none or suggest when in doubt. Never escalate only because a notice exists. Your choice is
  final: nothing checks it before the person is reached.
Fill the fields this way, or the answer is thrown away: action is one of that item's allowed actions
when the decision is suggest, and null when the decision is none or escalate. severity is normal,
high or urgent when the decision is escalate, and null when the decision is none or suggest. The
reason is one of the reasons listed for the chosen decision.
Answer only with the JSON object. Use only the item keys, reasons and actions you were given.
"""

# ── "Work on this" templates (plan 5.5, P26) ──────────────────────────────────
# Server text typed into the composer. The only slots are the project number
# (pattern-checked) and one absolute Pacific time label; no free text, no
# relative-hours slot.

_TEMPLATES: dict[str, dict[str, str]] = {
    "open_receive_quotes": {
        "numbered": "Help me with the vendor quotes on bid {number}. Which quotes are in and which are still missing?",
        "plain": "Help me with my vendor quotes. Which bids still have quotes missing?",
    },
    "review_verify": {
        "numbered": "Walk me through what needs checking before I verify bid {number}.",
        "plain": "Walk me through what needs checking before I verify a bid.",
    },
    "review_scope_lines": {
        "numbered": "Show me the scope lines I need to review on bid {number}.",
        "plain": "Show me the scope lines I need to review.",
    },
    "open_call_in_list": {
        "numbered": "Help me with the call in list for bid {number}. Which GCs are left to call?",
        "plain": "Help me with my call in lists. Which GCs are left to call?",
    },
    "review_changed_drawings": {
        "numbered": (
            "Help me check bid {number}: drawings changed {when}. "
            "Which of my open RFQs and quotes do the changed sheets affect?"
        ),
        "plain": "Which of my open RFQs and quotes do the recently changed drawings affect?",
    },
    "review_revision": {
        "numbered": "Summarize the revision on bid {number} and what I need to do about it.",
        "plain": "Summarize the latest revision and what I need to do about it.",
    },
    "review_approval_request": {
        "numbered": "Help me review the approval request on bid {number}. What is it asking for?",
        "plain": "Help me review my pending approval request. What is it asking for?",
    },
    "follow_up_vendor": {
        "numbered": "Which vendors on bid {number} have not replied, and what should I follow up on?",
        "plain": "Which vendors have not replied to me, and what should I follow up on?",
    },
    "create_followup_todo": {
        "numbered": "Help me set up a follow-up to-do for bid {number}.",
        "plain": "Help me set up a follow-up to-do for this update.",
    },
    "ask_assistant_summary": {
        "numbered": "Give me a short summary of where bid {number} stands.",
        "plain": "Give me a short summary of where this stands.",
    },
}
ACTION_TEMPLATES = _TEMPLATES
TEMPLATE_SLOTS = frozenset({"number", "when"})


class PackError(ValueError):
    """The rows or facts handed to `build_pack` cannot make a safe pack."""


class PackNotClean(ValueError):
    """`assert_pack_clean` found a value outside the closed vocabularies."""


# ── small helpers ─────────────────────────────────────────────────────────────


def _role_value(role: Any) -> str | None:
    value = role.value if isinstance(role, Role) else role
    return value if isinstance(value, str) and value in _ROLE_VALUES else None


def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value.strip():
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _pt_iso(moment: datetime) -> str:
    return moment.astimezone(PT).isoformat(timespec="minutes")


def _pt_iso_safe(moment: datetime) -> str | None:
    """`_pt_iso`, or None when the instant cannot be a clean Pacific time (a
    year before 1883 formats with a local mean time offset, a year near the
    edge of the calendar overflows). The database can hold such values; the
    pack must stay total over them (a null fact, never a failed batch)."""
    try:
        text = _pt_iso(moment)
    except (OverflowError, ValueError, OSError):
        return None
    return text if _pt_iso_ok(text) else None


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and abs(value) < _INT_BOUND


def _count(value: Any) -> int:
    if _is_int(value):
        return max(0, int(value))
    return 0


def project_number(value: Any) -> str | None:
    """A job number safe to show, or None. Anything that is not a system made
    number (a name typed where a number belongs) is refused."""
    if isinstance(value, str) and _PROJECT_NUMBER_RE.fullmatch(value):
        return value
    return None


def batch_keys(count: int) -> list[str]:
    """The item keys for a batch of `count` rows: t1 to t{count}."""
    if not isinstance(count, int) or count < 0 or count > KEY_MAX:
        raise PackError(f"a batch holds at most {KEY_MAX} rows")
    return list(BATCH_KEYS[:count])


def is_triage_kind(kind: Any) -> bool:
    return isinstance(kind, str) and kind in KIND_LABELS


def allowed_actions(kind: Any, role: Any) -> tuple[str, ...]:
    """The actions a `suggest` may name for this record kind and role: the
    kind's catalog entry, minus write work for a read-only role. Empty for an
    unknown kind or role."""
    value = _role_value(role)
    if value is None or not is_triage_kind(kind):
        return ()
    actions = KIND_ACTIONS.get(kind, ())
    if value in READ_ONLY_ROLES:
        actions = tuple(a for a in actions if a not in WRITE_ACTIONS)
    return actions


def pacific_label(value: datetime) -> str:
    """"Thu Oct 29, 1:00 PM PT": the only time form a template may carry."""
    return proactive.pt_label(value)


def action_prompt(action: str, *, number: Any = None, when: Any = None) -> str:
    """The "Work on this" text for an action. Slots are the pattern-checked
    project number and one `pacific_label` string; a missing or invalid slot
    selects the plain variant. Raises KeyError for an unknown action."""
    templates = _TEMPLATES[action]
    num = project_number(number)
    label = when if isinstance(when, str) and _WHEN_RE.fullmatch(when) else None
    numbered = templates["numbered"]
    if num is None or ("{when}" in numbered and label is None):
        return templates["plain"]
    return numbered.format(number=num, when=label or "")


# ── the fact pack (plan 5.3) ──────────────────────────────────────────────────


def _row_get(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if row.get(name) is not None:
            return row[name]
    return None


def _flag(entry: Mapping[str, Any] | None, *names: str) -> bool | None:
    """A boolean from the first present name of a fact entry (a bool, or a
    timestamp or id whose presence means true), or None when the entry or all
    names are absent."""
    if not isinstance(entry, Mapping):
        return None
    seen = False
    for name in names:
        if name in entry:
            seen = True
            if entry[name]:
                return True
    return False if seen else None


def _bell_fields(payload: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(payload, Mapping):
        return out
    stage = payload.get("stage")
    if isinstance(stage, str) and stage in STAGES:
        out["stage"] = stage
    if _is_int(payload.get("round")):
        out["round"] = int(payload["round"])
    routing = payload.get("routing_event")
    if isinstance(routing, str) and routing in proactive.BELL_EVENTS:
        out["routing_event"] = routing
    return out


def _quiet_window(start: Any, end: Any) -> tuple[time, time]:
    try:
        return proactive._as_time(start), proactive._as_time(end)
    except ValueError as exc:
        raise PackError("the quiet hours window is not a pair of HH:MM times") from exc


def _hhmm(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def _clock_words(hhmm: str) -> str:
    """"18:00" as "6 PM", "06:30" as "6:30 AM"."""
    hour, minute = int(hhmm[:2]), int(hhmm[3:])
    text = f"{hour % 12 or 12}" + (f":{minute:02d}" if minute else "")
    return f"{text} {'AM' if hour < 12 else 'PM'}"


def _time_line(
    now: datetime, holidays: Iterable[Any] | None, quiet: tuple[time, time]
) -> dict[str, Any]:
    local = now.astimezone(PT)
    day = local.date()
    skip = _holidays(holidays)
    return {
        "now": _pt_iso(now),
        "weekday": _WEEKDAYS[day.weekday()],
        "workday": proactive.is_workday(day, skip),
        "weekend": day.weekday() >= 5,
        "holiday": day in skip,
        "quiet_hours": proactive.in_quiet_hours(now, start=quiet[0], end=quiet[1]),
        "quiet_from": _hhmm(quiet[0]),
        "quiet_to": _hhmm(quiet[1]),
    }


def _project_closed(project: Mapping[str, Any]) -> bool:
    if _is_bool(project.get("closed")):
        return bool(project["closed"])
    return bool(
        project.get("abandoned_at") or project.get("failed_to_bid_at")
        or project.get("current_stage") in CLOSED_STAGES
    )


def build_pack(
    rows: Sequence[Mapping[str, Any]],
    facts: Mapping[str, Any],
    role: Any,
    *,
    now: datetime,
    quiet_start: Any = None,
    quiet_end: Any = None,
) -> dict[str, Any]:
    """The fact pack for one person's batch (plan 5.3).

    `rows` are the batch's record rows in order (`event_type`, `created_at`,
    `cap_deferred_at`, `project_id`, `source_notification_id`, `payload`);
    row i gets key t{i+1}. `facts` holds what the service gathered, keyed by id:

    - `projects`: project id -> {number, current_stage, internal_bid_at,
      abandoned_at, failed_to_bid_at, closed (optional boolean)}
    - `notifications`: source notification id -> {read or read_at, dismissed or
      dismissed_at, teams_sent or teams_sent_at, email_sent or email_log_id}
    - `your_part_missing`: project id -> boolean or None
    - `router_bid_due_projects`: project ids with the person's own bid-due row
    - `open_todos`, `pending_cards`: project id -> count
    - `escalated_projects_last_2h`: project ids
    - `holidays`: company holiday dates

    `now` is required: the pack never reads the wall clock, so a caller that
    runs on an injected clock cannot show the model a different one.
    `quiet_start` / `quiet_end` (a time or "HH:MM") are the configured quiet
    window; when omitted they come from `facts["quiet_start"]` /
    `facts["quiet_end"]`, then from the proactive defaults (18:00 to 06:00).

    Only ids, enums, booleans, counts, Pacific times and a pattern-checked
    project number get through; every other string in the inputs is ignored.
    A row of a kind outside `TRIAGE_KINDS` (`project.failed_to_bid`, a
    router-owned kind) raises PackError.
    """
    role_value = _role_value(role)
    if role_value is None:
        raise PackError("the pack needs an internal role")
    if len(rows) > KEY_MAX:
        raise PackError(f"a batch holds at most {KEY_MAX} rows")
    moment = _dt(now)
    if moment is None:
        raise PackError("now must be a datetime")
    quiet = _quiet_window(
        quiet_start if quiet_start is not None else (facts.get("quiet_start") or proactive.QUIET_START),
        quiet_end if quiet_end is not None else (facts.get("quiet_end") or proactive.QUIET_END),
    )
    projects = facts.get("projects") or {}
    notifications = facts.get("notifications") or {}
    part_missing = facts.get("your_part_missing") or {}
    bid_due_rows = set(facts.get("router_bid_due_projects") or ())
    todos = facts.get("open_todos") or {}
    cards = facts.get("pending_cards") or {}
    escalated = set(facts.get("escalated_projects_last_2h") or ())

    items: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        kind = _row_get(row, "event_type", "kind")
        if not is_triage_kind(kind):
            raise PackError("a row of this kind is not triaged")
        received = _dt(_row_get(row, "created_at", "received_at"))
        if received is None:
            raise PackError("a row has no usable time")
        received_iso = _pt_iso_safe(received)
        age_minutes = max(0, int((moment - received).total_seconds() // 60))
        if received_iso is None or not _is_int(age_minutes):
            received_iso = age_minutes = None  # an out of range time: null, not a failed batch
        project_id = row.get("project_id")
        project = projects.get(project_id) if project_id is not None else None
        project = project if isinstance(project, Mapping) else None
        stage = None
        closed = None
        due_iso = None
        hours_to_due = None
        number = None
        if project is not None:
            number = project_number(project.get("number"))
            raw_stage = project.get("current_stage")
            stage = raw_stage if isinstance(raw_stage, str) and raw_stage in STAGES else None
            closed = _project_closed(project)
            due = _dt(project.get("internal_bid_at"))
            if due is not None:
                due_iso = _pt_iso_safe(due)
                hours = math.floor((due - moment).total_seconds() / 3600)
                if due_iso is None or not _is_int(hours):
                    due_iso = None  # an out of range date: both due facts are null
                else:
                    hours_to_due = hours
        missing = part_missing.get(project_id) if project_id is not None else None
        note = notifications.get(row.get("source_notification_id"))
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        linked = payload.get("linked_notification_ids")
        items.append(
            {
                "key": f"t{index + 1}",
                "kind": kind,
                "kind_label": KIND_LABELS[kind],
                "received_at": received_iso,
                "age_minutes": age_minutes,
                "project_number": number,
                "stage": stage,
                "project_closed": closed,
                "internal_due_at": due_iso,
                "hours_to_due": hours_to_due,
                "your_part_missing": missing if _is_bool(missing) else None,
                "router_bid_due_row": project_id in bid_due_rows if project_id is not None else False,
                "bell_read": _flag(note, "read", "read_at"),
                "bell_dismissed": _flag(note, "dismissed", "dismissed_at"),
                "bdr_sent_teams": _flag(note, "teams_sent", "teams_sent_at"),
                "bdr_sent_email": _flag(note, "email_sent", "email_log_id"),
                "linked_bells": min(len(linked), 99) if isinstance(linked, list) else 0,
                "bell_fields": _bell_fields(payload),
                "open_todos_on_project": _count(todos.get(project_id)) if project_id is not None else 0,
                "pending_cards_on_project": _count(cards.get(project_id)) if project_id is not None else 0,
                "escalated_project_last_2h": project_id in escalated if project_id is not None else False,
                "deferred_overnight": bool(row.get("cap_deferred_at")),
                "allowed_actions": list(allowed_actions(kind, role_value)),
            }
        )
    pack = {"role": role_value, "time": _time_line(moment, facts.get("holidays"), quiet), "items": items}
    assert_pack_clean(pack)
    return pack


def _check_nullable(check):
    return lambda value, ctx: value is None or check(value, ctx)


def _pt_iso_ok(value: Any, _ctx: Any = None) -> bool:
    if not isinstance(value, str) or not _PT_ISO_RE.fullmatch(value):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


_ITEM_CHECKS = {
    "key": lambda v, c: isinstance(v, str) and bool(_KEY_RE.fullmatch(v)) and v in BATCH_KEYS,
    "kind": lambda v, c: is_triage_kind(v),
    "kind_label": lambda v, c: isinstance(v, str) and v == KIND_LABELS.get(c["kind"]),
    "received_at": _check_nullable(lambda v, c: _pt_iso_ok(v)),
    "age_minutes": _check_nullable(lambda v, c: _is_int(v)),
    "project_number": _check_nullable(lambda v, c: project_number(v) is not None),
    "stage": _check_nullable(lambda v, c: isinstance(v, str) and v in STAGES),
    "project_closed": _check_nullable(lambda v, c: _is_bool(v)),
    "internal_due_at": _check_nullable(lambda v, c: _pt_iso_ok(v)),
    "hours_to_due": _check_nullable(lambda v, c: _is_int(v)),
    "your_part_missing": _check_nullable(lambda v, c: _is_bool(v)),
    "router_bid_due_row": lambda v, c: _is_bool(v),
    "bell_read": _check_nullable(lambda v, c: _is_bool(v)),
    "bell_dismissed": _check_nullable(lambda v, c: _is_bool(v)),
    "bdr_sent_teams": _check_nullable(lambda v, c: _is_bool(v)),
    "bdr_sent_email": _check_nullable(lambda v, c: _is_bool(v)),
    "linked_bells": lambda v, c: _is_int(v),
    "bell_fields": lambda v, c: _bell_fields_ok(v),
    "open_todos_on_project": lambda v, c: _is_int(v),
    "pending_cards_on_project": lambda v, c: _is_int(v),
    "escalated_project_last_2h": lambda v, c: _is_bool(v),
    "deferred_overnight": lambda v, c: _is_bool(v),
    "allowed_actions": lambda v, c: (
        isinstance(v, list) and all(isinstance(a, str) and a in ACTIONS for a in v)
    ),
}
_TIME_CHECKS = {
    "now": lambda v: _pt_iso_ok(v),
    "weekday": lambda v: isinstance(v, str) and v in _WEEKDAYS,
    "workday": _is_bool,
    "weekend": _is_bool,
    "holiday": _is_bool,
    "quiet_hours": _is_bool,
    "quiet_from": lambda v: isinstance(v, str) and bool(_HHMM_RE.fullmatch(v)),
    "quiet_to": lambda v: isinstance(v, str) and bool(_HHMM_RE.fullmatch(v)),
}


def _bell_fields_ok(value: Any) -> bool:
    if not isinstance(value, dict) or not set(value) <= set(_BELL_FIELD_KEYS):
        return False
    if "stage" in value and not (isinstance(value["stage"], str) and value["stage"] in STAGES):
        return False
    if "round" in value and not _is_int(value["round"]):
        return False
    return not (
        "routing_event" in value
        and not (isinstance(value["routing_event"], str) and value["routing_event"] in proactive.BELL_EVENTS)
    )


def assert_pack_clean(pack: Any) -> None:
    """Raise PackNotClean unless every key and value of the pack belongs to a
    closed vocabulary: catalog enums, Pacific ISO times, pattern-checked
    project numbers, `t<n>` keys, booleans and bounded integers. This is the
    control that keeps planted text away from the model (plan 15.1)."""
    if not isinstance(pack, dict) or set(pack) != {"role", "time", "items"}:
        raise PackNotClean("pack shape")
    if _role_value(pack["role"]) is None:
        raise PackNotClean("role")
    time_line = pack["time"]
    if not isinstance(time_line, dict) or set(time_line) != set(_TIME_CHECKS):
        raise PackNotClean("time line shape")
    for name, check in _TIME_CHECKS.items():
        if not check(time_line[name]):
            raise PackNotClean(f"time line {name}")
    items = pack["items"]
    if not isinstance(items, list) or len(items) > KEY_MAX:
        raise PackNotClean("items")
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != set(_ITEM_CHECKS):
            raise PackNotClean("item shape")
        for name, check in _ITEM_CHECKS.items():
            if not check(item[name], item):
                raise PackNotClean(f"item field {name}")
        if item["key"] in seen:
            raise PackNotClean("duplicate key")
        seen.add(item["key"])


# ── rendering and the hold estimate ───────────────────────────────────────────


def render_pack(pack: dict[str, Any]) -> str:
    """The user-role content for the model call: the time line, the reasons and
    actions it may use, and one JSON line of facts per item. Facts only."""
    assert_pack_clean(pack)
    time_line = pack["time"]
    label = pacific_label(datetime.fromisoformat(time_line["now"]))
    yes = lambda flag: "yes" if flag else "no"  # noqa: E731
    lines = [
        f"It is {label} in Pacific time ({time_line['weekday']}).",
        f"Workday: {yes(time_line['workday'])}. Weekend: {yes(time_line['weekend'])}. "
        f"Company holiday: {yes(time_line['holiday'])}. "
        f"Quiet hours ({_clock_words(time_line['quiet_from'])} to {_clock_words(time_line['quiet_to'])}): "
        f"{yes(time_line['quiet_hours'])}.",
        f"The person's role is {pack['role']}.",
        "Reasons you may give, by decision:",
    ]
    for decision in DECISIONS:
        lines.append(f"- {decision}: " + "; ".join(f"{r} ({REASON_HELP[r]})" for r in REASONS_BY_DECISION[decision]))
    used = sorted({a for item in pack["items"] for a in item["allowed_actions"]}, key=ACTIONS.index)
    if used:
        lines.append("Actions you may suggest:")
        lines.extend(f"- {a}: {ACTION_HELP[a]}" for a in used)
    lines.append("Items (one JSON object per line; each lists the actions allowed for it):")
    lines.extend(json.dumps(item, separators=(",", ":")) for item in pack["items"])
    return "\n".join(lines)


def estimate_input_tokens(system: str, schema: Any, rendered: str) -> int:
    """The input size behind the spend hold (plan 5.4): one token per four
    characters of the system prompt, the serialized schema and the rendered
    pack together, rounded up."""
    total = len(system) + len(json.dumps(schema, sort_keys=True)) + len(rendered)
    return max(1, math.ceil(total / 4))


# ── answer format validation (plan 5.5) ───────────────────────────────────────


@dataclass
class ValidationResult:
    """`decisions` maps each answered batch key to a normalized decision dict
    (`key`, `decision`, `reason`, `action`, `severity`); `missing` lists batch
    keys the answer left out (the service retries them once, then settles
    `skipped bad_answer`). The counters feed logs only."""

    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    unknown_keys: int = 0
    duplicates: int = 0
    invalid: int = 0
    extra_fields: int = 0
    malformed: bool = False


_ITEM_FIELDS = ("key", "decision", "reason", "action", "severity")


def _downgraded(key: str) -> dict[str, Any]:
    return {"key": key, "decision": "none", "reason": INVALID_ANSWER, "action": None, "severity": None}


def _normalize(key: str, raw: Mapping[str, Any], allowed: tuple[str, ...]) -> dict[str, Any] | None:
    """The normalized decision when the item is well formed, else None."""
    decision, reason = raw.get("decision"), raw.get("reason")
    action, severity = raw.get("action"), raw.get("severity")
    if not (isinstance(decision, str) and decision in DECISIONS):
        return None
    if not (isinstance(reason, str) and reason in REASONS_BY_DECISION[decision]):
        return None
    if decision == "suggest":
        if not (isinstance(action, str) and action in allowed) or severity is not None:
            return None
    elif decision == "escalate":
        if not (isinstance(severity, str) and severity in SEVERITIES) or action is not None:
            return None
    elif action is not None or severity is not None:
        return None
    return {"key": key, "decision": decision, "reason": reason, "action": action, "severity": severity}


def validate(
    answer: Any,
    batch_keys_: Sequence[str],
    kinds: Mapping[str, str] | Sequence[str],
    role: Any,
) -> ValidationResult:
    """Check the model's answer for format only. There is no fact predicate: a
    well-formed choice is kept exactly as chosen, whatever the pack said.

    - keys outside the batch are dropped; a duplicate key keeps its first
      occurrence; a batch key the answer lacks lands in `missing`;
    - a malformed item (decision, reason, action or severity outside its enum,
      an action not in the kind and role catalog, severity on a non-escalation,
      an action on a non-suggestion) becomes `none` with reason `invalid_answer`;
    - fields beyond the five are dropped and counted.

    `kinds` is a mapping from batch key to record kind, or a sequence aligned
    with `batch_keys_`."""
    keys = list(batch_keys_)
    kind_of = dict(zip(keys, kinds, strict=False)) if not isinstance(kinds, Mapping) else dict(kinds)
    result = ValidationResult()
    entries = answer.get("items") if isinstance(answer, Mapping) else None
    if not isinstance(entries, list):
        result.malformed = True
        result.missing = keys
        return result
    wanted = set(keys)
    for raw in entries:
        key = raw.get("key") if isinstance(raw, Mapping) else None
        if not (isinstance(key, str) and key in wanted):
            result.unknown_keys += 1
            continue
        if key in result.decisions:
            result.duplicates += 1
            continue
        result.extra_fields += sum(1 for name in raw if name not in _ITEM_FIELDS)
        normalized = _normalize(key, raw, allowed_actions(kind_of.get(key), role))
        if normalized is None:
            result.invalid += 1
            normalized = _downgraded(key)
        result.decisions[key] = normalized
    result.missing = [k for k in keys if k not in result.decisions]
    return result

"""Phase 8 triage evals (docs/ASSISTANT_PHASE8_PLAN.md 16.4, WI-14a).

Two files and this harness:

  triage_cases.json      the golden set: one fact pack item per case with the
                         expected decision (allowed sets of decisions,
                         reasons, actions and severities). Covered negatives
                         fail on any escalation; the not_urgent time-of-day
                         cases fail on an urgent severity.
  triage_injection.json  the offline injection corpus: hostile text planted
                         in a notification message, project name, vendor
                         name, note, file name, email subject or project
                         number. The built fact pack must contain none of it.

Modes (the default is --check):

    uv run python -m app.assistant.evals.run_triage_evals --check

  Offline and free. Validates the structure of both files (enums, calendar
  fields derived from `now`, per-kind roles when the router module loads, no
  em or en dash anywhere) and prints the counts. It makes no network call and
  never touches a database.

    uv run python -m app.assistant.evals.run_triage_evals --injection

  Offline and free. Builds the fact pack for every injection case with the
  real `app.assistant.triage.build_pack` and fails if any planted string, or
  any string the pack cleaner would reject, appears in the pack or its
  rendering. Exit 2 (incomplete) when the pack builder is not available, so
  the gate never passes on a skipped tier.

    ASSISTANT_TRIAGE_TEST_SPEND_CAP_USD=1 \
    ASSISTANT_TRIAGE_USER_ALLOWLIST=<the two e2e user ids> uv run python -m \
        app.assistant.evals.run_triage_evals --live --report <out.json>

  The live run makes real calls to the triage model (gpt-6-luna) through
  `assistant_triage_model.classify`, one call per batch of golden cases for
  one role and one clock time, so a batch is one person at one moment. It
  REFUSES (exit 2, before anything is imported or any call is made) unless
  ASSISTANT_TRIAGE_TEST_SPEND_CAP_USD is set to a number above 0 and at most
  1, the settings object honours it (so `classify` enforces the same cap on
  company triage spend for the Pacific day) and SUPABASE_URL is the dev
  project. The runner also keeps its own running total of the calls it made
  and stops at the cap. It reports decision agreement, severity agreement and
  reasoning-token usage per batch size (plan 5.4), and a gate of at least 85
  percent decision agreement, zero escalations on covered negatives, zero
  urgent on the not-urgent time-of-day cases and zero invalid answers. The
  gates measure the model before release; they are not runtime checks (owner
  question 6 (c)). A miss is reported to the owner, never fixed by switching
  models (owner question 4).

  Only answer-quality failures (TriageBadAnswer, TriageTruncated) count as
  invalid answers. Every other failure of the call (busy, timeout, provider
  error, spend check, company cap, unpriced model) is infrastructure: those
  cases stay ungraded, the report says `incomplete` (never `fail`) and the
  error class is recorded. Busy and timeout are retried once; the company cap,
  the spend check and an unpriced model stop the run. A run limited with
  --only covers a subset and is always `incomplete`.

  Preconditions for a live run (plan 5.4 accepts a non-atomic test-cap check
  only because exactly one triage process runs): the 5071 server and any dev
  server must run with ASSISTANT_TRIAGE_LOOP_ENABLED=false, and the runner
  refuses while any assistant_triage_runs row is `claimed`. The settings
  validator (WI-6) refuses the test cap without ASSISTANT_TRIAGE_USER_ALLOWLIST,
  so that variable is part of the command. The report is written after every
  call (a partial copy survives a crash or Ctrl-C); the default path is in the
  system temp directory, never inside the repo.

The reconciliation point with `app.assistant.triage` (WI-2) is
`to_service_inputs`: it turns a case into the `(rows, facts, role)` the triage
service hands `triage.build_pack` (plan 5.3). If the real builder reads other
keys, change that one function.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
CASES = HERE / "triage_cases.json"
INJECTION = HERE / "triage_injection.json"
PT = ZoneInfo("America/Los_Angeles")
UTC = ZoneInfo("UTC")

DEV_REF = "bpidntbyvoooqvaispup"
PROD_REF = "zxqkqotlcgbalwoqeeca"
CAP_ENV = "ASSISTANT_TRIAGE_TEST_SPEND_CAP_USD"
MAX_BATCH_SIZE = 20  # the ceiling config.py allows for assistant_triage_batch_max
MAX_TEST_CAP = Decimal(1)  # owner question 22 (a): the live test is capped at 1 USD
FORBIDDEN_DASHES = ("\u2014", "\u2013")

# ── Enums (plan 5.5; the real module is cross-checked when it exposes them) ──
DECISIONS = ("none", "suggest", "escalate")
SEVERITIES = ("normal", "high", "urgent")
NONE_REASONS = ("covered_by_app", "already_seen", "not_your_part", "info_only", "no_action_needed")
SUGGEST_REASONS = (
    "due_soon_part_missing", "waiting_on_you", "needs_decision", "follow_up_due", "changed_after_your_work",
)
ESCALATE_REASONS = (
    "due_soon_part_missing", "due_soon_unread", "waiting_on_you", "unread_long", "needs_decision",
    "changed_after_your_work",
)
REASONS_BY_DECISION = {"none": NONE_REASONS, "suggest": SUGGEST_REASONS, "escalate": ESCALATE_REASONS}
REASONS = (*NONE_REASONS, "due_soon_part_missing", "due_soon_unread", "waiting_on_you", "unread_long",
           "needs_decision", "follow_up_due", "changed_after_your_work", "invalid_answer")
STAGES = (
    "intake", "go_no_go", "to_estimator", "estimate_received", "rfqs", "receive_quotes", "select_vendors",
    "labor_numbers", "markup", "gc_pricing", "verify", "send_out", "submitted", "declined", "bid_outcome",
    "pm_only", "cp_only",
)
CLOSED_STAGES = ("declined", "bid_outcome", "pm_only", "cp_only")
ACTIONS = (
    "open_receive_quotes", "review_verify", "review_scope_lines", "open_call_in_list",
    "review_changed_drawings", "review_revision", "review_approval_request", "follow_up_vendor",
    "create_followup_todo", "ask_assistant_summary",
)
ROLES = (
    "executive", "estimating_admin", "estimating_engineer_labor", "estimating_engineer_materials",
    "it_admin", "accountant",
)
TAGS = ("covered_negative", "suggest", "escalate", "time_of_day", "not_urgent", "urgent_ok", "ambiguous")
SURFACES = (
    "notification_message", "project_name", "vendor_name", "note_body", "file_name", "email_subject",
    "project_number", "routing_event", "payload_stage", "payload_round", "current_stage",
)
ITEM_FIELDS = {
    "kind": str, "age_minutes": int, "project_number": (str, type(None)), "stage": (str, type(None)),
    "project_closed": (bool, type(None)), "internal_due_at": (str, type(None)), "hours_to_due": (int, type(None)),
    "your_part_missing": (bool, type(None)), "router_bid_due_row": bool, "bell_read": bool,
    "bell_dismissed": bool, "bdr_sent_teams": bool, "bdr_sent_email": bool, "linked_bells": int,
    "bell_fields": dict, "open_todos_on_project": int, "pending_cards_on_project": int,
    "escalated_project_last_2h": bool, "deferred_overnight": bool,
}
BELL_FIELD_KEYS = {"stage": str, "round": int, "routing_event": str}
CALENDAR_FIELDS = ("weekday", "workday", "weekend", "holiday", "quiet_hours")
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_SLUG = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
# The job-number shape the pack accepts (mirrors proactive._NUMBER_RE).
NUMBER_RE = re.compile(r"^[0-9][0-9A-Za-z.\-]{0,23}$")

# Gates (plan 16.4).
GATE_AGREEMENT = 0.85


class EvalFileError(ValueError):
    pass


class Refused(Exception):
    """The live run will not start; nothing was imported, sent or spent."""


class BuilderUnavailable(Exception):
    """`app.assistant.triage.build_pack` is not built yet (or not importable)."""


# ── Loading and validation (offline) ─────────────────────────────────────────


def load_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise EvalFileError(f"{path.name}: expected an object with version 1")
    return data


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for k, v in value.items():
            yield str(k)
            yield from _walk_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk_strings(v)


def _no_dashes(value: Any, label: str) -> None:
    for text in _walk_strings(value):
        if any(d in text for d in FORBIDDEN_DASHES):
            raise EvalFileError(f"{label}: contains an em or en dash")


def derive_calendar(now: datetime, holidays: Iterable[str]) -> dict:
    """The calendar facts of `now` (Pacific): the same rules as the router's
    `is_workday` (Monday to Friday, not a company holiday) and quiet hours
    (18:00 to 06:00). A test pins this copy against `proactive`."""
    weekend = now.weekday() >= 5
    holiday = now.date().isoformat() in set(holidays)
    return {
        "weekday": _DAYS[now.weekday()],
        "workday": not weekend and not holiday,
        "weekend": weekend,
        "holiday": holiday,
        "quiet_hours": now.hour >= 18 or now.hour < 6,
    }


def parse_pacific(text: Any, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(text))
    except ValueError as exc:
        raise EvalFileError(f"{label}: not an ISO time ({text!r})") from exc
    if parsed.tzinfo is None:
        raise EvalFileError(f"{label}: needs a UTC offset")
    return parsed.astimezone(PT)


def _bell_audiences() -> dict[str, frozenset] | None:
    """kind -> audience roles from the router module, or None when it does not
    import (the structural check then skips the audience test)."""
    try:
        from app.assistant import proactive
    except Exception:  # noqa: BLE001 - offline structure check must not need the router
        return None
    out: dict[str, frozenset] = {}
    for key, (_label, _group, audience) in proactive._BELL_EVENTS.items():
        out["bell." + key] = frozenset(getattr(r, "value", str(r)) for r in audience)
    return out


def validate_item(item: Mapping[str, Any], label: str, *, audiences: Mapping[str, frozenset] | None = None,
                  role: str | None = None) -> None:
    if set(item) != set(ITEM_FIELDS):
        raise EvalFileError(f"{label}: item fields must be exactly {sorted(ITEM_FIELDS)}")
    for key, kinds in ITEM_FIELDS.items():
        value = item[key]
        if isinstance(value, bool) and kinds in (int, (int, type(None))):
            raise EvalFileError(f"{label}: {key} must not be a boolean")
        if not isinstance(value, kinds):
            raise EvalFileError(f"{label}: {key} has the wrong type")
    kind = item["kind"]
    if not kind.startswith("bell.") or not _SLUG.match(kind):
        raise EvalFileError(f"{label}: kind must be a bell.* enum (plan P2: bell-sourced rows only)")
    if audiences is not None:
        if kind not in audiences:
            raise EvalFileError(f"{label}: {kind} is not a router bell kind")
        if role is not None and role not in audiences[kind]:
            raise EvalFileError(f"{label}: role {role} does not receive {kind}")
    number = item["project_number"]
    if number is not None and not NUMBER_RE.match(number):
        raise EvalFileError(f"{label}: project_number does not match the job-number pattern")
    if item["stage"] is not None and item["stage"] not in STAGES:
        raise EvalFileError(f"{label}: stage is not a project stage enum")
    if item["stage"] in CLOSED_STAGES and not item["project_closed"]:
        raise EvalFileError(f"{label}: a project in stage {item['stage']} is closed")
    if (number is None) != (item["stage"] is None) or (number is None) != (item["project_closed"] is None):
        raise EvalFileError(f"{label}: stage and project_closed are set exactly when the project number is")
    if (item["internal_due_at"] is None) != (item["hours_to_due"] is None):
        raise EvalFileError(f"{label}: internal_due_at and hours_to_due are set together")
    if item["internal_due_at"] is not None:
        parse_pacific(item["internal_due_at"], f"{label}.internal_due_at")
    for key in ("age_minutes", "linked_bells", "open_todos_on_project", "pending_cards_on_project"):
        if item[key] < 0:
            raise EvalFileError(f"{label}: {key} must not be negative")
    for key, value in item["bell_fields"].items():
        want = BELL_FIELD_KEYS.get(key)
        if want is None or not isinstance(value, want) or isinstance(value, bool):
            raise EvalFileError(f"{label}: bell_fields.{key} is not an allowlisted id or enum")
        if isinstance(value, str) and not _SLUG.match(value):
            raise EvalFileError(f"{label}: bell_fields.{key} must be an enum slug")
    if item["your_part_missing"] is not None and item["hours_to_due"] is not None:
        if not 0 <= item["hours_to_due"] < 24:
            raise EvalFileError(f"{label}: your_part_missing is computed only for a bid due in the next 24 hours")
    if item["your_part_missing"] is not None and item["hours_to_due"] is None:
        raise EvalFileError(f"{label}: your_part_missing needs a due time")


def _subset(values: Any, allowed: Sequence[str], label: str) -> list[str]:
    if not isinstance(values, list) or len(set(values)) != len(values):
        raise EvalFileError(f"{label}: expected a list without duplicates")
    bad = [v for v in values if v not in allowed]
    if bad:
        raise EvalFileError(f"{label}: unknown values {bad}")
    return values


def validate_cases(data: Mapping[str, Any]) -> list[dict]:
    cases = data.get("cases")
    holidays = data.get("holidays")
    if not isinstance(cases, list) or not cases:
        raise EvalFileError("triage_cases.json: no cases")
    if not isinstance(holidays, list):
        raise EvalFileError("triage_cases.json: holidays must be a list of dates")
    for h in holidays:
        datetime.fromisoformat(str(h))
    audiences = _bell_audiences()
    seen: set[str] = set()
    for case in cases:
        cid = case.get("id")
        label = f"case {cid}"
        if not isinstance(cid, str) or not re.match(r"^[a-z][a-z0-9_-]{1,31}$", cid) or cid in seen:
            raise EvalFileError(f"{label}: id must be a unique slug")
        seen.add(cid)
        tags = _subset(case.get("tags"), TAGS, f"{label}.tags")
        if case.get("role") not in ROLES:
            raise EvalFileError(f"{label}: unknown role")
        if not isinstance(case.get("note"), str) or not case["note"].strip():
            raise EvalFileError(f"{label}: note is required")
        now = parse_pacific(case.get("now"), f"{label}.now")
        if datetime.fromisoformat(case["now"]).utcoffset() != now.utcoffset():
            raise EvalFileError(f"{label}: now must carry the Pacific offset of that day")
        if case.get("calendar") != derive_calendar(now, holidays):
            raise EvalFileError(f"{label}: calendar does not match `now` (derived: {derive_calendar(now, holidays)})")
        validate_item(case.get("item", {}), f"{label}.item", audiences=audiences, role=case["role"])
        item = case["item"]
        if item["internal_due_at"] is not None:
            due = parse_pacific(item["internal_due_at"], label)
            if item["hours_to_due"] != int((due.astimezone(UTC) - now.astimezone(UTC)).total_seconds() // 3600):
                raise EvalFileError(f"{label}: hours_to_due does not match now and internal_due_at")
        exp = case.get("expected")
        if not isinstance(exp, dict) or set(exp) != {"decisions", "reasons", "actions", "severities"}:
            raise EvalFileError(f"{label}: expected needs decisions, reasons, actions and severities")
        decisions = _subset(exp["decisions"], DECISIONS, f"{label}.expected.decisions")
        if not decisions:
            raise EvalFileError(f"{label}: expected.decisions is empty")
        _subset(exp["reasons"], REASONS, f"{label}.expected.reasons")
        _subset(exp["actions"], ACTIONS, f"{label}.expected.actions")
        _subset(exp["severities"], SEVERITIES, f"{label}.expected.severities")
        if "suggest" not in decisions and exp["actions"]:
            raise EvalFileError(f"{label}: actions listed but suggest is not an allowed decision")
        if "escalate" not in decisions and exp["severities"]:
            raise EvalFileError(f"{label}: severities listed but escalate is not an allowed decision")
        if "covered_negative" in tags and "escalate" in decisions:
            raise EvalFileError(f"{label}: a covered negative must not allow escalate")
        if "not_urgent" in tags and "urgent" in exp["severities"]:
            raise EvalFileError(f"{label}: a not_urgent case must not allow urgent")
        if "urgent_ok" in tags and "not_urgent" in tags:
            raise EvalFileError(f"{label}: urgent_ok and not_urgent exclude each other")
        if "not_urgent" in tags or "urgent_ok" in tags:
            if "time_of_day" not in tags:
                raise EvalFileError(f"{label}: not_urgent and urgent_ok belong to time_of_day cases")
        if "time_of_day" in tags and not (
            case["calendar"]["quiet_hours"] or case["calendar"]["weekend"] or case["calendar"]["holiday"]
        ):
            raise EvalFileError(f"{label}: a time_of_day case must be at night, on a weekend or on a holiday")
        if "time_of_day" in tags and ("not_urgent" in tags) == ("urgent_ok" in tags):
            raise EvalFileError(f"{label}: a time_of_day case carries exactly one of not_urgent or urgent_ok")
        if "urgent_ok" in tags:
            # Plan 16.4: urgent only when the bid is due before the next morning is over
            # and the person's part is missing. "Before the next morning is over" is
            # today, or tomorrow no later than noon Pacific.
            if item["your_part_missing"] is not True:
                raise EvalFileError(f"{label}: an urgent_ok case needs your_part_missing true")
            if item["internal_due_at"] is None:
                raise EvalFileError(f"{label}: an urgent_ok case needs a due time")
            due = parse_pacific(item["internal_due_at"], label)
            days_ahead = (due.date() - now.date()).days
            if due < now or days_ahead > 1 or (days_ahead == 1 and due.hour >= 12):
                raise EvalFileError(f"{label}: an urgent_ok case must be due today or before noon tomorrow")
        _no_dashes(case, label)
    return cases


def validate_injection(data: Mapping[str, Any]) -> list[dict]:
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise EvalFileError("triage_injection.json: no cases")
    audiences = _bell_audiences()
    seen: set[str] = set()
    for case in cases:
        cid = case.get("id")
        label = f"injection {cid}"
        if not isinstance(cid, str) or not re.match(r"^[a-z][a-z0-9_-]{1,31}$", cid) or cid in seen:
            raise EvalFileError(f"{label}: id must be a unique slug")
        seen.add(cid)
        if case.get("surface") not in SURFACES:
            raise EvalFileError(f"{label}: unknown surface")
        plant = case.get("plant")
        if not isinstance(plant, str) or len(plant) < 8:
            raise EvalFileError(f"{label}: plant must be a hostile string of at least 8 characters")
        needles = case.get("needles")
        if not isinstance(needles, list) or not needles or not all(isinstance(n, str) and len(n) >= 4 for n in needles):
            raise EvalFileError(f"{label}: needles must be a list of strings of at least 4 characters")
        if plant not in needles:
            raise EvalFileError(f"{label}: the whole plant must be one of the needles")
        if case.get("role") not in ROLES:
            raise EvalFileError(f"{label}: unknown role")
        now = parse_pacific(case.get("now"), f"{label}.now")
        if datetime.fromisoformat(case["now"]).utcoffset() != now.utcoffset():
            raise EvalFileError(f"{label}: now must carry the Pacific offset of that day")
        validate_item(case.get("item", {}), f"{label}.item", audiences=audiences, role=case["role"])
        # Dashes are forbidden everywhere except that a plant may carry one on
        # purpose; the hostile corpus does not need them, so none is allowed.
        _no_dashes(case, label)
    return cases


def check() -> dict:
    golden = validate_cases(load_json(CASES))
    injection = validate_injection(load_json(INJECTION))
    tags = [set(c["tags"]) for c in golden]

    def count(tag: str) -> int:
        return sum(1 for t in tags if tag in t)

    return {
        "cases": len(golden),
        "covered_negatives": count("covered_negative"),
        "time_of_day": count("time_of_day"),
        "not_urgent": count("not_urgent"),
        "urgent_ok": count("urgent_ok"),
        "escalate_expected": sum(1 for c in golden if c["expected"]["decisions"] == ["escalate"]),
        "injection_cases": len(injection),
        "injection_surfaces": sorted({c["surface"] for c in injection}),
        "roles": sorted({c["role"] for c in golden}),
        "triage_module": cross_check_triage(golden),
    }


def cross_check_triage(cases: Sequence[Mapping[str, Any]] | None = None) -> str:
    """Compare the enums here with `app.assistant.triage` when it exposes
    them, then (when the pack builder works) build a pack for every golden
    case and require that the expected actions are ones the model is offered
    for that kind and role, so no case is unsatisfiable. Returns a short
    status; raises EvalFileError on a mismatch."""
    try:
        from app.assistant import triage
    except Exception:  # noqa: BLE001 - WI-2 may not be built yet
        return "unavailable"
    pairs = (("DECISIONS", DECISIONS), ("SEVERITIES", SEVERITIES), ("REASONS", REASONS),
             ("NONE_REASONS", NONE_REASONS), ("SUGGEST_REASONS", SUGGEST_REASONS),
             ("ESCALATE_REASONS", ESCALATE_REASONS), ("ACTIONS", ACTIONS), ("STAGES", STAGES),
             ("CLOSED_STAGES", CLOSED_STAGES))
    compared = 0
    for name, local in pairs:
        theirs = getattr(triage, name, None)
        if theirs is None:
            continue
        compared += 1
        if set(theirs) != set(local):
            raise EvalFileError(f"{name} differs from app.assistant.triage.{name}: "
                                f"only here {sorted(set(local) - set(theirs))}, only there {sorted(set(theirs) - set(local))}")
    by_decision = getattr(triage, "REASONS_BY_DECISION", None)
    if by_decision is not None:
        compared += 1
        if {k: set(v) for k, v in by_decision.items()} != {k: set(v) for k, v in REASONS_BY_DECISION.items()}:
            raise EvalFileError("REASONS_BY_DECISION differs from app.assistant.triage.REASONS_BY_DECISION")
    status = f"compared {compared} enum lists" if compared else "no enums exposed yet"
    if cases is None:
        return status
    try:
        builder = load_pack_builder()
        holidays = load_json(CASES)["holidays"]
        built = 0
        for case in cases:
            rows, facts, role, now = to_service_inputs(case, holidays)
            pack = builder(rows, facts, role, now=now)
            offered = set(pack["items"][0]["allowed_actions"])
            wanted = set(case["expected"]["actions"])
            if "suggest" in case["expected"]["decisions"] and not offered:
                raise EvalFileError(f"case {case['id']}: no action is offered for {case['item']['kind']}")
            if wanted - offered:
                raise EvalFileError(f"case {case['id']}: expected actions {sorted(wanted - offered)} are not offered "
                                    f"for {case['item']['kind']} and role {case['role']}")
            built += 1
    except (BuilderUnavailable, NotImplementedError):
        return status + "; pack builder unavailable"
    return status + f"; built {built} packs"


# ── Service inputs (the one reconciliation point with triage.build_pack) ─────


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _stable_id(prefix: str, case_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"bdr-triage-eval:{prefix}:{case_id}"))


def to_service_inputs(case: Mapping[str, Any], holidays: Iterable[str] = ()) -> tuple[list[dict], dict, str, datetime]:
    """`(rows, facts, role, now)` for one case, in the shape the triage
    service passes `triage.build_pack(rows, facts, role, now=now)` (plan 5.3
    and the docstring of `build_pack`): one record inbox row plus the facts
    the service gathers around it. Raw columns are rebuilt from the case's
    derived facts (age, hours to due, read state), so the real pack code does
    its own derivation. `facts` also carries the tainted sources (message,
    names, notes, file names, subjects) as empty strings unless an injection
    case plants one; a correct builder never reads them."""
    item = case["item"]
    now = parse_pacific(case["now"], "now")
    cid = case["id"]
    inbox_id = _stable_id("inbox", cid)
    nid = _stable_id("notification", cid)
    pid = _stable_id("project", cid) if item["project_number"] is not None else None
    created = (now.astimezone(UTC) - timedelta(minutes=item["age_minutes"])).astimezone(PT)
    event_key = item["kind"].split(".", 1)[1]
    payload: dict[str, Any] = {
        "notification_id": nid,
        "notification_type": event_key,
        "routing_event": item["bell_fields"].get("routing_event", event_key),
    }
    for key in ("stage", "round"):
        if key in item["bell_fields"]:
            payload[key] = item["bell_fields"][key]
    if item["linked_bells"]:
        payload["linked_notification_ids"] = [_stable_id(f"linked{i}", cid) for i in range(item["linked_bells"])]
    row = {
        "id": inbox_id,
        "user_id": _stable_id("user", cid),
        "event_type": item["kind"],
        "severity": "low",
        "title": "",
        "why": None,
        "payload": payload,
        "project_id": pid,
        "source_notification_id": nid,
        "created_at": _utc_iso(created),
        "cap_deferred_at": _utc_iso(created) if item["deferred_overnight"] else None,
    }
    facts: dict[str, Any] = {
        "holidays": list(holidays),
        "projects": {},
        "notifications": {
            nid: {
                "read_at": _utc_iso(now) if item["bell_read"] else None,
                "dismissed_at": _utc_iso(now) if item["bell_dismissed"] else None,
                "teams_sent_at": _utc_iso(created) if item["bdr_sent_teams"] else None,
                "email_log_id": 1 if item["bdr_sent_email"] else None,
                "message": "",
            },
        },
        "your_part_missing": {},
        "router_bid_due_projects": [],
        "open_todos": {},
        "pending_cards": {},
        "escalated_projects_last_2h": [],
        "vendors": {},
        "notes": {},
        "files": {},
        "emails": {},
    }
    if pid is not None:
        facts["projects"][pid] = {
            "id": pid,
            "number": item["project_number"],
            "name": "",
            "internal_bid_at": parse_pacific(item["internal_due_at"], "due").isoformat() if item["internal_due_at"] else None,
            "current_stage": item["stage"],
            "abandoned_at": _utc_iso(now) if item["project_closed"] and item["stage"] not in CLOSED_STAGES else None,
            "failed_to_bid_at": None,
        }
        if item["your_part_missing"] is not None:
            facts["your_part_missing"][pid] = item["your_part_missing"]
        if item["router_bid_due_row"]:
            facts["router_bid_due_projects"].append(pid)
        facts["open_todos"][pid] = item["open_todos_on_project"]
        facts["pending_cards"][pid] = item["pending_cards_on_project"]
        if item["escalated_project_last_2h"]:
            facts["escalated_projects_last_2h"].append(pid)
    return [row], facts, case["role"], now


def plant_in_inputs(case: Mapping[str, Any], rows: list[dict], facts: dict) -> None:
    """Put the case's hostile string where its surface lives, in the raw
    source records the service reads around the row. The first six surfaces
    are keys the builder never reads. `project_number`, `routing_event`,
    `payload_stage`, `payload_round` and `current_stage` are the fields that
    really cross the builder (pattern-checked or enum-checked there), so a
    hostile value planted in one must be dropped."""
    plant = case["plant"]
    surface = case["surface"]
    row = rows[0]
    pid = row["project_id"]
    nid = row["source_notification_id"]
    if surface == "notification_message":
        facts["notifications"][nid]["message"] = plant
        row["title"] = plant
        row["why"] = plant
    elif surface == "project_name":
        if pid is None:
            raise EvalFileError(f"injection {case['id']}: project_name needs a project")
        facts["projects"][pid]["name"] = plant
        row["payload"]["project_name"] = plant
    elif surface == "vendor_name":
        facts["vendors"]["v1"] = {"name": plant}
        row["payload"]["vendor_name"] = plant
        facts["notifications"][nid]["vendor_name"] = plant
    elif surface == "note_body":
        facts["notes"][pid or "none"] = [{"body": plant}]
        row["payload"]["note"] = plant
    elif surface == "file_name":
        facts["files"][pid or "none"] = [{"name": plant}]
        row["payload"]["file_name"] = plant
    elif surface == "email_subject":
        facts["emails"]["e1"] = {"subject": plant}
        row["payload"]["email_subject"] = plant
    elif surface == "project_number":
        if pid is None:
            raise EvalFileError(f"injection {case['id']}: project_number needs a project")
        facts["projects"][pid]["number"] = plant
    elif surface in ("routing_event", "payload_stage", "payload_round"):
        # Allowlisted payload keys: the builder keeps an enum or an int and drops the rest.
        row["payload"][{"routing_event": "routing_event", "payload_stage": "stage", "payload_round": "round"}[surface]] = plant
    elif surface == "current_stage":
        if pid is None:
            raise EvalFileError(f"injection {case['id']}: current_stage needs a project")
        facts["projects"][pid]["current_stage"] = plant
    else:  # pragma: no cover - validate_injection refuses it first
        raise EvalFileError(f"unknown surface {surface}")


# ── Pack builder and the offline injection tier ──────────────────────────────

PackBuilder = Callable[..., dict]


def load_pack_builder() -> PackBuilder:
    """The real `triage.build_pack`, or BuilderUnavailable when WI-2 has not
    landed. The eval never substitutes its own builder: a self-made one would
    prove nothing about the product."""
    try:
        from app.assistant import triage
    except Exception as exc:  # noqa: BLE001
        raise BuilderUnavailable(f"app.assistant.triage did not import ({exc.__class__.__name__})") from None
    build = getattr(triage, "build_pack", None)
    if not callable(build):
        raise BuilderUnavailable("app.assistant.triage has no build_pack yet")
    return build


def render_for_scan(pack: Any) -> str:
    """Everything a leak could ride in: the pack serialised, plus the model
    input when `triage.render_pack` exists."""
    text = json.dumps(pack, ensure_ascii=False, sort_keys=True, default=str)
    try:
        from app.assistant import triage

        render = getattr(triage, "render_pack", None)
        if callable(render):
            text += "\n" + str(render(pack))
    except Exception:  # noqa: BLE001 - rendering is a bonus scan, never a requirement
        pass
    return text


def _fold(text: str) -> str:
    """Case-fold and drop zero-width and spacing characters, so a planted
    string cannot hide from the scan behind invisible characters."""
    return re.sub(r"[\s​‌‍⁠﻿]+", "", text).casefold()


def leaked_needles(case: Mapping[str, Any], haystack: str) -> list[str]:
    raw = haystack.casefold()
    folded = _fold(haystack)
    return [n for n in case["needles"] if n.casefold() in raw or _fold(n) in folded]


def run_injection(cases: list[dict], builder: PackBuilder) -> dict:
    """Offline tier: build the pack for every case with `builder` and assert
    none of the planted strings is in it. 100 percent is the gate. A case also
    fails when the pack is empty or lost the benign project number (a builder
    that returns nothing would otherwise pass every case), except that the
    `project_number` surface must come back with the number dropped."""
    results: list[dict] = []
    holidays = load_json(CASES)["holidays"]
    for case in cases:
        golden_like = {"id": case["id"], "role": case["role"], "now": case["now"], "item": case["item"],
                       "calendar": derive_calendar(parse_pacific(case["now"], "now"), holidays)}
        rows, facts, role, now = to_service_inputs(golden_like, holidays)
        plant_in_inputs(case, rows, facts)
        try:
            pack = builder(rows, facts, role, now=now)
            leaks = leaked_needles(case, render_for_scan(pack))
            items = pack.get("items") if isinstance(pack, Mapping) else None
            number = items[0].get("project_number") if items else "missing"
            want = None if case["surface"] == "project_number" else case["item"]["project_number"]
            vacuous = not items or number != want
            error = None
        except NotImplementedError:
            raise BuilderUnavailable("app.assistant.triage.build_pack is still a stub") from None
        except Exception as exc:  # noqa: BLE001 - a builder crash is a failure, with the class only (no hostile text)
            leaks, vacuous, error = [], False, exc.__class__.__name__
        results.append({"id": case["id"], "surface": case["surface"], "leaks": len(leaks), "vacuous": vacuous,
                        "error": error, "pass": not leaks and not vacuous and error is None})
    passed = sum(1 for r in results if r["pass"])
    return {"cases": len(results), "passed": passed, "failed": len(results) - passed,
            "pass_rate": (passed / len(results)) if results else 0.0, "results": results,
            "gate": "pass" if results and passed == len(results) else "fail"}


# ── Grading (pure) ───────────────────────────────────────────────────────────


def format_problems(answer: Mapping[str, Any] | None, allowed: Sequence[str] | None = None) -> list[str]:
    """Format-only validation of one answer item (plan 5.5): the same checks
    `triage.validate` applies, written here so the eval can count invalid
    answers without trusting the module under test. `allowed` is the action
    list the pack offered this item (the whole catalog when None)."""
    if not isinstance(answer, Mapping):
        return ["missing"]
    decision, reason = answer.get("decision"), answer.get("reason")
    action, severity = answer.get("action"), answer.get("severity")
    if decision not in DECISIONS:
        return ["decision"]
    problems: list[str] = []
    if reason not in REASONS_BY_DECISION[decision]:
        problems.append("reason")
    if decision == "suggest":
        if action not in (ACTIONS if allowed is None else allowed):
            problems.append("action")
        if severity is not None:
            problems.append("severity")
    elif decision == "escalate":
        if severity not in SEVERITIES:
            problems.append("severity")
        if action is not None:
            problems.append("action")
    elif action is not None or severity is not None:
        problems.append("action" if action is not None else "severity")
    return sorted(set(problems))


def grade_case(case: Mapping[str, Any], answer: Mapping[str, Any] | None,
               allowed: Sequence[str] | None = None) -> dict:
    exp = case["expected"]
    tags = set(case["tags"])
    problems = format_problems(answer, allowed)
    valid = not problems
    decision = answer.get("decision") if valid and answer else None
    severity = answer.get("severity") if valid and answer else None
    decision_ok = valid and decision in exp["decisions"]
    sev_ok: bool | None = None
    if valid and decision == "escalate" and exp["severities"]:
        sev_ok = severity in exp["severities"]
    return {
        "id": case["id"],
        "valid": valid,
        "problems": problems,
        "decision": decision,
        "severity": severity,
        "decision_ok": decision_ok,
        "severity_ok": sev_ok,
        "covered_escalation": "covered_negative" in tags and decision == "escalate",
        "urgent_on_not_urgent": "not_urgent" in tags and decision == "escalate" and severity == "urgent",
        "reason_ok": (not exp["reasons"]) or (valid and answer.get("reason") in exp["reasons"]),
        "action_ok": (not exp["actions"]) or decision != "suggest" or (valid and answer.get("action") in exp["actions"]),
    }


def summarize(graded: Sequence[Mapping[str, Any]], total_cases: int) -> dict:
    n = len(graded)
    sev = [g["severity_ok"] for g in graded if g["severity_ok"] is not None]
    decision_ok = sum(1 for g in graded if g["decision_ok"])
    out = {
        "cases": total_cases,
        "graded": n,
        "decision_agreement": (decision_ok / n) if n else 0.0,
        "severity_agreement": (sum(1 for s in sev if s) / len(sev)) if sev else None,
        "severity_graded": len(sev),
        "reason_agreement": (sum(1 for g in graded if g["reason_ok"]) / n) if n else 0.0,
        "action_agreement": (sum(1 for g in graded if g["action_ok"]) / n) if n else 0.0,
        "covered_escalations": sum(1 for g in graded if g["covered_escalation"]),
        "urgent_on_not_urgent": sum(1 for g in graded if g["urgent_on_not_urgent"]),
        "invalid_answers": sum(1 for g in graded if not g["valid"]),
    }
    if n < total_cases:
        out["gate"] = "incomplete"
    elif (out["decision_agreement"] >= GATE_AGREEMENT and out["covered_escalations"] == 0
          and out["urgent_on_not_urgent"] == 0 and out["invalid_answers"] == 0):
        out["gate"] = "pass"
    else:
        out["gate"] = "fail"
    return out


def plan_batches(cases: Sequence[Mapping[str, Any]], batch_size: int) -> list[list[Mapping[str, Any]]]:
    """Batches of golden cases for the live run. A batch is one person at one
    moment, so cases share a batch only when role and `now` are equal; each
    holds at most `batch_size` cases (the configured maximum is 1 to
    MAX_BATCH_SIZE, the ceiling config.py allows for assistant_triage_batch_max)."""
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for case in cases:
        groups.setdefault((case["role"], case["now"]), []).append(case)
    batches: list[list[Mapping[str, Any]]] = []
    for group in groups.values():
        for i in range(0, len(group), batch_size):
            batches.append(group[i:i + batch_size])
    return batches


def merge_inputs(batch: Sequence[Mapping[str, Any]],
                 holidays: Iterable[str] = ()) -> tuple[list[dict], dict, str, datetime]:
    """One `(rows, facts, role, now)` for a batch: rows in batch order (the
    pack's keys t1..tn follow that order), facts merged. Every case in a batch
    has the same role and clock, so the shared inputs agree."""
    rows: list[dict] = []
    merged: dict[str, Any] = {}
    role, now = batch[0]["role"], None
    for case in batch:
        r, facts, _role, now = to_service_inputs(case, holidays)
        rows.extend(r)
        for key, value in facts.items():
            if key == "holidays":
                merged[key] = value
            elif isinstance(value, dict):
                merged.setdefault(key, {}).update(value)
            else:
                merged.setdefault(key, []).extend(value)
    return rows, merged, role, now


# ── The live run ─────────────────────────────────────────────────────────────


def supabase_ref(url: str | None) -> str:
    host = (urlsplit((url or "").strip()).hostname or "").lower()
    label, _, rest = host.partition(".")
    return label if rest == "supabase.co" else ""


def parse_cap(raw: Any) -> Decimal:
    try:
        cap = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise Refused(f"{CAP_ENV} is not a number.") from None
    if not cap.is_finite() or cap <= 0:
        raise Refused(f"{CAP_ENV} must be above 0.")
    if cap > MAX_TEST_CAP:
        raise Refused(f"{CAP_ENV} must be at most {MAX_TEST_CAP} (owner question 22 (a)).")
    return cap


def require_live_guards(settings: Any, environ: Mapping[str, str]) -> Decimal:
    """Refuse the live run unless every guard holds. Pure: it reads only the
    settings object and the environment it is given. Checked in an order that
    spends nothing and imports nothing before the cap is known."""
    cap = parse_cap(environ.get(CAP_ENV, ""))
    url = str(getattr(settings, "supabase_url", "") or "")
    if PROD_REF in url:
        raise Refused("SUPABASE_URL is the production project. The live eval runs only against dev.")
    if supabase_ref(url) != DEV_REF:
        raise Refused(f"SUPABASE_URL is not the dev project {DEV_REF}.")
    honoured = getattr(settings, "assistant_triage_test_spend_cap_usd", None)
    if honoured is None:
        raise Refused("The settings do not carry assistant_triage_test_spend_cap_usd, so classify would not enforce "
                      "the cap (the setting arrives with WI-6). Refusing.")
    try:
        if Decimal(str(honoured)) != cap:
            raise Refused("The settings cap differs from the environment cap. Refusing.")
    except InvalidOperation:
        raise Refused("The settings cap is not a number. Refusing.") from None
    return cap


# Failure classes by name (the closed set of plan 5.4; the test cap error is
# matched by name so a fake classifier in a test can raise its own).
ANSWER_QUALITY_ERRORS = ("TriageBadAnswer", "TriageTruncated")  # billed; graded as invalid answers
STOP_ERRORS = {  # the run cannot go on: error class -> `stopped` value
    "TriageTestCap": "cap",
    "TriageCapRefused": "company_cap",
    "TriageUnpriced": "unpriced",
    "TriageSpendCheck": "spend_check",
}
RETRY_ONCE_ERRORS = ("TriageBusy", "TriageTimeout")  # infrastructure; one more try
# Everything else (TriageModelError and any unknown exception) is infrastructure:
# the batch stays ungraded and the pass is `incomplete`, never `fail`.


def _pass_summary(graded: Sequence[Mapping[str, Any]], calls: list[dict], total: int, size: int,
                  infra: Mapping[str, int]) -> dict:
    summary = summarize(graded, total)
    summary["batch_size_setting"] = size
    summary["infrastructure_errors"] = dict(infra)
    summary["calls"] = list(calls)
    summary["usage_by_batch_size"] = usage_by_batch_size(calls)
    summary["results"] = list(graded)
    return summary


def sources_sha256() -> str | None:
    """The digest scripts/check_eval_report.py compares a report against, from
    its own `triage_sources_hash` so the two recipes can never drift. None when
    that script cannot be loaded (the gate then rejects the report)."""
    try:
        import importlib.util

        script = HERE.parents[2] / "scripts" / "check_eval_report.py"
        spec = importlib.util.spec_from_file_location("check_eval_report_for_triage_runner", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.triage_sources_hash()
    except Exception:  # noqa: BLE001 - class name only is never needed: the gate reports the missing digest
        return None


def run_metadata(settings: Any) -> dict:
    """started_at (before the first call), sources_sha256, model and batch_max:
    the fields scripts/check_eval_report.py --require-triage checks for freshness
    and for the configured model and batch maximum."""
    return {"started_at": datetime.now(UTC).isoformat(), "sources_sha256": sources_sha256(),
            "model": getattr(settings, "assistant_triage_model", None),
            "batch_max": getattr(settings, "assistant_triage_batch_max", None)}


def _assemble(passes: list[dict], stopped: str | None, cap: Decimal, spent: Decimal, *, partial: bool,
              subset: Sequence[str] | None, meta: Mapping[str, Any] | None = None) -> dict:
    complete = bool(passes) and not stopped and not partial and not subset
    if complete and all(p["gate"] == "pass" for p in passes):
        gate = "pass"
    elif stopped or partial or subset or any(p["gate"] == "incomplete" for p in passes):
        gate = "incomplete"
    else:
        gate = "fail"
    report = {"gate": gate, "stopped": stopped, "cap_usd": float(cap), "spent_usd": float(spent),
              "subset": bool(subset), "partial": partial, "passes": passes}
    if subset:
        report["subset_ids"] = list(subset)
    if meta:
        report.update(meta)
    return report


def run_live(cases: list[dict], *, builder: PackBuilder, classify: Callable[..., Any],
             usage_for_run: Callable[[str], Mapping[str, Any] | None], settings: Any, cap: Decimal,
             batch_sizes: Sequence[int] = (10,), holidays: Iterable[str] = (),
             total_cases: int | None = None, subset: Sequence[str] | None = None,
             on_progress: Callable[[dict], None] | None = None) -> dict:
    """Run the golden set through `classify`, one pass per batch size.

    `classify(run_id, pack, settings=settings)` returns a result whose
    `.answer` is the parsed answer (`{"items": [...]}`), like the real
    `assistant_triage_model.TriageResult`; a plain mapping is accepted too. `usage_for_run(run_id)` returns the summed token
    counts and cost of the calls logged under that run id. The runner stops
    when its own running cost reaches `cap`; the report is then `incomplete`.

    Failure handling splits by class name: TriageBadAnswer and TriageTruncated
    are answer-quality failures and grade as invalid answers; the company cap,
    the spend check and an unpriced model stop the run; busy and timeout are
    retried once; every other failure is infrastructure and leaves the batch
    ungraded. `total_cases` is the size of the whole golden set (so a run over
    a subset can never pass); `subset` lists the ids of that subset.
    `on_progress` receives a partial report after every call.
    """
    total = total_cases if total_cases is not None else len(cases)
    meta = run_metadata(settings)
    passes: list[dict] = []
    spent = Decimal(0)
    stopped: str | None = None

    def progress(current: dict) -> None:
        if on_progress is not None:
            on_progress(_assemble([*passes, current], stopped, cap, spent, partial=True, subset=subset, meta=meta))

    for size in batch_sizes:
        graded: list[dict] = []
        calls: list[dict] = []
        infra: dict[str, int] = {}
        for batch in plan_batches(cases, size):
            if spent >= cap:
                stopped = "cap"
                break
            try:
                rows, facts, role, now = merge_inputs(batch, holidays)
                pack = builder(rows, facts, role, now=now)
            except Exception as exc:  # noqa: BLE001 - class name only
                stopped = "builder_error"
                calls.append(_call_record(len(batch), exc.__class__.__name__, {}, Decimal(0)))
                break
            answer: Mapping[str, Any] | None = None
            error: str | None = None
            for attempt in (1, 2):
                run_id = str(uuid.uuid4())
                try:
                    result = classify(run_id, pack, settings=settings)
                    answer, error = getattr(result, "answer", result), None
                except Exception as exc:  # noqa: BLE001 - class name only: the message could echo a route or key
                    answer, error = None, exc.__class__.__name__
                usage_failed = False
                try:
                    usage = dict(usage_for_run(run_id) or {})
                except Exception:  # noqa: BLE001 - the spend of this call is unknown: stop rather than guess
                    usage, usage_failed = {}, True
                cost = Decimal(str(usage.get("cost_usd") or 0))
                spent += cost
                calls.append(_call_record(len(batch), error, usage, cost, attempt=attempt))
                progress(_pass_summary(graded, calls, total, size, infra))
                if usage_failed:
                    stopped = "usage_read"
                    break
                if error in RETRY_ONCE_ERRORS and attempt == 1 and spent < cap:
                    continue
                break
            if stopped:
                break
            if error in STOP_ERRORS:
                stopped = STOP_ERRORS[error]
                break
            if error is not None and error not in ANSWER_QUALITY_ERRORS:
                infra[error] = infra.get(error, 0) + 1  # ungraded: the pass will be incomplete
                continue
            by_key = {}
            for entry in (answer or {}).get("items", []) if isinstance(answer, Mapping) else []:
                if isinstance(entry, Mapping) and entry.get("key") not in by_key:
                    by_key[entry.get("key")] = entry
            offered = {it["key"]: it.get("allowed_actions") for it in pack["items"]}
            for i, case in enumerate(batch, start=1):
                graded.append(grade_case(case, by_key.get(f"t{i}"), offered.get(f"t{i}")))
            progress(_pass_summary(graded, calls, total, size, infra))
        passes.append(_pass_summary(graded, calls, total, size, infra))
        if stopped:
            break
    return _assemble(passes, stopped, cap, spent, partial=False, subset=subset, meta=meta)


def _call_record(batch_size: int, error: str | None, usage: Mapping[str, Any], cost: Decimal, *,
                 attempt: int = 1) -> dict:
    return {
        "batch_size": batch_size, "error": error, "attempt": attempt,
        "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"), "cost_usd": float(cost),
    }


def usage_by_batch_size(calls: Sequence[Mapping[str, Any]]) -> dict[str, dict]:
    """Per batch size: call count, items, and the average output and reasoning
    tokens per call and per item (plan 5.4: WI-6 confirms or lowers the
    1200 + 200 x items output allowance from these; P8L-3 raised it from 400). Every billed call counts
    in the averages, truncated ones included (they used the whole allowance,
    so leaving them out would hide a formula that is too tight); the truncated
    calls are also shown on their own, next to the averages. Calls that failed
    before the model answered (busy, timeout, provider error) are only counted
    as `failed_calls`."""
    out: dict[str, dict] = {}
    for size in sorted({c["batch_size"] for c in calls}):
        sized = [c for c in calls if c["batch_size"] == size]
        group = [c for c in sized if c["error"] is None or c["error"] in ANSWER_QUALITY_ERRORS]
        truncated = [c for c in sized if c["error"] == "TriageTruncated"]
        if not group:
            continue

        def avg(rows: Sequence[Mapping[str, Any]], key: str) -> float | None:
            vals = [c[key] for c in rows if c.get(key) is not None]
            return (sum(vals) / len(vals)) if vals else None

        reasoning, output = avg(group, "reasoning_tokens"), avg(group, "output_tokens")
        out[str(size)] = {
            "calls": len(group),
            "items": size * len(group),
            "failed_calls": len(sized) - len(group),
            "avg_output_tokens": output,
            "avg_reasoning_tokens": reasoning,
            "output_tokens_per_item": (output / size) if output is not None else None,
            "reasoning_tokens_per_item": (reasoning / size) if reasoning is not None else None,
            "truncated_calls": len(truncated),
            "truncated_avg_output_tokens": avg(truncated, "output_tokens"),
            "truncated_avg_reasoning_tokens": avg(truncated, "reasoning_tokens"),
        }
    return out


def default_usage_reader() -> Callable[[str], dict]:
    from app.core.supabase_client import get_supabase

    sb = get_supabase()

    def read(run_id: str) -> dict:
        rows = (sb.table("llm_call_log")
                .select("input_tokens,cached_tokens,output_tokens,reasoning_tokens,cost_usd")
                .eq("assistant_run_id", run_id).execute().data or [])
        total: dict[str, Any] = {}
        for row in rows:
            for key in ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "cost_usd"):
                if row.get(key) is not None:
                    total[key] = Decimal(str(total.get(key, 0))) + Decimal(str(row[key]))
        return {k: (float(v) if k == "cost_usd" else int(v)) for k, v in total.items()}

    return read


def default_claimed_runs_reader() -> Callable[[], int]:
    """A reader for how many `assistant_triage_runs` rows are `claimed` (the
    same test the WI-16 tick script uses before it starts)."""
    from app.core.supabase_client import get_supabase

    sb = get_supabase()

    def read() -> int:
        rows = sb.table("assistant_triage_runs").select("id").eq("status", "claimed").limit(1).execute().data or []
        return len(rows)

    return read


def require_no_claimed_runs(read_claimed: Callable[[], int]) -> None:
    """Plan 5.4 accepts that the test-cap check is a read then a reserve (not
    atomic) only because exactly one triage process runs. Refuse while any run
    is claimed, and when the table cannot be read (fail closed)."""
    try:
        claimed = read_claimed()
    except Exception as exc:  # noqa: BLE001 - class name only
        raise Refused(f"Cannot read assistant_triage_runs ({exc.__class__.__name__}). Refusing.") from None
    if claimed:
        raise Refused("A triage run is claimed on this database. Stop the other triage process (the loop on 5071 "
                      "and any tick script) and wait for its lease to end. Refusing.")


def default_report_path() -> str:
    """Outside the repo: a stray report must never land in a tree other work is editing."""
    return str(Path(tempfile.gettempdir()) / "bdr_triage_evals_report.json")


def write_report(path: str, report: Mapping[str, Any]) -> None:
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def live_main(args: argparse.Namespace) -> int:
    # Guard 1 happens before anything heavy is imported or any client is made.
    try:
        parse_cap(os.environ.get(CAP_ENV, ""))
        try:
            from app.core.config import get_settings

            settings = get_settings()
        except Exception as exc:  # noqa: BLE001 - class name only: a validation error can echo setting values
            raise Refused(f"The settings did not load or were refused ({exc.__class__.__name__}). "
                          f"{CAP_ENV} also needs ASSISTANT_TRIAGE_USER_ALLOWLIST.") from None
        cap = require_live_guards(settings, os.environ)
        sizes = [int(s) for s in str(args.batch_sizes).split(",") if s.strip()]
        if not sizes or any(not 1 <= s <= MAX_BATCH_SIZE for s in sizes):
            raise Refused(f"--batch-sizes must be numbers from 1 to {MAX_BATCH_SIZE}.")
        data = load_json(CASES)
        cases = validate_cases(data)
        total_cases, subset = len(cases), None
        if args.only:
            wanted = {s.strip() for s in args.only.split(",") if s.strip()}
            unknown = wanted - {c["id"] for c in cases}
            if unknown:
                raise Refused(f"--only names unknown cases: {sorted(unknown)}.")
            cases = [c for c in cases if c["id"] in wanted]
            if not cases:
                raise Refused("--only matched no case.")
            subset = [c["id"] for c in cases]
        try:
            builder = load_pack_builder()
            from app.services import assistant_triage_model
        except BuilderUnavailable as exc:
            raise Refused(f"Cannot build packs: {exc}") from None
        require_no_claimed_runs(default_claimed_runs_reader())
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    def save(partial: dict) -> None:
        try:
            write_report(args.report, partial)
        except OSError:
            pass  # the final write below reports a path problem

    try:
        report = run_live(cases, builder=builder, classify=assistant_triage_model.classify,
                          usage_for_run=default_usage_reader(), settings=settings, cap=cap, batch_sizes=sizes,
                          holidays=data["holidays"], total_cases=total_cases, subset=subset, on_progress=save)
    except KeyboardInterrupt:
        print(f"interrupted: the partial report so far is in {args.report}", file=sys.stderr)
        return 130
    write_report(args.report, report)
    print(json.dumps({k: report[k] for k in ("gate", "stopped", "subset", "cap_usd", "spent_usd")}))
    for p in report["passes"]:
        keys = ("batch_size_setting", "graded", "decision_agreement", "severity_agreement", "covered_escalations",
                "urgent_on_not_urgent", "invalid_answers", "infrastructure_errors", "gate")
        print(json.dumps({k: p[k] for k in keys}))
    print(f"report: {args.report}")
    return 0 if report["gate"] == "pass" else 1


def injection_main() -> int:
    try:
        builder = load_pack_builder()
        report = run_injection(validate_injection(load_json(INJECTION)), builder)
    except BuilderUnavailable as exc:
        print(f"incomplete: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({k: report[k] for k in ("cases", "passed", "failed", "pass_rate", "gate")}))
    for r in report["results"]:
        if not r["pass"]:
            print(json.dumps(r))
    return 0 if report["gate"] == "pass" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 8 triage evals")
    parser.add_argument("--check", action="store_true", help="validate both files offline (default)")
    parser.add_argument("--injection", action="store_true", help="offline injection tier through triage.build_pack")
    parser.add_argument("--live", action="store_true", help=f"real model calls; refuses unless {CAP_ENV} is set")
    parser.add_argument("--report", default=default_report_path(), help="where the live report goes (default: system temp dir)")
    parser.add_argument("--batch-sizes", default="10", help="comma list, one full pass per size (default 10)")
    parser.add_argument("--only", help="comma-separated golden case ids")
    args = parser.parse_args(argv)
    if args.live:
        return live_main(args)
    if args.injection:
        return injection_main()
    print(json.dumps(check(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

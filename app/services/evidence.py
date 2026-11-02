"""Evidence reads for Executive and IT Admin (docs/ASSISTANT_PHASE4_PLAN.md 3.8).

Created as a stub by WI-1 (Wave 0) and filled by WI-3. The names and the
fixed parameters below are binding for WI-9b (`app/routers/analytics_evidence.py`)
and WI-4 (the xlsx builders); only keyword parameters with defaults and
private helpers may be added.

Common row shape: `{at, at_pt, source, kind, actor {id, name, kind
("human"|"assistant"|"system"), label}, summary, why, ref {table, id}}`.
Every response carries `truncated` and `caps {source: cap}`. Timestamps are
UTC ISO plus `at_pt` ("2026-10-21 02:00 PM PT"). `since` and `until` are
Pacific dates (`until` inclusive); default window
`ASSISTANT_EVIDENCE_DEFAULT_DAYS` (90), maximum `ASSISTANT_EVIDENCE_MAX_DAYS`
(400).

Rules every read applies (so the GET routes, the sensitive POSTs and the
xlsx builders share them):
- `audit_log` rows whose action starts with `cp.` or `cp_report.` keep
  action, entity, entity id and time only; `payload` is dropped before any
  summary is built (decision 19). `_audit_view` is the one place that reads
  an audit row, and it drops the payload first.
- Rows the person's assistant wrote (`actor_kind='assistant'`), `assistant.*`
  audit actions and `assistant.*` bell types are labeled as the assistant's
  and never counted as the person's own actions.
- No email bodies, no note bodies; no em dash in any `why` or caveat text.
- Taint by default (3.8): `has_free_text` is the whitelist rule. Any string
  value under a key outside `TAINT_SAFE_KEYS` (or the safe suffixes) means
  the payload carries free text. The function is pure so a trimmer can copy
  it; tools never import this module.

Every function here is plain `def` and takes the sync Supabase client `sb`.
`file_access.access_report` and `notification_log.build` read through their
own `get_supabase()`, which is the same client in production.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.core import provenance
from app.core.config import get_settings
from app.core.roles import Role
from app.services import file_access, notification_log

logger = logging.getLogger(__name__)

PT = ZoneInfo("America/Los_Angeles")

# Per-person views of `person_evidence` (3.8.6, deviation D4).
VIEWS = ("activity", "file_access", "notifications", "ai_usage")

# Source keys of the project timeline (3.8.1).
TIMELINE_SOURCES = (
    "stage", "pm_stage", "audit", "file_access", "notifications", "email", "proposal_sends",
    "estimator", "auto_send", "outcome", "opens", "markers",
)

# Non-column field groups of the change history (3.8.3); any `projects`
# column name is accepted too.
CHANGE_FIELD_GROUPS = ("pricing", "quotes", "markup", "labor", "proposal", "files")

# `group_by` values of the AI usage aggregate (3.8.4).
AI_USAGE_GROUPS = ("feature", "model", "day")


class EvidenceError(ValueError):
    """A refused evidence read. `code` is one of the section 5.4 codes the
    route maps (`view_invalid`, `window_invalid`, `range_invalid`,
    `assignment_required`, `approval_mismatch`) or `not_found` (404,
    byte-identical to an unknown id). WI-3 adds four 422 codes for bad
    arguments: `sources_invalid`, `field_invalid`, `group_by_invalid`,
    `cursor_invalid`."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


# ── caps ──────────────────────────────────────────────────────────────────

# Rows read per source per request. Hitting one sets `truncated`.
PERSON_SOURCE_CAP = 1000
TIMELINE_CAPS = {
    "stage": 500, "pm_stage": 500, "audit": 1000, "file_access": 1000,
    "notifications": notification_log.MAX_NOTIFICATIONS, "email": notification_log.MAX_EMAILS,
    "proposal_sends": 500, "estimator": 500, "auto_send": 200, "outcome": 10, "opens": 2000,
    "markers": 10,
}
CHANGE_CAPS = {"project_update": 2000, "audit": 1000, "stage": 500}
OPENS_CAP = 20000
AI_USAGE_CAP = 20000
AI_USAGE_JOB_CAP = 5000
AE_RUNS_CAP = 200
ESTIMATOR_CAP = 500
_PAGE = 1000
_ID_CHUNK = 200

TIMELINE_LIMIT_MAX = 200
CHANGE_LIMIT_MAX = 200
PERSON_LIMIT_MAX = 500

SUMMARY_CLIP = 200
MESSAGE_CLIP = 300

# ── fixed server text ─────────────────────────────────────────────────────

# One fixed sentence per row kind: why the record exists and what it does not
# prove. Every row carries one (a test pins the coverage and the absence of
# em dashes).
WHY: dict[str, str] = {
    "stage_advanced": "Recorded when the bidding lane moved to a new stage.",
    "pm_stage_changed": "Recorded when the project management stage changed.",
    "audit": "From the audit log, which records each saved change and who made it.",
    "audit_payroll": "Payroll audit rows show the action and time only; details stay in certified payroll.",
    "file_view": "Logged when BDR opened the file for this person. It shows the open, not reading time.",
    "file_download": "Logged when BDR handed this person a download of the file.",
    "file_export": "Logged once per file when the file went into a ZIP export.",
    "bell": "The bell shows when they opened the notice, not the email.",
    "email": "Graph accepted the email. BDR has no read receipts.",
    "proposal_send": "A proposal transmission to a GC, from the send record.",
    "estimator_assigned": "Recorded when the estimator was assigned to the project.",
    "estimator_revoked": "Recorded when the estimator's access was revoked.",
    "estimator_returned": "Recorded when the estimator returned their deliverables.",
    "estimator_round_submitted": "Recorded when the estimator pressed Send on a round.",
    "estimator_files_sent": "A file package or update was sent to the estimator.",
    "auto_send_run": "An auto send run for this project's internal bid date.",
    "auto_send_accepted": "Someone accepted the auto sent numbers into the project.",
    "outcome_recorded": "The win or loss result recorded for the bid.",
    "project_opened": "Counts throttled project page visits per person per Pacific day.",
    "abandoned": "The project was abandoned and its lanes froze.",
    "failed_to_bid": "The project was marked failed to bid with a coded reason.",
    "deleted": "The project was deleted; rows stored with it were removed with it.",
    "assigned": "When the assignment row was created.",
    "files_emailed": "Graph accepted the email. BDR has no read receipts.",
    "bell_assigned": "The bell shows when they opened the notice, not the email.",
    "bell_files_updated": "The bell shows when they opened the notice, not the email.",
    "first_file_open": "The first time BDR served a project file to the estimator in the portal.",
    "log_view": "The estimator opened the file send log in the portal.",
    "round_submitted": "Recorded when the estimator pressed Send on a round.",
    "due": "The due date set on the assignment, not an event.",
    "returned": "Recorded when the estimator returned their deliverables.",
    "revoked": "Recorded when the estimator's access was revoked.",
    "access_expires": "The date the estimator's access ends, not an event.",
    "email_sent": "Graph accepted the email. BDR has no read receipts.",
    "todo_created": "A to do this person created. Titles are clipped.",
    "estimator_note": "A note this person posted to the estimator thread; the body is not shown.",
    "rfp_review": "This person's yes or no review of an RFP email.",
    "ai_usage_day": "AI calls made for this person, summed per feature and Pacific day.",
    "ae_run": "An AI Estimating run this person created, with its recorded cost.",
    "project_update": "Rebuilt from saved project edits; the earlier value is known only when the trail has it.",
    "pricing_change": "From the pricing audit, which stores the old and new figure.",
    "quote_select": "A vendor quote was selected for a category.",
    "quote_deselect": "The selected quote was cleared for a category.",
    "markup_set": "Markup was saved; the audit stores no figures for it.",
    "labor_review": "The labor numbers were saved or verified.",
    "proposal": "From the proposal audit trail.",
    "file_upload": "A project file was uploaded.",
    "file_delete": "A project file was deleted.",
}

OPENS_CAVEATS = (
    "Accountant opens are not recorded.",
    "Open tracking starts with migration 0117; older visits are not in it.",
    "One row is one throttled visit, so quick repeat visits count once.",
    "Estimator portal page views are not tracked here.",
)
# file.upload / file.delete audit rows carry payload.project_id from 2026-10-24
# on; an older file deleted before anyone opened it is in no source.
FILE_TRAIL_CAVEATS = (
    "Uploads and deletions of files removed before 2026-10-24 that nobody ever opened cannot be traced.",
)
AI_RETENTION_NOTE = "AI call history is kept 90 days."
DELETED_NOTE = (
    "This project was deleted. Stage, notice, send, estimator, outcome and open history "
    "was stored with it and is gone; audit and file access rows remain."
)
DELETED_GONE_SOURCES = (
    "stage", "pm_stage", "notifications", "email", "proposal_sends", "estimator", "auto_send",
    "outcome", "opens",
)

# ── taint whitelist (3.8) ─────────────────────────────────────────────────

# Keys whose string values are ids, enums, dates or server-fixed text. Any
# other key holding a string marks the payload as carrying free text.
TAINT_SAFE_KEYS = frozenset({
    "id", "at", "at_pt", "source", "kind", "status", "action", "field", "view", "role",
    "before_known", "since", "until", "day", "date", "next_cursor", "cursor", "group_by",
    "feature", "model", "channel", "type", "category", "entity", "table", "stage",
    "from_stage", "to_stage", "decision", "result", "via", "trigger", "audience", "code",
    "email_status", "why", "caveats", "retention_note", "notice",
})
TAINT_SAFE_SUFFIXES = (
    "_id", "_ids", "_at", "_at_pt", "_pt", "_date", "_status", "_kind", "_count", "_usd",
    "_tokens",
)


def _safe_key(key: Any) -> bool:
    k = str(key)
    return k in TAINT_SAFE_KEYS or k.endswith(TAINT_SAFE_SUFFIXES)


def free_text_paths(value: Any, *, extra_safe: frozenset[str] = frozenset(), _path: str = "",
                    _key: str | None = None) -> list[str]:
    """Dotted paths of every string that sits under a key outside the
    whitelist. A string at the top level or directly in a top-level list
    counts as free text. Lists inherit their parent key."""
    out: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            out += free_text_paths(v, extra_safe=extra_safe, _path=f"{_path}.{k}" if _path else str(k),
                                   _key=str(k))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            out += free_text_paths(v, extra_safe=extra_safe, _path=f"{_path}[{i}]", _key=_key)
    elif isinstance(value, str) and value:
        if _key is None or not (_safe_key(_key) or _key in extra_safe):
            out.append(_path or "$")
    return out


def has_free_text(value: Any, *, extra_safe: frozenset[str] = frozenset()) -> bool:
    """The 3.8 whitelist taint rule: True when any string value sits under a
    key outside `TAINT_SAFE_KEYS` and `TAINT_SAFE_SUFFIXES` (plus
    `extra_safe`, for server-built keys such as a report's `title`)."""
    return bool(free_text_paths(value, extra_safe=extra_safe))


# ── time ──────────────────────────────────────────────────────────────────


def _parse(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min, tzinfo=PT)
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(value: Any) -> str | None:
    dt = _parse(value)
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def pt(value: Any) -> str | None:
    """A timestamp rendered in America/Los_Angeles as
    "2026-10-21 02:00 PM PT"; None for None."""
    dt = _parse(value)
    if dt is None:
        return None
    return dt.astimezone(PT).strftime("%Y-%m-%d %I:%M %p PT")


def _today_pt() -> date:
    return datetime.now(timezone.utc).astimezone(PT).date()


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(PT).date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise EvidenceError("window_invalid", "Dates are YYYY-MM-DD.") from exc


def _window_dates(since: date | None, until: date | None) -> tuple[date, date]:
    s = get_settings()
    default_days = int(s.assistant_evidence_default_days)
    max_days = int(s.assistant_evidence_max_days)
    since, until = _as_date(since), _as_date(until)
    if until is None:
        until = _today_pt() if since is None else min(
            since + timedelta(days=default_days - 1), max(_today_pt(), since))
    if since is None:
        since = until - timedelta(days=default_days - 1)
    if since > until:
        raise EvidenceError("window_invalid", "since is after until.")
    if (until - since).days + 1 > max_days:
        raise EvidenceError("window_invalid", f"The window is at most {max_days} days.")
    return since, until


def window(since: date | None, until: date | None) -> tuple[datetime, datetime]:
    """The UTC [start, end) bounds of a Pacific date window (`until`
    inclusive, `datetime.combine(d, time.min, tzinfo=PT)`), defaulting and
    capping per the module docstring. Raises EvidenceError("window_invalid")."""
    a, b = _window_dates(since, until)
    start = datetime.combine(a, time.min, tzinfo=PT).astimezone(timezone.utc)
    end = datetime.combine(b + timedelta(days=1), time.min, tzinfo=PT).astimezone(timezone.utc)
    return start, end


def _window_out(since: date | None, until: date | None) -> dict:
    a, b = _window_dates(since, until)
    return {"since": a.isoformat(), "until": b.isoformat()}


def _in_window(value: Any, start: datetime, end: datetime) -> bool:
    dt = _parse(value)
    return dt is not None and start <= dt < end


# ── small helpers ─────────────────────────────────────────────────────────


def _clip(text: Any, n: int) -> str | None:
    if text is None:
        return None
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", str(text)).strip()
    return s if len(s) <= n else s[: n - 3].rstrip() + "..."


def _missing_table(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    text = str(exc)
    return code in ("PGRST205", "42P01") or "PGRST205" in text or "42P01" in text


def _paged(build: Callable[[], Any], cap: int) -> tuple[list[dict], bool]:
    """Up to `cap` rows of a query, paged `_PAGE` at a time with `.range`.
    `build` returns a fresh, ordered query each call. The bool is True when
    more than `cap` rows exist. A missing table reads as empty."""
    rows: list[dict] = []
    want = cap + 1
    start = 0
    try:
        while len(rows) < want:
            end = min(start + _PAGE, want) - 1
            page = build().range(start, end).execute().data or []
            rows.extend(page)
            if len(page) < end - start + 1:
                break
            start = end + 1
    except Exception as exc:  # noqa: BLE001
        if _missing_table(exc):
            return [], False
        raise
    return rows[:cap], len(rows) > cap


def _chunks(values: list, size: int = _ID_CHUNK):
    ordered = sorted({str(v) for v in values if v})
    for i in range(0, len(ordered), size):
        yield ordered[i: i + size]


def _select_in(sb: Any, table: str, select: str, column: str, values: list) -> list[dict]:
    out: list[dict] = []
    try:
        for chunk in _chunks(values):
            out += sb.table(table).select(select).in_(column, chunk).execute().data or []
    except Exception as exc:  # noqa: BLE001
        if _missing_table(exc):
            return []
        raise
    return out


def _people(sb: Any, ids) -> dict[str, dict]:
    rows = _select_in(sb, "profiles", "id, full_name, role, is_active", "id", list(ids))
    return {str(r["id"]): r for r in rows if r.get("id")}


def _project_row(sb: Any, project_id: str) -> dict | None:
    rows = (
        sb.table("projects")
        .select("id, number, name, current_stage, pm_stage, internal_bid_at, actual_bid_at, "
                "abandoned_at, abandoned_by, failed_to_bid_at, failed_to_bid_by, "
                "failed_to_bid_reason, created_at")
        .eq("id", project_id).limit(1).execute()
    ).data or []
    return rows[0] if rows else None


def _deleted_row(sb: Any, project_id: str) -> dict | None:
    try:
        rows = (
            sb.table("deleted_projects")
            .select("id, project_id, project_number, project_name, reason, deleted_by, deleted_at, "
                    "restored_at")
            .eq("project_id", project_id).order("deleted_at", desc=True).limit(1).execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001
        if _missing_table(exc):
            return None
        raise
    return rows[0] if rows else None


def _project_ref(project: dict | None) -> dict | None:
    if not project:
        return None
    return {"id": project.get("id"), "number": project.get("number"), "name": project.get("name")}


def _actor(people: dict[str, dict], actor_id: Any, actor_kind: str | None = None, *,
           action: str | None = None, name: str | None = None) -> dict:
    """`{id, name, kind, label}`. `assistant.*` audit actions are the
    assistant's whatever the row says; a row with no actor is the system's."""
    kind = actor_kind if actor_kind in provenance.ACTOR_KINDS else provenance.ACTOR_HUMAN
    if action and str(action).startswith("assistant."):
        kind = provenance.ACTOR_ASSISTANT
    if not actor_id and not name and kind != provenance.ACTOR_ASSISTANT:
        kind = provenance.ACTOR_SYSTEM
    if kind == provenance.ACTOR_SYSTEM:
        return {"id": str(actor_id) if actor_id else None, "name": None, "kind": kind, "label": "System"}
    full = name or (people.get(str(actor_id)) or {}).get("full_name")
    return {
        "id": str(actor_id) if actor_id else None,
        "name": full,
        "kind": kind,
        "label": provenance.display_actor(full, kind),
    }


def _row(source: str, kind: str, at: Any, actor: dict, summary: str | None, table: str | None,
         ref_id: Any, **extra: Any) -> dict | None:
    iso = _iso(at)
    if iso is None:
        return None
    row = {
        "at": iso,
        "at_pt": pt(iso),
        "source": source,
        "kind": kind,
        "actor": actor,
        "summary": summary,
        "why": WHY.get(kind) or WHY["audit"],
        "ref": {"table": table, "id": str(ref_id) if ref_id is not None else None},
    }
    row.update(extra)
    return row


def _sort_key(row: dict) -> tuple:
    return (row["at"] and _parse(row["at"]), row["source"], str(row["ref"].get("id") or ""))


def _encode_cursor(row: dict) -> str:
    raw = f"{row['at']}|{row['source']}|{row['ref'].get('id') or ''}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        at, source, rid = base64.urlsafe_b64decode(padded.encode()).decode().split("|", 2)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise EvidenceError("cursor_invalid") from exc
    dt = _parse(at)
    if dt is None:
        raise EvidenceError("cursor_invalid")
    return (dt, source, rid)


def _page(rows: list[dict], limit: int, cursor_key: tuple | None) -> tuple[list[dict], str | None]:
    """Newest first by (at, source, id); rows strictly after the cursor."""
    rows = sorted(rows, key=_sort_key, reverse=True)
    if cursor_key is not None:
        rows = [r for r in rows if _sort_key(r) < cursor_key]
    page = rows[:limit]
    nxt = _encode_cursor(page[-1]) if len(rows) > limit and page else None
    return page, nxt


def _clamp(limit: Any, default: int, hi: int) -> int:
    try:
        n = int(limit) if limit is not None else default
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, hi))


def _num(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.000001")))


def _short_hash(*parts: Any) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


# ── audit rows ────────────────────────────────────────────────────────────

_PAYROLL_PREFIXES = ("cp.", "cp_report.")
_ACTION_LABELS: dict[str, str] | None = None


def _action_label(action: str) -> str:
    global _ACTION_LABELS
    if _ACTION_LABELS is None:
        try:
            from app.services.analytics_metrics import ACTIVITY_ACTION_LABELS
            _ACTION_LABELS = dict(ACTIVITY_ACTION_LABELS)
        except Exception:  # noqa: BLE001 - a label is cosmetic
            _ACTION_LABELS = {}
    return _ACTION_LABELS.get(action) or action


def _is_payroll(action: Any) -> bool:
    return str(action or "").startswith(_PAYROLL_PREFIXES)


def _audit_view(row: dict) -> dict:
    """The one reader of an audit row. A payroll action keeps action,
    entity, entity id, time and actor only: the payload never leaves."""
    out = dict(row)
    if _is_payroll(out.get("action")):
        out.pop("payload", None)
        out["payload"] = None
    return out


_AUDIT_SELECT = "id, actor_id, action, entity, entity_id, payload, created_at, actor_kind"


def _audit_summary(row: dict) -> str:
    action = str(row.get("action") or "")
    label = _action_label(action)
    if _is_payroll(action):
        return f"{label} ({row.get('entity') or 'payroll'})"
    payload = row.get("payload")
    if action == "project.update" and isinstance(payload, dict) and payload:
        return f"{label}: {', '.join(sorted(str(k) for k in payload)[:12])}"
    return label


def _audit_row(row: dict, people: dict, source: str = "audit") -> dict | None:
    row = _audit_view(row)
    action = row.get("action")
    kind = "audit_payroll" if _is_payroll(action) else "audit"
    return _row(source, kind, row.get("created_at"),
                _actor(people, row.get("actor_id"), row.get("actor_kind"), action=action),
                _audit_summary(row), "audit_log", row.get("id"), action=action,
                entity=row.get("entity"), entity_id=row.get("entity_id"))


def _child_ids(sb: Any, project_id: str) -> dict[str, list[str]]:
    """Ids of the project's child records whose audit rows name them as the
    entity: files (live plus any seen in file access history), RFQs, their
    quotes and proposal drafts."""
    files = _select_in(sb, "project_files", "id", "project_id", [project_id])
    file_ids = {str(r["id"]) for r in files if r.get("id")}
    seen, _ = _paged(lambda: sb.table("file_access_events").select("file_id")
                     .eq("project_id", project_id).eq("source", "project_file")
                     .order("created_at", desc=True), 5000)
    file_ids |= {str(r["file_id"]) for r in seen if r.get("file_id")}
    rfqs = _select_in(sb, "rfqs", "id", "project_id", [project_id])
    rfq_ids = sorted({str(r["id"]) for r in rfqs if r.get("id")})
    quotes = _select_in(sb, "quotes", "id", "rfq_id", rfq_ids)
    drafts = _select_in(sb, "proposal_drafts", "id", "project_id", [project_id])
    return {
        "project_file": sorted(file_ids),
        "rfq": rfq_ids,
        "quote": sorted({str(r["id"]) for r in quotes if r.get("id")}),
        "proposal_draft": sorted({str(r["id"]) for r in drafts if r.get("id")}),
    }


def _audit_for_entities(sb: Any, project_id: str, children: dict[str, list[str]], start: datetime,
                        end: datetime, cap: int) -> tuple[list[dict], bool]:
    """Audit rows of the project and its children inside [start, end),
    the child ids chunked by `_ID_CHUNK`, newest first, at most `cap`."""
    s, e = start.isoformat(), end.isoformat()
    rows, truncated = _paged(lambda: sb.table("audit_log").select(_AUDIT_SELECT)
                             .eq("entity", "project").eq("entity_id", project_id)
                             .gte("created_at", s).lt("created_at", e)
                             .order("created_at", desc=True).order("id", desc=True), cap)
    for entity, ids in children.items():
        for chunk in _chunks(ids):
            got, more = _paged(lambda entity=entity, chunk=chunk: sb.table("audit_log").select(_AUDIT_SELECT)
                               .eq("entity", entity).in_("entity_id", chunk)
                               .gte("created_at", s).lt("created_at", e)
                               .order("created_at", desc=True).order("id", desc=True), cap)
            rows += got
            truncated = truncated or more
    if "project_file" in children:
        # file.upload / file.delete rows carry payload.project_id, so the rows
        # of a file deleted before anyone opened it (in neither id source of
        # _child_ids) are still found. Older rows rely on the id lookup.
        got, more = _paged(lambda: sb.table("audit_log").select(_AUDIT_SELECT)
                           .eq("entity", "project_file").eq("payload->>project_id", project_id)
                           .gte("created_at", s).lt("created_at", e)
                           .order("created_at", desc=True).order("id", desc=True), cap)
        have = {str(r.get("id")) for r in rows}
        rows += [r for r in got if str(r.get("id")) not in have]
        truncated = truncated or more
    rows.sort(key=lambda r: (_parse(r.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
                             str(r.get("id"))), reverse=True)
    if len(rows) > cap:
        rows, truncated = rows[:cap], True
    return rows, truncated


# ── 3.8.1 project timeline ────────────────────────────────────────────────


def _redact(project: dict, role: str) -> dict:
    from app.routers.projects import redact_for_role  # lazy: routers import this module
    try:
        r = Role(str(role))
    except ValueError:
        return {**project, "actual_bid_at": None}
    return redact_for_role(project, r)


def _stage_rows(sb, project_id, s, e, cap, table):
    rows, more = _paged(lambda: sb.table(table)
                        .select("id, project_id, from_stage, to_stage, actor_id, entered_at, actor_kind")
                        .eq("project_id", project_id).gte("entered_at", s).lt("entered_at", e)
                        .order("entered_at", desc=True).order("id", desc=True), cap)
    return rows, more


def _notification_rows(project_id: str, start: datetime, end: datetime) -> tuple[list[dict], list[dict], bool]:
    """Bell and email events of the project from `notification_log.build`.
    Recipients who are estimators are reduced to "sent to the estimator"
    with no read time or delivery status."""
    log = notification_log.build(project_id)
    bells: list[dict] = []
    emails: list[dict] = []
    for entry in log.get("entries") or []:
        at = entry.get("at")
        if not _in_window(at, start, end):
            continue
        recipients = entry.get("recipients") or []
        internal = [r for r in recipients if r.get("audience") != "estimator"]
        to_estimator = len(internal) != len(recipients)
        sender = entry.get("sent_by") or {}
        actor = _actor({}, None, sender.get("actor_kind"), name=sender.get("name")) if sender else \
            _actor({}, None, provenance.ACTOR_SYSTEM)
        if entry.get("source") == "notification":
            title = entry.get("title") or entry.get("type")
            parts = []
            if internal:
                read = sum(1 for r in internal if r.get("read_at"))
                parts.append(f"to {len(internal)} {'person' if len(internal) == 1 else 'people'}, {read} read")
            if to_estimator:
                parts.append("sent to the estimator")
            row = _row("notifications", "bell", at, actor,
                       _clip(f"Bell: {title} ({'; '.join(parts)})", SUMMARY_CLIP), "notifications",
                       f"bell:{_short_hash(at, entry.get('type'), entry.get('message'))}",
                       type=entry.get("type"), message=_clip(entry.get("message"), MESSAGE_CLIP),
                       counts={"recipients": len(internal), "read": sum(1 for r in internal if r.get("read_at"))},
                       to_estimator=to_estimator)
            if row:
                bells.append(row)
        else:
            subject = entry.get("subject") or entry.get("title")
            parts = []
            if internal:
                sent = sum(1 for r in internal if r.get("email_status") == "sent")
                failed = sum(1 for r in internal if r.get("email_status") == "failed")
                parts.append(f"to {len(internal)} recipient{'s' if len(internal) != 1 else ''}, "
                             f"{sent} sent, {failed} failed")
            if to_estimator:
                parts.append("sent to the estimator")
            row = _row("email", "email", at, actor,
                       _clip(f"Email: {subject} ({'; '.join(parts)})", SUMMARY_CLIP), "email_log",
                       f"email:{_short_hash(at, entry.get('type'), subject)}",
                       type=entry.get("type"), category=entry.get("category"),
                       subject=_clip(subject, SUMMARY_CLIP),
                       counts={
                           "recipients": len(internal),
                           "emailed": sum(1 for r in internal if r.get("email_status") == "sent"),
                           "failed": sum(1 for r in internal if r.get("email_status") == "failed"),
                       },
                       to_estimator=to_estimator)
            if row:
                emails.append(row)
    return bells, emails, bool(log.get("truncated"))


def project_timeline(
    sb: Any, project_id: str, *, role: str, since: date | None = None, until: date | None = None,
    sources: list[str] | None = None, limit: int = 50, cursor: str | None = None,
) -> dict:
    """3.8.1: merged project events newest first, `{project, rows,
    next_cursor, truncated, caps}`. The embedded project goes through
    `projects.redact_for_role(role)`. For estimator subjects no `read_at`,
    email status, first open or per-person open count (those stay in
    `estimator_timeline`). A deleted project resolves through
    `deleted_projects`.

    Also returns `window {since, until}` and, for a deleted project,
    `deleted {deleted_at, at_pt, reason, gone_sources}` plus `notice`.
    `cursor` is the opaque base64 of `at|source|id`; with one, the reads
    stop at the cursor's time so deeper pages stay reachable under the caps."""
    if sources:
        bad = [x for x in sources if x not in TIMELINE_SOURCES]
        if bad:
            raise EvidenceError("sources_invalid", f"Unknown sources: {', '.join(sorted(bad))[:200]}")
        wanted = [x for x in TIMELINE_SOURCES if x in set(sources)]
    else:
        wanted = list(TIMELINE_SOURCES)
    limit = _clamp(limit, 50, TIMELINE_LIMIT_MAX)
    start, end = window(since, until)
    full_end = end
    cursor_key = _decode_cursor(cursor)
    # Opens are grouped per (person, Pacific day), so they are read through
    # the end of the cursor's own day: a group then has the same count and
    # `at` (its last open) on every page, and _page drops the groups an
    # earlier page already showed.
    opens_end = full_end
    if cursor_key is not None:
        end = min(end, cursor_key[0] + timedelta(microseconds=1))
        next_day = (cursor_key[0].astimezone(PT) + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                                              microsecond=0)
        opens_end = min(full_end, next_day)
    s, e = start.isoformat(), end.isoformat()

    project = _project_row(sb, project_id)
    deleted = None
    if project is None:
        gone = _deleted_row(sb, project_id)
        if gone is None:
            raise EvidenceError("not_found")
        deleted = gone
        project_out = {"id": project_id, "number": gone.get("project_number"),
                       "name": gone.get("project_name"), "deleted": True}
        wanted = [x for x in wanted if x not in DELETED_GONE_SOURCES]
    else:
        project_out = _redact(dict(project), role)

    caps = {x: TIMELINE_CAPS[x] for x in wanted}
    truncated = False
    raw: dict[str, list[dict]] = {}
    actor_ids: set[str] = set()

    def note_actors(rows, *cols):
        for r in rows:
            for c in cols:
                if r.get(c):
                    actor_ids.add(str(r[c]))

    if "stage" in wanted:
        raw["stage"], more = _stage_rows(sb, project_id, s, e, caps["stage"], "stage_events")
        truncated |= more
        note_actors(raw["stage"], "actor_id")
    if "pm_stage" in wanted:
        raw["pm_stage"], more = _stage_rows(sb, project_id, s, e, caps["pm_stage"],
                                            "pm_stage_events")
        truncated |= more
        note_actors(raw["pm_stage"], "actor_id")
    if "audit" in wanted:
        children = _child_ids(sb, project_id) if deleted is None else {}
        rows, more = _audit_for_entities(sb, project_id, children, start, end, caps["audit"])
        # The estimator's own portal reads belong to estimator_timeline.
        raw["audit"] = [r for r in rows if r.get("action") != "estimator.log_view"]
        truncated |= more
        note_actors(raw["audit"], "actor_id")
    if "file_access" in wanted:
        rows, more = file_access._fetch_events(
            sb, date_from=start, date_to=end - timedelta(microseconds=1), project_id=project_id,
            actor_kind="human", include_prefetch=False, cap=caps["file_access"])
        raw["file_access"] = [r for r in rows if r.get("actor_role") != Role.ESTIMATOR.value]
        truncated |= more
        note_actors(raw["file_access"], "actor_id")
    if "proposal_sends" in wanted:
        raw["proposal_sends"], more = _paged(
            lambda: sb.table("proposal_send_events")
            .select("id, proposal_send_id, gc_id, kind, via, status, sent_at, sent_by, created_at, actor_kind")
            .eq("project_id", project_id).gte("created_at", s).lt("created_at", e)
            .order("created_at", desc=True).order("id", desc=True), caps["proposal_sends"])
        truncated |= more
        note_actors(raw["proposal_sends"], "sent_by")
    if "outcome" in wanted:
        raw["outcome"], _ = _paged(lambda: sb.table("bid_outcomes")
                                   .select("id, result, recorded_by, recorded_at")
                                   .eq("project_id", project_id).order("recorded_at", desc=True),
                                   caps["outcome"])
        note_actors(raw["outcome"], "recorded_by")
    if "auto_send" in wanted:
        raw["auto_send"], more = _paged(
            lambda: sb.table("auto_send_runs")
            .select("id, bid_date, status, trigger, sent_at, finished_at, created_at, triggered_by, "
                    "accepted_at, accepted_by")
            .eq("project_id", project_id).order("created_at", desc=True), caps["auto_send"])
        truncated |= more
        note_actors(raw["auto_send"], "triggered_by", "accepted_by")
    est: dict[str, list[dict]] = {}
    if "estimator" in wanted:
        cap = caps["estimator"]
        est["assignments"], m1 = _paged(lambda: sb.table("estimator_assignments")
                                        .select("id, estimator_id, assigned_by, created_at, revoked_at, "
                                                "returned_at")
                                        .eq("project_id", project_id).order("created_at", desc=True), cap)
        est["submissions"], m2 = _paged(lambda: sb.table("estimator_submissions")
                                        .select("id, estimator_id, round, submitted_at")
                                        .eq("project_id", project_id).order("submitted_at", desc=True), cap)
        est["batches"], m3 = _paged(lambda: sb.table("file_send_batches")
                                    .select("id, kind, sent_by, sent_at, summary, reconstructed")
                                    .eq("project_id", project_id).order("sent_at", desc=True), cap)
        truncated |= m1 or m2 or m3
        note_actors(est["assignments"], "estimator_id", "assigned_by")
        note_actors(est["submissions"], "estimator_id")
        note_actors(est["batches"], "sent_by")
    if "opens" in wanted:
        raw["opens"], more = _paged(lambda: sb.table("project_open_events")
                                    .select("id, user_id, kind, opened_at")
                                    .eq("project_id", project_id).gte("opened_at", s)
                                    .lt("opened_at", opens_end.isoformat())
                                    .order("opened_at", desc=True).order("id", desc=True), caps["opens"])
        truncated |= more
        note_actors(raw["opens"], "user_id")
    if "markers" in wanted and project is not None:
        note_actors([project], "abandoned_by", "failed_to_bid_by")
    if deleted is not None:
        note_actors([deleted], "deleted_by")

    people = _people(sb, actor_ids)
    rows: list[dict] = []

    def add(row):
        if row is not None and _in_window(row["at"], start, end):
            rows.append(row)

    for src, kind in (("stage", "stage_advanced"), ("pm_stage", "pm_stage_changed")):
        for r in raw.get(src, []):
            add(_row(src, kind, r.get("entered_at"), _actor(people, r.get("actor_id"), r.get("actor_kind")),
                     f"{r.get('from_stage') or 'start'} to {r.get('to_stage')}",
                     "stage_events" if src == "stage" else "pm_stage_events", r.get("id"),
                     from_stage=r.get("from_stage"), to_stage=r.get("to_stage")))
    for r in raw.get("audit", []):
        add(_audit_row(r, people))
    for r in raw.get("file_access", []):
        action = r.get("action") or "view"
        add(_row("file_access", f"file_{action}" if f"file_{action}" in WHY else "file_view",
                 r.get("created_at"), _actor(people, r.get("actor_id"), r.get("actor_kind")),
                 _clip(f"{action.capitalize()}: {r.get('filename') or 'a file'}", SUMMARY_CLIP),
                 "file_access_events", r.get("id"), action=action, filename=_clip(r.get("filename"), 200),
                 category=r.get("category"), file_id=r.get("file_id")))
    if "notifications" in wanted or "email" in wanted:
        bells, mails, more = _notification_rows(project_id, start, end)
        truncated |= more
        if "notifications" in wanted:
            for r in bells:
                add(r)
        if "email" in wanted:
            for r in mails:
                add(r)
    if raw.get("proposal_sends"):
        gcs = {str(g["id"]): g.get("name") for g in _select_in(
            sb, "general_contractors", "id, name", "id", [r.get("gc_id") for r in raw["proposal_sends"]])}
        for r in raw["proposal_sends"]:
            gc = gcs.get(str(r.get("gc_id"))) or "a GC"
            add(_row("proposal_sends", "proposal_send", r.get("sent_at") or r.get("created_at"),
                     _actor(people, r.get("sent_by"), r.get("actor_kind")),
                     _clip(f"Proposal {r.get('kind') or 'send'} to {gc} by {r.get('via') or 'email'}: "
                           f"{r.get('status')}", SUMMARY_CLIP),
                     "proposal_send_events", r.get("id"), status=r.get("status"), via=r.get("via"),
                     gc_id=r.get("gc_id"), gc_name=gc))
    for r in raw.get("outcome", []):
        add(_row("outcome", "outcome_recorded", r.get("recorded_at"), _actor(people, r.get("recorded_by")),
                 f"Bid result recorded: {r.get('result')}", "bid_outcomes", r.get("id"), result=r.get("result")))
    for r in raw.get("auto_send", []):
        add(_row("auto_send", "auto_send_run", r.get("sent_at") or r.get("finished_at") or r.get("created_at"),
                 _actor(people, r.get("triggered_by"), provenance.ACTOR_HUMAN if r.get("triggered_by")
                        else provenance.ACTOR_SYSTEM),
                 f"Auto send ({r.get('trigger') or 'scheduled'}) for bid date {r.get('bid_date')}: {r.get('status')}",
                 "auto_send_runs", r.get("id"), status=r.get("status"), trigger=r.get("trigger")))
        if r.get("accepted_at"):
            add(_row("auto_send", "auto_send_accepted", r.get("accepted_at"), _actor(people, r.get("accepted_by")),
                     "Auto sent numbers accepted into the project", "auto_send_runs", f"{r.get('id')}:accepted"))
    for r in est.get("assignments", []):
        who = _actor(people, r.get("estimator_id"))
        add(_row("estimator", "estimator_assigned", r.get("created_at"), _actor(people, r.get("assigned_by")),
                 _clip(f"Assigned estimator {who['label']}", SUMMARY_CLIP), "estimator_assignments", r.get("id"),
                 estimator_id=r.get("estimator_id"), assignment_id=r.get("id")))
        if r.get("revoked_at"):
            add(_row("estimator", "estimator_revoked", r.get("revoked_at"), _actor(people, None),
                     _clip(f"Estimator access revoked for {who['label']}", SUMMARY_CLIP), "estimator_assignments",
                     f"{r.get('id')}:revoked", estimator_id=r.get("estimator_id"), assignment_id=r.get("id")))
        if r.get("returned_at"):
            add(_row("estimator", "estimator_returned", r.get("returned_at"), who,
                     _clip(f"{who['label']} returned deliverables", SUMMARY_CLIP), "estimator_assignments",
                     f"{r.get('id')}:returned", estimator_id=r.get("estimator_id"), assignment_id=r.get("id")))
    for r in est.get("submissions", []):
        add(_row("estimator", "estimator_round_submitted", r.get("submitted_at"),
                 _actor(people, r.get("estimator_id")), f"Estimator round {r.get('round')} submitted",
                 "estimator_submissions", r.get("id"), round=r.get("round")))
    for r in est.get("batches", []):
        add(_row("estimator", "estimator_files_sent", r.get("sent_at"), _actor(people, r.get("sent_by")),
                 f"Files sent to the estimator ({r.get('kind') or 'initial'})", "file_send_batches", r.get("id"),
                 batch_kind=r.get("kind")))
    if raw.get("opens"):
        groups: dict[tuple[str, str], dict] = {}
        for r in raw["opens"]:
            uid = str(r.get("user_id") or "")
            if (people.get(uid) or {}).get("role") == Role.ESTIMATOR.value:
                continue
            dt = _parse(r.get("opened_at"))
            if dt is None:
                continue
            key = (uid, dt.astimezone(PT).date().isoformat())
            g = groups.setdefault(key, {"count": 0, "last": dt})
            g["count"] += 1
            g["last"] = max(g["last"], dt)
        for (uid, day), g in groups.items():
            who = _actor(people, uid)
            row = _row("opens", "project_opened", g["last"], who,
                       _clip(f"{who['label']} opened the project {g['count']} "
                             f"time{'s' if g['count'] != 1 else ''} on {day}", SUMMARY_CLIP),
                       "project_open_events", f"{uid}:{day}", day=day, open_count=g["count"])
            # Not add(): its window stops at the cursor, which would drop a
            # group whose last open is later; _page filters by the cursor.
            if row is not None and _in_window(row["at"], start, full_end):
                rows.append(row)
    if "markers" in wanted and project is not None:
        if project.get("failed_to_bid_at"):
            add(_row("markers", "failed_to_bid", project["failed_to_bid_at"],
                     _actor(people, project.get("failed_to_bid_by")),
                     f"Marked failed to bid ({project.get('failed_to_bid_reason') or 'no reason code'})",
                     "projects", f"{project_id}:failed_to_bid", code=project.get("failed_to_bid_reason")))
        elif project.get("abandoned_at"):
            add(_row("markers", "abandoned", project["abandoned_at"], _actor(people, project.get("abandoned_by")),
                     "Project abandoned", "projects", f"{project_id}:abandoned"))
    out: dict[str, Any] = {}
    if deleted is not None:
        if "markers" in wanted:
            add(_row("markers", "deleted", deleted.get("deleted_at"), _actor(people, deleted.get("deleted_by")),
                     f"Project deleted ({deleted.get('reason')})", "deleted_projects", deleted.get("id"),
                     code=deleted.get("reason")))
        out["deleted"] = {
            "deleted_at": _iso(deleted.get("deleted_at")), "at_pt": pt(deleted.get("deleted_at")),
            "reason": deleted.get("reason"), "gone_sources": list(DELETED_GONE_SOURCES),
        }
        out["notice"] = DELETED_NOTE

    page, next_cursor = _page(rows, limit, cursor_key)
    out.update({
        "project": project_out,
        "window": _window_out(since, until),
        "rows": page,
        "next_cursor": next_cursor,
        "truncated": bool(truncated),
        "caps": caps,
    })
    if "audit" in wanted:
        out["caveats"] = list(FILE_TRAIL_CAVEATS)
    return out


# ── 3.8.2 opens ───────────────────────────────────────────────────────────


def project_opens(sb: Any, project_id: str) -> dict:
    """3.8.2: `project_open_events` counts per (user_id, kind) with first and
    last `opened_at` and user names, plus `caveats`. Returns `{project, rows,
    caveats, truncated, caps}`; rows sorted by count, then last open. An id
    that is neither a project nor a deleted project raises not_found."""
    project = _project_row(sb, project_id)
    project_out = _project_ref(project)
    if project is None:
        gone = _deleted_row(sb, project_id)
        if gone is None:
            raise EvidenceError("not_found")
        project_out = {"id": project_id, "number": gone.get("project_number"),
                       "name": gone.get("project_name"), "deleted": True}
    rows, truncated = _paged(lambda: sb.table("project_open_events").select("id, user_id, kind, opened_at")
                             .eq("project_id", project_id).order("opened_at", desc=True).order("id", desc=True),
                             OPENS_CAP)
    groups: dict[tuple[str, str], dict] = {}
    for r in rows:
        dt = _parse(r.get("opened_at"))
        if dt is None:
            continue
        key = (str(r.get("user_id") or ""), str(r.get("kind") or "project"))
        g = groups.setdefault(key, {"count": 0, "first": dt, "last": dt})
        g["count"] += 1
        g["first"] = min(g["first"], dt)
        g["last"] = max(g["last"], dt)
    people = _people(sb, {k[0] for k in groups if k[0]})
    out_rows = []
    for (uid, kind), g in groups.items():
        p = people.get(uid) or {}
        out_rows.append({
            "user_id": uid or None,
            "name": p.get("full_name"),
            "role": p.get("role"),
            "kind": kind,
            "open_count": g["count"],
            "first_opened_at": _iso(g["first"]),
            "first_opened_at_pt": pt(g["first"]),
            "last_opened_at": _iso(g["last"]),
            "last_opened_at_pt": pt(g["last"]),
        })
    out_rows.sort(key=lambda r: (-r["open_count"], r["last_opened_at"] or ""), reverse=False)
    return {
        "project": project_out,
        "rows": out_rows,
        "caveats": list(OPENS_CAVEATS),
        "truncated": truncated,
        "caps": {"opens": OPENS_CAP},
    }


# ── 3.8.3 change history ──────────────────────────────────────────────────

_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_GROUP_ACTIONS = {
    "pricing": lambda a: a.startswith("pricing."),
    "quotes": lambda a: a in ("quote.select", "quote.deselect"),
    "markup": lambda a: a == "markup.set",
    "labor": lambda a: a == "labor.review",
    "proposal": lambda a: a.startswith("proposal."),
    "files": lambda a: a in ("file.upload", "file.delete"),
}
_UNSAFE_PAYLOAD_KEY = re.compile(r"(email|addr|recipient|body|^to$|^cc$|token|secret|password)", re.I)


def _small_payload(payload: Any) -> Any:
    """A short, scalar-only view of an audit payload for `after`: no
    addresses or bodies, strings clipped, at most 12 keys."""
    if not isinstance(payload, dict):
        return None
    out: dict[str, Any] = {}
    for k in sorted(payload)[:24]:
        if _UNSAFE_PAYLOAD_KEY.search(str(k)):
            continue
        v = payload[k]
        if isinstance(v, (int, float, bool)) or v is None:
            out[str(k)] = v
        elif isinstance(v, str):
            out[str(k)] = _clip(v, SUMMARY_CLIP)
        if len(out) >= 12:
            break
    return out or None


def _group_of(action: str) -> str | None:
    for group, test in _GROUP_ACTIONS.items():
        if test(action):
            return group
    return None


def _change_kind(action: str) -> str:
    return {
        "quote.select": "quote_select", "quote.deselect": "quote_deselect", "markup.set": "markup_set",
        "labor.review": "labor_review", "file.upload": "file_upload", "file.delete": "file_delete",
    }.get(action) or ("pricing_change" if action.startswith("pricing.") else "proposal")


def change_history(
    sb: Any, project_id: str, *, field: str | None = None, since: date | None = None,
    until: date | None = None, limit: int = 50, max_limit: int = CHANGE_LIMIT_MAX,
) -> dict:
    """3.8.3: rows `{at, field, before, after, before_known, actor, action}`
    (before reconstructed oldest to newest; `before_known=false` when the
    first value predates the trail).

    Rows also carry the common shape keys (`at_pt`, `source`, `kind`,
    `summary`, `why`, `ref`), newest first. Returns `{project, field, window,
    rows, total, truncated, caps}`; `truncated` is also set when `total`
    exceeds `limit` (there is no cursor here). Amounts are returned
    (EVIDENCE_ROLES are bid amount viewers). Stage rows use the fields
    `current_stage` and `pm_stage`."""
    if field is not None:
        field = str(field).strip()
        if not field:
            field = None
        elif field not in CHANGE_FIELD_GROUPS and not _FIELD_RE.match(field):
            raise EvidenceError("field_invalid")
    # `max_limit` lifts the route's 200-row clamp for the in-process workbook
    # builder (assistant_reports, up to ASSISTANT_REPORT_MAX_ROWS); the
    # CHANGE_CAPS reads still bound the work.
    limit = _clamp(limit, 50, max(int(max_limit or CHANGE_LIMIT_MAX), 1))
    start, end = window(since, until)
    s, e = start.isoformat(), end.isoformat()

    project = _project_row(sb, project_id)
    if project is None:
        gone = _deleted_row(sb, project_id)
        if gone is None:
            raise EvidenceError("not_found")
        project_out = {"id": project_id, "number": gone.get("project_number"),
                       "name": gone.get("project_name"), "deleted": True}
    else:
        project_out = _project_ref(project)

    truncated = False
    want_updates = field is None or field not in CHANGE_FIELD_GROUPS
    want_group = field is None or field in CHANGE_FIELD_GROUPS
    want_stage = field in (None, "current_stage", "pm_stage")

    # project.update over the whole trail (not just the window) so `before`
    # is known for a field edited before the window opened.
    updates: list[dict] = []
    if want_updates:
        updates, more = _paged(lambda: sb.table("audit_log").select(_AUDIT_SELECT)
                               .eq("entity", "project").eq("entity_id", project_id)
                               .eq("action", "project.update")
                               .order("created_at", desc=True).order("id", desc=True),
                               CHANGE_CAPS["project_update"])
        truncated |= more
        updates = [_audit_view(r) for r in updates]
        updates.sort(key=lambda r: (_parse(r.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
                                    str(r.get("id"))))
    others: list[dict] = []
    if want_group:
        children = _child_ids(sb, project_id) if project is not None else {}
        children = {k: v for k, v in children.items() if k in ("project_file", "rfq", "quote", "proposal_draft")}
        rows, more = _audit_for_entities(sb, project_id, children, start, end, CHANGE_CAPS["audit"])
        truncated |= more
        others = [_audit_view(r) for r in rows
                  if _group_of(str(r.get("action") or "")) and not _is_payroll(r.get("action"))]
        if field in CHANGE_FIELD_GROUPS:
            others = [r for r in others if _group_of(str(r.get("action"))) == field]
    stages: list[tuple[str, dict]] = []
    if want_stage and project is not None:
        for table, fname in (("stage_events", "current_stage"), ("pm_stage_events", "pm_stage")):
            if field not in (None, fname):
                continue
            got, more = _stage_rows(sb, project_id, s, e, CHANGE_CAPS["stage"], table)
            truncated |= more
            stages += [(fname, r) for r in got]

    ids = {str(r.get("actor_id")) for r in updates + others if r.get("actor_id")}
    ids |= {str(r.get("actor_id")) for _, r in stages if r.get("actor_id")}
    people = _people(sb, ids)

    quotes: dict[str, dict] = {}
    q_ids = [r.get("entity_id") for r in others if r.get("action") == "quote.select"]
    if q_ids:
        qrows = _select_in(sb, "quotes", "id, rfq_id, vendor_id, amount", "id", q_ids)
        vendors = {str(v["id"]): v.get("name") for v in _select_in(
            sb, "vendors", "id, name", "id", [q.get("vendor_id") for q in qrows])}
        quotes = {str(q["id"]): {**q, "vendor": vendors.get(str(q.get("vendor_id")))} for q in qrows}

    out_rows: list[dict] = []

    def emit(at, fname, before, after, known, actor, action, kind, summary, table, ref_id):
        row = _row("audit" if table == "audit_log" else "stage", kind, at, actor, _clip(summary, SUMMARY_CLIP),
                   table, ref_id, field=fname, before=before, after=after, before_known=known, action=action)
        if row is not None and _in_window(row["at"], start, end):
            out_rows.append(row)

    last: dict[str, Any] = {}
    for r in updates:
        patch = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        actor = _actor(people, r.get("actor_id"), r.get("actor_kind"), action=r.get("action"))
        for k in sorted(patch):
            known = k in last
            before = last.get(k)
            after = patch[k]
            last[k] = after
            if field is not None and field != k:
                continue
            emit(r.get("created_at"), k, before, after, known, actor, "project.update", "project_update",
                 f"{k} changed", "audit_log", f"{r.get('id')}:{k}")
    for r in others:
        action = str(r.get("action"))
        actor = _actor(people, r.get("actor_id"), r.get("actor_kind"), action=action)
        payload = r.get("payload")
        kind = _change_kind(action)
        group = _group_of(action)
        if action.startswith("pricing.") and isinstance(payload, dict) and payload and all(
                isinstance(v, dict) and ("from" in v or "to" in v) for v in payload.values()):
            for k in sorted(payload):
                emit(r.get("created_at"), f"pricing.{k}", payload[k].get("from"), payload[k].get("to"), True,
                     actor, action, kind, f"{_action_label(action)}: {k}", "audit_log", f"{r.get('id')}:{k}")
            continue
        if action == "quote.select":
            q = quotes.get(str(r.get("entity_id"))) or {}
            after = {"quote_id": r.get("entity_id"), "rfq_id": q.get("rfq_id") or (payload or {}).get("rfq_id"),
                     "vendor": q.get("vendor"), "amount": q.get("amount")}
            summary = f"Selected quote{(' from ' + q['vendor']) if q.get('vendor') else ''}"
        else:
            after = _small_payload(payload)
            summary = _action_label(action)
        emit(r.get("created_at"), group, None, after, False, actor, action, kind, summary, "audit_log", r.get("id"))
    for fname, r in stages:
        emit(r.get("entered_at"), fname, r.get("from_stage"), r.get("to_stage"), r.get("from_stage") is not None,
             _actor(people, r.get("actor_id"), r.get("actor_kind")), "stage.advance",
             "stage_advanced" if fname == "current_stage" else "pm_stage_changed",
             f"{r.get('from_stage') or 'start'} to {r.get('to_stage')}",
             "stage_events" if fname == "current_stage" else "pm_stage_events", r.get("id"))

    out_rows.sort(key=_sort_key, reverse=True)
    total = len(out_rows)
    return {
        "project": project_out,
        "field": field,
        "window": _window_out(since, until),
        "rows": out_rows[:limit],
        "total": total,
        "truncated": bool(truncated or total > limit),
        "caps": dict(CHANGE_CAPS),
        "caveats": list(FILE_TRAIL_CAVEATS),
    }


# ── 3.8.4 AI usage ────────────────────────────────────────────────────────

_CALL_SELECT = "id, feature, model, ok, input_tokens, cached_tokens, output_tokens, cost_usd, created_at, " \
               "job_id, user_id, assistant_run_id"


def _usage_groups(rows: list[dict], key: Callable[[dict], tuple]) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for r in rows:
        k = key(r)
        g = groups.get(k)
        if g is None:
            g = groups[k] = {"calls": 0, "failures": 0, "input_tokens": 0, "cached_tokens": 0,
                             "output_tokens": 0, "_cost": Decimal("0"), "_key": k}
        g["calls"] += 1
        if r.get("ok") is False:
            g["failures"] += 1
        g["input_tokens"] += int(r.get("input_tokens") or 0)
        g["cached_tokens"] += int(r.get("cached_tokens") or 0)
        g["output_tokens"] += int(r.get("output_tokens") or 0)
        g["_cost"] += _num(r.get("cost_usd"))
    out = []
    for g in groups.values():
        g["cost_usd"] = _money(g.pop("_cost"))
        out.append(g)
    return out


def _totals(rows: list[dict]) -> dict:
    g = _usage_groups(rows, lambda r: ())
    if not g:
        return {"calls": 0, "failures": 0, "input_tokens": 0, "cached_tokens": 0, "output_tokens": 0,
                "cost_usd": 0.0}
    g[0].pop("_key", None)
    return g[0]


def _day_pt(value: Any) -> str | None:
    dt = _parse(value)
    return dt.astimezone(PT).date().isoformat() if dt else None


def _ae_runs(sb: Any, *, project_id: str | None = None, created_by: str | None = None, s: str, e: str,
             cap: int = AE_RUNS_CAP) -> tuple[list[dict], bool]:
    def build():
        q = sb.table("plans_reader_runs").select("id, name, status, cost_usd, created_at, finished_at")
        if project_id:
            q = q.eq("project_id", project_id)
        if created_by:
            q = q.eq("created_by", created_by)
        return q.gte("created_at", s).lt("created_at", e).order("created_at", desc=True).order("id", desc=True)
    rows, more = _paged(build, cap)
    return [{
        "id": r.get("id"), "name": _clip(r.get("name"), SUMMARY_CLIP), "status": r.get("status"),
        "cost_usd": _money(_num(r.get("cost_usd"))), "created_at": _iso(r.get("created_at")),
        "created_at_pt": pt(r.get("created_at")), "finished_at": _iso(r.get("finished_at")),
        "finished_at_pt": pt(r.get("finished_at")),
    } for r in rows], more


def ai_usage(
    sb: Any, *, project_id: str | None = None, since: date | None = None, until: date | None = None,
    group_by: str = "feature",
) -> dict:
    """3.8.4: company or project aggregate from `llm_call_log` (paged 1,000
    at a time up to 20,000 rows) plus AE run costs (`created_by` stripped)
    and `retention_note`. No per-person split.

    Returns `{project, group_by, window, groups: [{<group_by>, calls,
    failures, input_tokens, cached_tokens, output_tokens, cost_usd}],
    totals, ae_runs, ae_cost_usd, retention_note, truncated, caps}`."""
    if group_by not in AI_USAGE_GROUPS:
        raise EvidenceError("group_by_invalid")
    start, end = window(since, until)
    s, e = start.isoformat(), end.isoformat()
    truncated = False
    project_out = None
    if project_id:
        project = _project_row(sb, project_id)
        if project is None:
            raise EvidenceError("not_found")
        project_out = _project_ref(project)
        jobs, more = _paged(lambda: sb.table("llm_jobs").select("id").eq("project_id", project_id)
                            .order("created_at", desc=True).order("id", desc=True), AI_USAGE_JOB_CAP)
        truncated |= more
        calls: list[dict] = []
        for chunk in _chunks([j.get("id") for j in jobs]):
            room = AI_USAGE_CAP - len(calls)
            if room <= 0:
                truncated = True
                break
            got, more = _paged(lambda chunk=chunk: sb.table("llm_call_log").select(_CALL_SELECT)
                               .in_("job_id", chunk).gte("created_at", s).lt("created_at", e)
                               .order("created_at", desc=True).order("id", desc=True), room)
            calls += got
            truncated |= more
    else:
        calls, more = _paged(lambda: sb.table("llm_call_log").select(_CALL_SELECT)
                             .gte("created_at", s).lt("created_at", e)
                             .order("created_at", desc=True).order("id", desc=True), AI_USAGE_CAP)
        truncated |= more

    keyer = {
        "feature": lambda r: (r.get("feature") or "unknown",),
        "model": lambda r: (r.get("model") or "unknown",),
        "day": lambda r: (_day_pt(r.get("created_at")) or "unknown",),
    }[group_by]
    groups = _usage_groups(calls, keyer)
    for g in groups:
        g[group_by] = g.pop("_key")[0]
    if group_by == "day":
        groups.sort(key=lambda g: g["day"], reverse=True)
    else:
        groups.sort(key=lambda g: (-g["cost_usd"], -g["calls"], g[group_by]))
    runs, more = _ae_runs(sb, project_id=project_id, s=s, e=e)
    truncated |= more
    return {
        "project": project_out,
        "group_by": group_by,
        "window": _window_out(since, until),
        "groups": groups,
        "totals": _totals(calls),
        "ae_runs": runs,
        "ae_cost_usd": _money(sum((_num(r["cost_usd"]) for r in runs), Decimal("0"))),
        "retention_note": AI_RETENTION_NOTE,
        "truncated": bool(truncated),
        "caps": {"llm_call_log": AI_USAGE_CAP, "ae_runs": AE_RUNS_CAP},
    }


# ── 3.8.5 card previews ───────────────────────────────────────────────────


def user_subject(sb: Any, user_id: str) -> dict | None:
    """3.8.5 card preview: `{id, full_name, role, is_active}` of an internal
    profile or estimator; None when the id is not a profile."""
    rows = (
        sb.table("profiles").select("id, full_name, role, is_active").eq("id", user_id).limit(1).execute()
    ).data or []
    if not rows:
        return None
    r = rows[0]
    return {"id": str(r.get("id")), "full_name": r.get("full_name"), "role": r.get("role"),
            "is_active": bool(r.get("is_active", True))}


def estimator_subjects(sb: Any, project_id: str) -> dict | None:
    """3.8.5 card preview: `{project {id, number, name}, assignments:
    [{assignment_id, estimator_id, name, created_at, revoked_at}]}` (active
    and past); None for an unknown project."""
    project = _project_row(sb, project_id)
    if project is None:
        return None
    rows, _ = _paged(lambda: sb.table("estimator_assignments")
                     .select("id, estimator_id, created_at, revoked_at")
                     .eq("project_id", project_id).order("created_at", desc=True).order("id", desc=True),
                     ESTIMATOR_CAP)
    people = _people(sb, {r.get("estimator_id") for r in rows})
    return {
        "project": _project_ref(project),
        "assignments": [{
            "assignment_id": str(r.get("id")),
            "estimator_id": str(r.get("estimator_id")) if r.get("estimator_id") else None,
            "name": (people.get(str(r.get("estimator_id"))) or {}).get("full_name"),
            "created_at": _iso(r.get("created_at")),
            "revoked_at": _iso(r.get("revoked_at")),
        } for r in rows],
    }


# ── 3.8.6 estimator timeline ──────────────────────────────────────────────


def estimator_timeline(sb: Any, project_id: str, assignment_id: str) -> dict:
    """3.8.6: the one estimator of `assignment_id` on the project: rows plus
    `summary {assigned_at, emailed_at, email_status, bell_read_at,
    first_file_open_at, first_log_view_at, rounds: [{round, submitted_at}]}`
    and `subject {user_id, label}`. An assignment of another project raises
    EvidenceError("approval_mismatch").

    Rows are oldest first (one assignment reads as a story). Also returns
    `project`, `assignment {id, created_at, due_at, expires_at, revoked_at,
    returned_at}`, `view: "estimator_timeline"`, `truncated`, `caps`; the
    summary adds `file_open_count` and an `_pt` twin of each time.
    Unknown project or assignment raises not_found; an empty id raises
    assignment_required."""
    if not assignment_id:
        raise EvidenceError("assignment_required")
    project = _project_row(sb, project_id)
    if project is None:
        raise EvidenceError("not_found")
    arows = (
        sb.table("estimator_assignments")
        .select("id, project_id, estimator_id, assigned_by, due_at, expires_at, revoked_at, returned_at, created_at")
        .eq("id", assignment_id).limit(1).execute()
    ).data or []
    if not arows:
        raise EvidenceError("not_found")
    a = arows[0]
    if str(a.get("project_id")) != str(project_id):
        raise EvidenceError("approval_mismatch")
    est_id = str(a.get("estimator_id"))
    a_start = _parse(a.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)
    a_end = _parse(a.get("revoked_at"))
    span_start = a_start - timedelta(minutes=5)

    def in_span(value) -> bool:
        dt = _parse(value)
        return dt is not None and dt >= span_start and (a_end is None or dt <= a_end + timedelta(minutes=5))

    truncated = False
    cap = ESTIMATOR_CAP
    recips = _select_in(sb, "file_send_recipients", "batch_id, estimator_id, email_log_id", "estimator_id", [est_id])
    batches = {str(b["id"]): b for b in _select_in(
        sb, "file_send_batches", "id, project_id, kind, sent_by, sent_at", "id",
        [r.get("batch_id") for r in recips])}
    sends = []
    for r in recips:
        b = batches.get(str(r.get("batch_id")))
        if b and str(b.get("project_id")) == str(project_id) and in_span(b.get("sent_at")):
            sends.append((b, r))
    emails = {str(m["id"]): m for m in _select_in(
        sb, "email_log", "id, status, created_at", "id", [r.get("email_log_id") for _, r in sends])}
    bells, more = _paged(lambda: sb.table("notifications")
                         .select("id, type, created_at, read_at, dismissed_at")
                         .eq("user_id", est_id).eq("project_id", project_id)
                         .in_("type", ["assigned", "files_updated"])
                         .order("created_at", desc=False).order("id", desc=False), cap)
    truncated |= more
    bells = [b for b in bells if in_span(b.get("created_at"))]
    opens, more = _paged(lambda: sb.table("file_access_events")
                         .select("id, created_at, action, filename, actor_role")
                         .eq("project_id", project_id).eq("actor_id", est_id).eq("actor_role", "estimator")
                         .neq("action", "prefetch")
                         .order("created_at", desc=False).order("id", desc=False), cap)
    truncated |= more
    opens = [o for o in opens if in_span(o.get("created_at")) and o.get("action") != "prefetch"]
    views, more = _paged(lambda: sb.table("audit_log").select(_AUDIT_SELECT)
                         .eq("entity", "project").eq("entity_id", project_id)
                         .eq("action", "estimator.log_view").eq("actor_id", est_id)
                         .order("created_at", desc=False).order("id", desc=False), cap)
    truncated |= more
    views = [_audit_view(v) for v in views if in_span(v.get("created_at"))]
    rounds, more = _paged(lambda: sb.table("estimator_submissions").select("id, round, submitted_at")
                          .eq("project_id", project_id).eq("estimator_id", est_id)
                          .order("submitted_at", desc=False), cap)
    truncated |= more
    rounds = [r for r in rounds if in_span(r.get("submitted_at"))]

    people = _people(sb, {est_id, a.get("assigned_by")} | {b.get("sent_by") for b, _ in sends})
    who = _actor(people, est_id)
    system = _actor(people, None)
    rows: list[dict] = []

    def add(row):
        if row is not None:
            rows.append(row)

    add(_row("estimator", "assigned", a.get("created_at"), _actor(people, a.get("assigned_by")),
             _clip(f"Assigned to {who['label']}", SUMMARY_CLIP), "estimator_assignments", a.get("id")))
    for b, r in sends:
        mail = emails.get(str(r.get("email_log_id"))) or {}
        status = mail.get("status") or ("not_recorded" if not r.get("email_log_id") else "unknown")
        add(_row("estimator", "files_emailed", b.get("sent_at"), _actor(people, b.get("sent_by")),
                 f"Files emailed to the estimator ({b.get('kind') or 'initial'}): {status}",
                 "file_send_batches", f"{b.get('id')}:{est_id}", email_status=status, batch_kind=b.get("kind")))
    for n in bells:
        kind = "bell_assigned" if n.get("type") == "assigned" else "bell_files_updated"
        read = n.get("read_at")
        add(_row("notifications", kind, n.get("created_at"), system,
                 f"Bell {n.get('type')}: {'read ' + pt(read) if read else 'not read'}", "notifications", n.get("id"),
                 read_at=_iso(read), read_at_pt=pt(read), dismissed_at=_iso(n.get("dismissed_at"))))
    if opens:
        o = opens[0]
        add(_row("file_access", "first_file_open", o.get("created_at"), who,
                 _clip(f"First file open: {o.get('filename') or 'a file'}", SUMMARY_CLIP), "file_access_events",
                 o.get("id"), filename=_clip(o.get("filename"), 200), open_count=len(opens)))
    for v in views:
        add(_row("audit", "log_view", v.get("created_at"), who, "Viewed the file send log", "audit_log", v.get("id")))
    for r in rounds:
        add(_row("estimator", "round_submitted", r.get("submitted_at"), who, f"Round {r.get('round')} submitted",
                 "estimator_submissions", r.get("id"), round=r.get("round")))
    for col, kind, text in (("due_at", "due", "Due"), ("returned_at", "returned", "Deliverables returned"),
                            ("revoked_at", "revoked", "Access revoked"),
                            ("expires_at", "access_expires", "Access expires")):
        if a.get(col):
            add(_row("estimator", kind, a[col], who if kind == "returned" else system, text,
                     "estimator_assignments", f"{a.get('id')}:{kind}"))
    rows.sort(key=_sort_key)

    first_email = min(((b, r) for b, r in sends), key=lambda br: _parse(br[0].get("sent_at")) or a_start,
                      default=None)
    emailed_at = None
    email_status = None
    if first_email:
        mail = emails.get(str(first_email[1].get("email_log_id"))) or {}
        emailed_at = _iso(mail.get("created_at") or first_email[0].get("sent_at"))
        email_status = mail.get("status") or ("not_recorded" if not first_email[1].get("email_log_id") else "unknown")
    assigned_bell = next((b for b in bells if b.get("type") == "assigned"), None)
    summary = {
        "assigned_at": _iso(a.get("created_at")),
        "emailed_at": emailed_at,
        "email_status": email_status,
        "bell_read_at": _iso(assigned_bell.get("read_at")) if assigned_bell else None,
        "first_file_open_at": _iso(opens[0].get("created_at")) if opens else None,
        "first_log_view_at": _iso(views[0].get("created_at")) if views else None,
        "rounds": [{"round": r.get("round"), "submitted_at": _iso(r.get("submitted_at")),
                    "submitted_at_pt": pt(r.get("submitted_at"))} for r in rounds],
        "file_open_count": len(opens),
    }
    for k in ("assigned_at", "emailed_at", "bell_read_at", "first_file_open_at", "first_log_view_at"):
        summary[f"{k}_pt"] = pt(summary[k])
    return {
        "view": "estimator_timeline",
        "project": _project_ref(project),
        "subject": {"user_id": est_id, "label": who["label"]},
        "assignment": {k: (_iso(a.get(k)) if k != "id" else str(a.get("id")))
                       for k in ("id", "created_at", "due_at", "expires_at", "revoked_at", "returned_at")},
        "summary": summary,
        "rows": rows,
        "truncated": bool(truncated),
        "caps": {"estimator": cap},
    }


# ── 3.8.6 person evidence ─────────────────────────────────────────────────


def _split_kind(rows: list[dict], *, action_col: str | None = None) -> tuple[list[dict], int]:
    """(human rows, count of the assistant's rows). `assistant.*` audit
    actions count as the assistant's."""
    mine, theirs = [], 0
    for r in rows:
        assistant = (r.get("actor_kind") == provenance.ACTOR_ASSISTANT
                     or (action_col and str(r.get(action_col) or "").startswith("assistant.")))
        if assistant:
            theirs += 1
        else:
            mine.append(r)
    return mine, theirs


def _activity(sb, user_id, project_id, s, e) -> tuple[dict, dict[str, int], bool, dict]:
    cap = PERSON_SOURCE_CAP
    raw: dict[str, list[dict]] = {}
    counts: dict[str, int] = {}
    truncated = False

    def fetch(name, build):
        nonlocal truncated
        rows, more = _paged(build, cap)
        truncated |= more
        raw[name] = rows

    def scoped(q, col="project_id"):
        return q.eq(col, project_id) if project_id else q

    fetch("audit", lambda: scoped(sb.table("audit_log").select(_AUDIT_SELECT).eq("actor_id", user_id)
                                  .gte("created_at", s).lt("created_at", e), "entity_id")
          .order("created_at", desc=True).order("id", desc=True))
    raw["audit"] = [_audit_view(r) for r in raw["audit"]
                    if not str(r.get("action") or "").startswith("user.")
                    and (not project_id or r.get("entity") == "project")]
    fetch("stage", lambda: scoped(sb.table("stage_events")
                                  .select("id, project_id, from_stage, to_stage, entered_at, actor_kind")
                                  .eq("actor_id", user_id).gte("entered_at", s).lt("entered_at", e))
          .order("entered_at", desc=True).order("id", desc=True))
    fetch("pm_stage", lambda: scoped(sb.table("pm_stage_events")
                                     .select("id, project_id, from_stage, to_stage, entered_at, actor_kind")
                                     .eq("actor_id", user_id).gte("entered_at", s).lt("entered_at", e))
          .order("entered_at", desc=True).order("id", desc=True))
    fetch("file_access", lambda: scoped(sb.table("file_access_events")
                                        .select("id, created_at, project_id, action, filename, category, file_id, "
                                                "actor_kind")
                                        .eq("actor_id", user_id).neq("action", "prefetch")
                                        .gte("created_at", s).lt("created_at", e))
          .order("created_at", desc=True).order("id", desc=True))
    raw["file_access"] = [r for r in raw["file_access"] if r.get("action") != "prefetch"]
    fetch("opens", lambda: scoped(sb.table("project_open_events").select("id, project_id, kind, opened_at")
                                  .eq("user_id", user_id).gte("opened_at", s).lt("opened_at", e))
          .order("opened_at", desc=True).order("id", desc=True))
    fetch("proposal_sends", lambda: scoped(sb.table("proposal_send_events")
                                           .select("id, project_id, gc_id, kind, via, status, sent_at, created_at, "
                                                   "actor_kind")
                                           .eq("sent_by", user_id).gte("created_at", s).lt("created_at", e))
          .order("created_at", desc=True).order("id", desc=True))
    fetch("email", lambda: scoped(sb.table("email_log").select("id, project_id, subject, status, created_at, actor_kind")
                                  .eq("sent_by", user_id).gte("created_at", s).lt("created_at", e))
          .order("created_at", desc=True).order("id", desc=True))
    fetch("todos", lambda: scoped(sb.table("todos").select("id, project_id, title, source, is_done, created_at, actor_kind")
                                  .eq("user_id", user_id).gte("created_at", s).lt("created_at", e))
          .order("created_at", desc=True).order("id", desc=True))
    fetch("estimator_notes", lambda: scoped(sb.table("estimator_notes").select("id, project_id, created_at, actor_kind")
                                            .eq("author_id", user_id).gte("created_at", s).lt("created_at", e))
          .order("created_at", desc=True).order("id", desc=True))
    if project_id:
        raw["rfp_reviews"] = []
    else:
        fetch("rfp_reviews", lambda: sb.table("rfp_emails").select("id, review_decision, review_at")
              .eq("review_by", user_id).gte("review_at", s).lt("review_at", e)
              .order("review_at", desc=True).order("id", desc=True))

    for name in ("audit", "stage", "pm_stage", "file_access", "proposal_sends", "email", "estimator_notes"):
        raw[name], n = _split_kind(raw[name], action_col="action" if name == "audit" else None)
        counts[name] = n
    mine, n = [], 0
    for r in raw["todos"]:
        if r.get("actor_kind") == provenance.ACTOR_ASSISTANT or r.get("source") == "assistant":
            n += 1
        else:
            mine.append(r)
    raw["todos"], counts["todos"] = mine, n
    caps = {k: cap for k in raw}
    return raw, {k: v for k, v in counts.items() if v}, truncated, caps


def _activity_rows(sb, raw: dict[str, list[dict]], subject_actor: dict) -> list[dict]:
    pids = set()
    for rows in raw.values():
        pids |= {str(r["project_id"]) for r in rows if r.get("project_id")}
    pids |= {str(r["entity_id"]) for r in raw.get("audit", []) if r.get("entity") == "project" and r.get("entity_id")}
    projects = {str(p["id"]): p for p in _select_in(sb, "projects", "id, number, name", "id", list(pids))}

    def proj(pid):
        p = projects.get(str(pid)) if pid else None
        return {"id": str(pid), "number": p.get("number"), "name": p.get("name")} if p else (
            {"id": str(pid), "number": None, "name": None} if pid else None)

    out: list[dict] = []

    def add(row):
        if row is not None:
            out.append(row)

    for r in raw.get("audit", []):
        row = _audit_row(r, {}, source="audit")
        if row is not None:
            row["actor"] = subject_actor
            row["project"] = proj(r.get("entity_id")) if r.get("entity") == "project" else None
            add(row)
    for src, table, kind in (("stage", "stage_events", "stage_advanced"),
                             ("pm_stage", "pm_stage_events", "pm_stage_changed")):
        for r in raw.get(src, []):
            add(_row(src, kind, r.get("entered_at"), subject_actor,
                     f"{r.get('from_stage') or 'start'} to {r.get('to_stage')}", table, r.get("id"),
                     project=proj(r.get("project_id")), from_stage=r.get("from_stage"), to_stage=r.get("to_stage")))
    for r in raw.get("file_access", []):
        action = r.get("action") or "view"
        add(_row("file_access", f"file_{action}" if f"file_{action}" in WHY else "file_view", r.get("created_at"),
                 subject_actor, _clip(f"{action.capitalize()}: {r.get('filename') or 'a file'}", SUMMARY_CLIP),
                 "file_access_events", r.get("id"), project=proj(r.get("project_id")), action=action,
                 filename=_clip(r.get("filename"), 200), category=r.get("category")))
    for r in raw.get("opens", []):
        add(_row("opens", "project_opened", r.get("opened_at"), subject_actor, f"Opened the project ({r.get('kind')})",
                 "project_open_events", r.get("id"), project=proj(r.get("project_id"))))
    for r in raw.get("proposal_sends", []):
        add(_row("proposal_sends", "proposal_send", r.get("sent_at") or r.get("created_at"), subject_actor,
                 f"Proposal {r.get('kind') or 'send'} by {r.get('via') or 'email'}: {r.get('status')}",
                 "proposal_send_events", r.get("id"), project=proj(r.get("project_id")), status=r.get("status"),
                 gc_id=r.get("gc_id")))
    for r in raw.get("email", []):
        add(_row("email", "email_sent", r.get("created_at"), subject_actor,
                 _clip(f"Email: {r.get('subject') or '(no subject)'} ({r.get('status')})", SUMMARY_CLIP),
                 "email_log", r.get("id"), project=proj(r.get("project_id")), status=r.get("status"),
                 subject=_clip(r.get("subject"), SUMMARY_CLIP)))
    for r in raw.get("todos", []):
        add(_row("todos", "todo_created", r.get("created_at"), subject_actor,
                 _clip(f"To do: {r.get('title') or ''}", SUMMARY_CLIP), "todos", r.get("id"),
                 project=proj(r.get("project_id")), title=_clip(r.get("title"), SUMMARY_CLIP),
                 is_done=bool(r.get("is_done"))))
    for r in raw.get("estimator_notes", []):
        add(_row("estimator_notes", "estimator_note", r.get("created_at"), subject_actor,
                 "Posted an estimator thread note", "estimator_notes", r.get("id"), project=proj(r.get("project_id"))))
    for r in raw.get("rfp_reviews", []):
        add(_row("rfp_reviews", "rfp_review", r.get("review_at"), subject_actor,
                 f"Reviewed an RFP email: {r.get('review_decision') or 'no decision'}", "rfp_emails", r.get("id"),
                 decision=r.get("review_decision")))
    return out


def _notifications_view(sb, user_id, project_id, s, e) -> tuple[list[dict], dict[str, int], bool, dict]:
    def build():
        q = sb.table("notifications").select("id, project_id, type, message, created_at, read_at, dismissed_at") \
            .eq("user_id", user_id).gte("created_at", s).lt("created_at", e)
        if project_id:
            q = q.eq("project_id", project_id)
        return q.order("created_at", desc=True).order("id", desc=True)
    rows, more = _paged(build, PERSON_SOURCE_CAP)
    mine = [r for r in rows if not str(r.get("type") or "").startswith("assistant.")]
    theirs = len(rows) - len(mine)
    projects = {str(p["id"]): p for p in _select_in(
        sb, "projects", "id, number, name", "id", [r.get("project_id") for r in mine])}
    system = _actor({}, None)
    out = []
    for r in mine:
        p = projects.get(str(r.get("project_id"))) if r.get("project_id") else None
        read = r.get("read_at")
        row = _row("notifications", "bell", r.get("created_at"), system,
                   _clip(f"Bell {r.get('type')}: {'read ' + pt(read) if read else 'not read'}", SUMMARY_CLIP),
                   "notifications", r.get("id"), type=r.get("type"), message=_clip(r.get("message"), MESSAGE_CLIP),
                   read_at=_iso(read), read_at_pt=pt(read), dismissed_at=_iso(r.get("dismissed_at")),
                   dismissed_at_pt=pt(r.get("dismissed_at")),
                   project={"id": str(r["project_id"]), "number": (p or {}).get("number"),
                            "name": (p or {}).get("name")} if r.get("project_id") else None)
        if row:
            out.append(row)
    return out, ({"notifications": theirs} if theirs else {}), more, {"notifications": PERSON_SOURCE_CAP}


def _ai_usage_view(sb, user_id, project_id, s, e, subject_actor) -> tuple[list[dict], dict[str, int], bool, dict, dict]:
    calls, more = _paged(lambda: sb.table("llm_call_log").select(_CALL_SELECT).eq("user_id", user_id)
                         .gte("created_at", s).lt("created_at", e)
                         .order("created_at", desc=True).order("id", desc=True), AI_USAGE_CAP)
    if project_id:
        jobs = _select_in(sb, "llm_jobs", "id, project_id", "id", [c.get("job_id") for c in calls])
        in_project = {str(j["id"]) for j in jobs if str(j.get("project_id")) == str(project_id)}
        calls = [c for c in calls if str(c.get("job_id")) in in_project]
    mine = [c for c in calls if not c.get("assistant_run_id")]
    theirs = len(calls) - len(mine)
    groups = _usage_groups(mine, lambda r: (_day_pt(r.get("created_at")) or "unknown", r.get("feature") or "unknown"))
    rows = []
    for g in groups:
        day, feature = g.pop("_key")
        at = datetime.combine(date.fromisoformat(day), time(12), tzinfo=PT) if day != "unknown" else None
        row = _row("llm_call_log", "ai_usage_day", at, subject_actor,
                   f"{g['calls']} AI call{'s' if g['calls'] != 1 else ''} for {feature} on {day}",
                   "llm_call_log", f"{day}:{feature}", day=day, feature=feature, **g)
        if row:
            rows.append(row)
    runs, more2 = _ae_runs(sb, project_id=project_id, created_by=user_id, s=s, e=e)
    for r in runs:
        row = _row("plans_reader_runs", "ae_run", r["created_at"], subject_actor,
                   _clip(f"AI Estimating run {r['name'] or ''}: {r['status']}", SUMMARY_CLIP),
                   "plans_reader_runs", r["id"], status=r["status"], cost_usd=r["cost_usd"],
                   finished_at=r["finished_at"], finished_at_pt=r["finished_at_pt"])
        if row:
            rows.append(row)
    extra = {"totals": _totals(mine), "retention_note": AI_RETENTION_NOTE}
    return rows, ({"ai_calls": theirs} if theirs else {}), more or more2, \
        {"llm_call_log": AI_USAGE_CAP, "ae_runs": AE_RUNS_CAP}, extra


def _file_access_view(sb, user_id, project_id, start, end, limit, offset, subject_actor):
    """The per-file summary and the feed through `file_access.access_report`
    with `actor_kind="human"` (its default `all` would count the assistant's
    page-read records as this person's opens)."""
    rep = file_access.access_report(
        date_from=start, date_to=end - timedelta(microseconds=1), project_id=project_id, actor_id=user_id,
        actor_kind="human", include_prefetch=False, limit=limit, offset=offset,
    )
    rows = []
    for ev in rep.get("events") or []:
        action = ev.get("action") or "view"
        row = _row("file_access", f"file_{action}" if f"file_{action}" in WHY else "file_view", ev.get("created_at"),
                   subject_actor, _clip(f"{action.capitalize()}: {ev.get('filename') or 'a file'}", SUMMARY_CLIP),
                   "file_access_events", ev.get("id"), action=action, filename=_clip(ev.get("filename"), 200),
                   category=ev.get("category"), source_kind=ev.get("source"),
                   project={"id": ev.get("project_id"), "number": ev.get("project_number"),
                            "name": ev.get("project_name")} if ev.get("project_id") else None)
        if row:
            rows.append(row)
    files = [{
        "project_id": g.get("project_id"), "project_number": g.get("project_number"),
        "file_id": g.get("file_id"), "filename": _clip(g.get("filename"), 200), "category": g.get("category"),
        "views": g.get("views"), "downloads": g.get("downloads"), "exports": g.get("exports"),
        "total": g.get("total"), "first_at": g.get("first_at"), "first_at_pt": pt(g.get("first_at")),
        "last_at": g.get("last_at"), "last_at_pt": pt(g.get("last_at")),
    } for g in rep.get("summary") or []]
    s, e = start.isoformat(), end.isoformat()

    def build():
        q = sb.table("file_access_events").select("id").eq("actor_id", user_id).eq("actor_kind", "assistant") \
            .neq("action", "prefetch").gte("created_at", s).lt("created_at", e)
        if project_id:
            q = q.eq("project_id", project_id)
        return q.order("created_at", desc=True).order("id", desc=True)
    theirs, _ = _paged(build, PERSON_SOURCE_CAP)
    total = int(rep.get("events_total") or 0)
    return rows, files, total, ({"file_access": len(theirs)} if theirs else {}), bool(rep.get("truncated"))


def _encode_offset(n: int) -> str:
    return base64.urlsafe_b64encode(f"offset|{n}".encode()).decode().rstrip("=")


def _decode_offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        tag, n = base64.urlsafe_b64decode(padded.encode()).decode().split("|", 1)
        if tag != "offset" or int(n) < 0:
            raise ValueError
        return int(n)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise EvidenceError("cursor_invalid") from exc


def person_evidence(
    sb: Any, user_id: str, view: str, *, since: date | None = None, until: date | None = None,
    project_id: str | None = None, limit: int = 100, cursor: str | None = None,
) -> dict:
    """3.8.6: one person's human rows for `view` in VIEWS (`activity`,
    `file_access` via `file_access.access_report(actor_kind="human")`,
    `notifications`, `ai_usage`), with the assistant's rows only as a separate
    `assistant_counts {source: n}` block. Each source capped at 1,000 per
    window. Returns `{subject, view, rows, assistant_counts, next_cursor,
    truncated, caps}`.

    Also returns `window {since, until}`, `total` (rows across all pages),
    for `file_access` a `files` per-file summary, and for `ai_usage`
    `totals` and `retention_note`. Raises view_invalid, window_invalid,
    cursor_invalid, or not_found for an id that is not a profile."""
    if view not in VIEWS:
        raise EvidenceError("view_invalid")
    limit = _clamp(limit, 100, PERSON_LIMIT_MAX)
    start, end = window(since, until)
    subject = user_subject(sb, user_id)
    if subject is None:
        raise EvidenceError("not_found")
    subject_actor = _actor({str(user_id): {"full_name": subject.get("full_name")}}, user_id, provenance.ACTOR_HUMAN)
    s, e = start.isoformat(), end.isoformat()
    out: dict[str, Any] = {"subject": subject, "view": view, "window": _window_out(since, until)}

    if view == "file_access":
        offset = _decode_offset(cursor)
        rows, files, total, counts, truncated = _file_access_view(sb, user_id, project_id, start, end, limit, offset,
                                                                  subject_actor)
        out.update({
            "rows": rows, "files": files, "total": total, "assistant_counts": counts,
            "next_cursor": _encode_offset(offset + limit) if offset + limit < total else None,
            "truncated": truncated, "caps": {"file_access": file_access.SCAN_CAP},
        })
        return out

    cursor_key = _decode_cursor(cursor)
    if cursor_key is not None:
        e = min(end, cursor_key[0] + timedelta(microseconds=1)).isoformat()
    if view == "activity":
        raw, counts, truncated, caps = _activity(sb, user_id, project_id, s, e)
        rows = _activity_rows(sb, raw, subject_actor)
    elif view == "notifications":
        rows, counts, truncated, caps = _notifications_view(sb, user_id, project_id, s, e)
    else:
        rows, counts, truncated, caps, extra = _ai_usage_view(sb, user_id, project_id, s, e, subject_actor)
        out.update(extra)
    # With a cursor, `total` counts the rows from this page on.
    rows = [r for r in rows if _in_window(r["at"], start, end)
            and (cursor_key is None or _sort_key(r) < cursor_key)]
    page, next_cursor = _page(rows, limit, None)
    out.update({
        "rows": page,
        "total": len(rows),
        "assistant_counts": counts,
        "next_cursor": next_cursor,
        "truncated": bool(truncated),
        "caps": caps,
    })
    return out

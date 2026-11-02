"""The evidence tools (docs/ASSISTANT_PHASE4_PLAN.md 3.8, 3.17): "why did X
happen" for Executive and IT Admin, plus the notification log for every
internal role. Namespace `evidence`, all deferred (find_tools or a skill
surfaces them).

Plain reads (EVIDENCE_ROLES, sub_app bidding):

  project_timeline     GET /analytics/evidence/projects/{id}/timeline
  file_access_report   GET /analytics/file-access (human, no prefetch) and
                       GET /analytics/evidence/projects/{id}/opens
  change_history       GET /analytics/evidence/projects/{id}/changes
  ai_usage             GET /analytics/evidence/ai-usage
  notification_history GET /projects/{id}/notification-log (INTERNAL roles;
                       no recipient names, addresses or per-person read_at)

Sensitive reads (EVIDENCE_ROLES; a card with a reason the human types):

  estimator_timeline   POST /analytics/evidence/projects/{id}/estimator-timeline
  person_evidence      POST /analytics/evidence/users/{user_id}

A sensitive tool's "write" is an approval-bound POST that logs the access
(assistant_evidence_access) and returns the rows; the request carries the
approval id header and never the reason (the reason lives on the approval
row). The preview reads name the person (or the estimators of a project) so
the card says exactly whose activity it opens. The model's `reason` argument
is only the card's prefill.

Every trimmer keeps a fixed set of keys (a key a route adds later is dropped
until someone lists it here), clips free text, shortens ids and states the
window, the caps and what the evidence cannot show. Free text (summaries,
messages, subjects, names, file names, before and after values) sits under
keys the executor treats as free text, so a result that carries any of it
taints the turn (the whitelist rule of 3.8).

This module never imports app.routers.* or app.services.*: the route
literals and vocabularies below are its own, and
tests/test_assistant_evidence_tools.py asserts they equal the routes' and
the service's.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from app.assistant.registry import (
    EFFECT_READ,
    EFFECT_SENSITIVE_READ,
    LIMIT_PROP,
    RESPONSE_FORMAT_PROP,
    Proposal,
    ProposalRefused,
    ToolRequest,
    ToolSpec,
    TrimContext,
    WriteSpec,
    common_args,
    nullable,
    register,
    schema,
)
from app.assistant.tools._common import (
    ToolArgumentError,
    clip,
    compact,
    dict_of,
    is_uuid,
    narrowing_hint,
    pt,
    pt_date_range,
    pt_range_label,
    require_uuid,
    rows_of,
)
from app.core.roles import EVIDENCE_ROLES, INTERNAL_ROLES

# ── The route side's literals (app/routers/analytics_evidence.py and friends) ─

ASSISTANT_IDEMPOTENCY_HEADER = "X-Assistant-Idempotency-Key"
TIMELINE_GET = ("GET", "/analytics/evidence/projects/{project_id}/timeline")
OPENS_GET = ("GET", "/analytics/evidence/projects/{project_id}/opens")
CHANGES_GET = ("GET", "/analytics/evidence/projects/{project_id}/changes")
AI_USAGE_GET = ("GET", "/analytics/evidence/ai-usage")
USER_SUBJECT_GET = ("GET", "/analytics/evidence/subjects/users/{user_id}")
ESTIMATOR_SUBJECTS_GET = ("GET", "/analytics/evidence/projects/{project_id}/estimator-subjects")
ESTIMATOR_TIMELINE_POST = ("POST", "/analytics/evidence/projects/{project_id}/estimator-timeline")
PERSON_EVIDENCE_POST = ("POST", "/analytics/evidence/users/{user_id}")
FILE_ACCESS_GET = ("GET", "/analytics/file-access")
NOTIFICATION_LOG_GET = ("GET", "/projects/{project_id}/notification-log")

TOOL_ESTIMATOR_TIMELINE = "estimator_timeline"
TOOL_PERSON_EVIDENCE = "person_evidence"

# app/services/evidence.py vocabularies (a test asserts they are equal).
TIMELINE_SOURCES = (
    "stage", "pm_stage", "audit", "file_access", "notifications", "email", "proposal_sends",
    "estimator", "auto_send", "outcome", "opens", "markers",
)
CHANGE_FIELD_GROUPS = ("pricing", "quotes", "markup", "labor", "proposal", "files")
AI_USAGE_GROUPS = ("feature", "model", "day")
VIEWS = ("activity", "file_access", "notifications", "ai_usage")
TIMELINE_LIMIT_MAX = 200
CHANGE_LIMIT_MAX = 200
PERSON_LIMIT_DEFAULT, PERSON_LIMIT_XLSX = 100, 500
CURSOR_MAX = 500

VIEW_LABELS = {
    "activity": "Activity",
    "file_access": "File access",
    "notifications": "Notifications",
    "ai_usage": "AI usage",
}
SENSITIVE_WARNING = {
    "code": "sensitive",
    "text": "This reads one person's activity. The reason you type is logged with your name.",
}
NOTIFICATION_LIMIT_DEFAULT, NOTIFICATION_LIMIT_MAX = 50, 200
REASON_PROPOSED_MAX = 500

_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# Row keys a person or estimator view may pass on (plus the common shape).
_ROW_EXTRA_TEXT = ("type", "status", "action", "filename", "category", "decision", "day", "feature", "subject",
                   "title", "message", "email_status", "batch_kind")
_ROW_EXTRA_PLAIN = ("calls", "failures", "input_tokens", "cached_tokens", "output_tokens", "cost_usd", "is_done",
                    "open_count", "round", "from_stage", "to_stage")
_ROW_EXTRA_PT = ("read_at_pt", "dismissed_at_pt", "finished_at_pt", "submitted_at_pt")
_TIMELINE_EXTRA_TEXT = ("status", "action", "field", "filename", "email_status", "batch_kind", "type")
_TIMELINE_EXTRA_PLAIN = ("open_count", "round")


# ── Shared helpers ───────────────────────────────────────────────────────────


def _window(args: Mapping[str, Any]) -> tuple[str, str]:
    """The Pacific window the call asks for, explicit on both ends (the
    routes default to 90 days and refuse more than 400)."""
    return pt_date_range(args.get("since"), args.get("until"), default_days=90, max_days=400)


def _window_out(since: str, until: str) -> dict:
    return {"since": since, "until": until}


def _body_window(body: Mapping[str, Any], args: Mapping[str, Any]) -> dict | None:
    window = dict_of(body.get("window"))
    if window.get("since") or window.get("until"):
        return compact({"since": window.get("since"), "until": window.get("until")})
    try:
        since, until = _window(args)
    except ToolArgumentError:
        return None
    return _window_out(since, until)


def _project_ref(project: Any, ctx: TrimContext) -> dict | None:
    data = dict_of(project)
    if not data:
        return None
    return compact({
        "id": ctx.short(data.get("id")) if data.get("id") else None,
        "number": data.get("number"),
        "name": clip(data.get("name"), 120),
        "deleted": True if data.get("deleted") else None,
    })


def _cursor(args: Mapping[str, Any]) -> str | None:
    value = args.get("cursor")
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > CURSOR_MAX:
        raise ToolArgumentError("cursor is the next_cursor of an earlier answer, unchanged.")
    return text


def _text_cap(ctx: TrimContext) -> int:
    return 300 if ctx.response_format == "detailed" else 200


def _value(value: Any) -> Any:
    """A before or after value: scalars as they are (strings clipped), a
    patch as compact JSON text."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return clip(value, 200)
    try:
        return clip(json.dumps(value, default=str, sort_keys=True, separators=(",", ":")), 300)
    except (TypeError, ValueError):
        return None


def _notes(rows: list[dict], cap: int) -> list[dict]:
    """The fixed `why` sentence of each kind shown, once each: what the
    record is and what it does not prove."""
    seen: dict[str, str] = {}
    for row in rows:
        kind = row.get("kind")
        why = row.get("why")
        if isinstance(kind, str) and isinstance(why, str) and kind not in seen:
            seen[kind] = why
        if len(seen) >= cap:
            break
    return [{"kind": kind, "why": clip(why, 220)} for kind, why in seen.items()]


def _finish(out: dict[str, Any], **lists: Any) -> dict:
    """compact(out) with the named lists put back even when empty: "no rows"
    is the answer, not noise to drop. A list left None is not added."""
    result = compact(out)
    for key, value in lists.items():
        if value is not None:
            result[key] = value
    return result


def _event(row: Mapping[str, Any], ctx: TrimContext, *, plain: tuple[str, ...], text: tuple[str, ...]) -> dict:
    actor = dict_of(row.get("actor"))
    cap = _text_cap(ctx)
    out: dict[str, Any] = {
        "at_pt": row.get("at_pt") or pt(row.get("at")),
        "source": row.get("source"),
        "kind": row.get("kind"),
        "actor": clip(actor.get("label"), 80),
        "actor_kind": actor.get("kind") if actor.get("kind") in ("assistant", "system") else None,
        "summary": clip(row.get("summary"), cap),
    }
    for key in plain:
        if row.get(key) is not None:
            out[key] = row[key]
    for key in text:
        if row.get(key) is not None:
            out[key] = clip(row[key], cap)
    for key in _ROW_EXTRA_PT:
        if row.get(key):
            out[key] = row[key]
    return compact(out)


# ── project_timeline ─────────────────────────────────────────────────────────


def _sources(args: Mapping[str, Any]) -> list[str]:
    value = args.get("sources")
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ToolArgumentError("sources is a list of timeline sources, or null for all.")
    out: list[str] = []
    for item in value:
        if item not in TIMELINE_SOURCES:
            raise ToolArgumentError(f"Unknown timeline source {str(item)[:40]!r}. Use: {', '.join(TIMELINE_SOURCES)}.")
        if item not in out:
            out.append(item)
    return out


def _timeline_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    pid = require_uuid(args, "project_id")
    since, until = _window(args)
    _, limit = common_args(args)
    params: dict[str, Any] = {"since": since, "until": until, "limit": min(TIMELINE_LIMIT_MAX, limit)}
    sources = _sources(args)
    if sources:
        params["sources"] = ",".join(sources)
    cursor = _cursor(args)
    if cursor:
        params["cursor"] = cursor
    return [ToolRequest(route=TIMELINE_GET, path=f"/analytics/evidence/projects/{pid}/timeline", params=params)]


def _timeline_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    body = dict_of(payloads[0] if payloads else None)
    rows = rows_of(body.get("rows"))
    shown = rows[: ctx.limit]
    deleted = dict_of(body.get("deleted"))
    out: dict[str, Any] = {
        "project": _project_ref(body.get("project"), ctx),
        "window": _body_window(body, args),
        "deleted": compact({"at_pt": deleted.get("at_pt"), "reason": clip(deleted.get("reason"), 200)})
        if deleted else None,
        "hint": clip(body.get("notice"), 300) if deleted else None,
        "next_cursor": body.get("next_cursor"),
        "truncated": True if body.get("truncated") else None,
        "caveats": [clip(c, 200) for c in body.get("caveats") or [] if isinstance(c, str)] or None,
    }
    events = [_event(r, ctx, plain=_TIMELINE_EXTRA_PLAIN, text=_TIMELINE_EXTRA_TEXT) for r in shown]
    notes = _notes(shown, 12 if ctx.response_format == "detailed" else 8)
    if body.get("truncated"):
        out["caps"] = dict_of(body.get("caps")) if ctx.response_format == "detailed" else None
        out["hint"] = out.get("hint") or "A source hit its cap, so this is not the whole history. Narrow the window."
    elif len(rows) > len(shown):
        out["hint"] = narrowing_hint(len(rows), len(shown), "Raise limit, or page with next_cursor.")
    return _finish(out, rows=events, notes=notes or None)


register(ToolSpec(
    name="project_timeline",
    namespace="evidence",
    sub_app="bidding",
    route=TIMELINE_GET,
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "project_id": {"type": "string", "description": "The project's id (short or full) from app_search."},
        "since": nullable({"type": "string", "description": "First Pacific date, YYYY-MM-DD; null means 90 days ago."}),
        "until": nullable({"type": "string", "description": "Last Pacific date, YYYY-MM-DD, inclusive; null means today."}),
        "sources": nullable({
            "type": "array", "items": {"type": "string", "enum": list(TIMELINE_SOURCES)},
            "description": "Only these sources (stage, audit, email, notifications, opens...); null for all.",
        }),
        "cursor": nullable({"type": "string", "description": "next_cursor of the previous answer, to page back in time."}),
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_timeline_trim,
    progress_label="Reading the project's history",
    roles=EVIDENCE_ROLES,
    description=(
        "Use for \"what happened on this project\" or \"why did it go out late\": the merged history of one "
        "project, newest first (stage moves, saved changes, files opened, notices, emails, proposal sends, "
        "estimator steps, outcome). Each kind comes with a note on what it does not prove (no read receipts, no "
        "email bodies). Start here before a narrower report. 90 days by default, 400 at most."
    ),
    requests=_timeline_requests,
    taint_fields=("summary", "message", "subject", "note", "title", "filename", "name", "before", "after"),
    deferred=True,
    keywords=("timeline", "history", "happened", "why", "late", "sequence", "when", "audit", "events"),
))


# ── file_access_report ───────────────────────────────────────────────────────


def _opens_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    pid = require_uuid(args, "project_id")
    since, until = _window(args)
    _, limit = common_args(args)
    return [
        ToolRequest(route=FILE_ACCESS_GET, path=FILE_ACCESS_GET[1], params={
            "range": "custom", "date_from": since, "date_to": until, "project_id": pid,
            "actor_kind": "human", "include_prefetch": "false", "limit": limit,
        }),
        ToolRequest(route=OPENS_GET, path=f"/analytics/evidence/projects/{pid}/opens"),
    ]


def _file_group(group: Mapping[str, Any], ctx: TrimContext) -> dict:
    return compact({
        "filename": clip(group.get("filename"), 160),
        "category": group.get("category"),
        "actor_name": clip(group.get("actor_name"), 80),
        "views": group.get("views"),
        "downloads": group.get("downloads"),
        "exports": group.get("exports"),
        "total": group.get("total"),
        "last_at_pt": pt(group.get("last_at")),
    })


def _opens_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    access = dict_of(payloads[0] if payloads else None)
    opens = dict_of(payloads[1] if len(payloads) > 1 else None)
    groups = rows_of(access.get("summary"))
    people = rows_of(opens.get("rows"))
    shown_groups, shown_people = groups[: ctx.limit], people[: ctx.limit]
    try:
        since, until = _window(args)
        window = _window_out(since, until)
    except ToolArgumentError:
        window = None
    out: dict[str, Any] = {
        "project": _project_ref(opens.get("project"), ctx),
        "window": window,
        "files_total": int(access.get("events_total") or 0) or None,
        "caveats": [clip(c, 200) for c in opens.get("caveats") or [] if isinstance(c, str)],
        "truncated": True if (access.get("truncated") or opens.get("truncated")) else None,
        "hint": narrowing_hint(len(groups), len(shown_groups), "Raise limit or narrow the window.")
        or narrowing_hint(len(people), len(shown_people), "Raise limit."),
    }
    return _finish(
        out,
        files_opened=[_file_group(g, ctx) for g in shown_groups],
        project_page_opens=[compact({
            "name": clip(p.get("name"), 80),
            "role": p.get("role"),
            "kind": p.get("kind"),
            "open_count": p.get("open_count"),
            "first_opened_at_pt": p.get("first_opened_at_pt"),
            "last_opened_at_pt": p.get("last_opened_at_pt"),
        }) for p in shown_people],
    )


register(ToolSpec(
    name="file_access_report",
    namespace="evidence",
    sub_app="bidding",
    route=FILE_ACCESS_GET,
    extra_routes=(OPENS_GET,),
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "project_id": {"type": "string", "description": "The project's id (short or full) from app_search."},
        "since": nullable({"type": "string", "description": "First Pacific date, YYYY-MM-DD; null means 90 days ago."}),
        "until": nullable({"type": "string", "description": "Last Pacific date, YYYY-MM-DD, inclusive; null means today."}),
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_opens_trim,
    progress_label="Reading who opened the files",
    roles=EVIDENCE_ROLES,
    description=(
        "Use for \"who opened the plans\" or \"did anyone look at this project\": which files people opened or "
        "downloaded on one project and how often (people only, not the assistant), plus project page visits per "
        "person. It shows opens, not reading time; the caveats list what is not recorded (accountant opens, "
        "history before tracking began, estimator portal views)."
    ),
    requests=_opens_requests,
    taint_fields=("summary", "message", "subject", "note", "title", "filename", "name", "actor_name"),
    deferred=True,
    keywords=("opened", "files", "file", "access", "viewed", "downloaded", "download", "plans", "visits"),
))


# ── change_history ───────────────────────────────────────────────────────────


def _changes_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    pid = require_uuid(args, "project_id")
    since, until = _window(args)
    _, limit = common_args(args)
    params: dict[str, Any] = {"since": since, "until": until, "limit": min(CHANGE_LIMIT_MAX, limit)}
    field = args.get("field")
    if field is not None and str(field).strip():
        name = str(field).strip()
        if name not in CHANGE_FIELD_GROUPS and not _FIELD_RE.match(name):
            raise ToolArgumentError(
                f"field is a project field name or one of {', '.join(CHANGE_FIELD_GROUPS)}; null for everything."
            )
        params["field"] = name
    return [ToolRequest(route=CHANGES_GET, path=f"/analytics/evidence/projects/{pid}/changes", params=params)]


def _change_row(row: Mapping[str, Any], ctx: TrimContext) -> dict:
    actor = dict_of(row.get("actor"))
    known = row.get("before_known")
    return compact({
        "at_pt": row.get("at_pt") or pt(row.get("at")),
        "field": row.get("field"),
        "before": _value(row.get("before")) if known else None,
        "after": _value(row.get("after")),
        "before_known": known if known is not None else None,
        "actor": clip(actor.get("label"), 80),
        "actor_kind": actor.get("kind") if actor.get("kind") in ("assistant", "system") else None,
        "action": row.get("action"),
        "summary": clip(row.get("summary"), _text_cap(ctx)) if ctx.response_format == "detailed" else None,
    })


def _changes_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    body = dict_of(payloads[0] if payloads else None)
    rows = rows_of(body.get("rows"))
    shown = rows[: ctx.limit]
    total = body.get("total") if isinstance(body.get("total"), int) else len(rows)
    out: dict[str, Any] = {
        "project": _project_ref(body.get("project"), ctx),
        "field": body.get("field"),
        "window": _body_window(body, args),
        "total": total,
        "truncated": True if body.get("truncated") else None,
        "caveats": [clip(c, 200) for c in body.get("caveats") or [] if isinstance(c, str)] or None,
        "hint": narrowing_hint(total, len(shown), "Raise limit, name one field, or narrow the window."),
    }
    return _finish(out, rows=[_change_row(r, ctx) for r in shown], notes=_notes(shown, 6) or None)


register(ToolSpec(
    name="change_history",
    namespace="evidence",
    sub_app="bidding",
    route=CHANGES_GET,
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "project_id": {"type": "string", "description": "The project's id (short or full) from app_search."},
        "field": nullable({"type": "string", "description": (
            "One project field (internal_bid_at, wage_type...) or a group: pricing, quotes, markup, labor, "
            "proposal, files; null for everything.")}),
        "since": nullable({"type": "string", "description": "First Pacific date, YYYY-MM-DD; null means 90 days ago."}),
        "until": nullable({"type": "string", "description": "Last Pacific date, YYYY-MM-DD, inclusive; null means today."}),
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_changes_trim,
    progress_label="Reading what changed",
    roles=EVIDENCE_ROLES,
    description=(
        "Use for \"what changed on this project\" or \"who changed the price\": field changes with the value "
        "before and after, who made them and when. A change whose first value predates the audit trail has no "
        "known before value. Amounts are included. One project only."
    ),
    requests=_changes_requests,
    taint_fields=("summary", "message", "subject", "note", "title", "filename", "name", "before", "after"),
    deferred=True,
    keywords=("changed", "change", "edit", "edited", "before", "after", "price", "field", "modified", "history"),
))


# ── ai_usage ─────────────────────────────────────────────────────────────────


def _ai_usage_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    since, until = _window(args)
    group_by = args.get("group_by") if args.get("group_by") is not None else "feature"
    if group_by not in AI_USAGE_GROUPS:
        raise ToolArgumentError(f"group_by must be one of {', '.join(AI_USAGE_GROUPS)}.")
    params: dict[str, Any] = {"since": since, "until": until, "group_by": group_by}
    if args.get("project_id") is not None:
        params["project_id"] = require_uuid(args, "project_id")
    return [ToolRequest(route=AI_USAGE_GET, path=AI_USAGE_GET[1], params=params)]


def _ai_usage_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    body = dict_of(payloads[0] if payloads else None)
    group_by = body.get("group_by") if body.get("group_by") in AI_USAGE_GROUPS else "feature"
    groups = rows_of(body.get("groups"))
    runs = rows_of(body.get("ae_runs"))
    shown, shown_runs = groups[: ctx.limit], runs[: min(ctx.limit, 10)]
    keys = (group_by, "calls", "failures", "input_tokens", "cached_tokens", "output_tokens", "cost_usd")
    out: dict[str, Any] = {
        "project": _project_ref(body.get("project"), ctx),
        "group_by": group_by,
        "window": _body_window(body, args),
        "totals": dict_of(body.get("totals")) or None,
        "ae_cost_usd": body.get("ae_cost_usd"),
        "retention_note": clip(body.get("retention_note"), 200),
        "truncated": True if body.get("truncated") else None,
        "hint": narrowing_hint(len(groups), len(shown), "Raise limit."),
    }
    return _finish(
        out,
        groups=[compact({k: g.get(k) for k in keys}) for g in shown],
        ae_runs=[compact({
            "id": ctx.short(r.get("id")) if r.get("id") else None,
            "name": clip(r.get("name"), 120),
            "status": r.get("status"),
            "cost_usd": r.get("cost_usd"),
            "finished_at_pt": r.get("finished_at_pt"),
        }) for r in shown_runs],
    )


register(ToolSpec(
    name="ai_usage",
    namespace="evidence",
    sub_app="bidding",
    route=AI_USAGE_GET,
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "project_id": nullable({"type": "string", "description": "One project (short or full id); null for the company."}),
        "group_by": nullable({"type": "string", "enum": list(AI_USAGE_GROUPS),
                              "description": "Group the calls by feature, model or day; null means feature."}),
        "since": nullable({"type": "string", "description": "First Pacific date, YYYY-MM-DD; null means 90 days ago."}),
        "until": nullable({"type": "string", "description": "Last Pacific date, YYYY-MM-DD, inclusive; null means today."}),
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_ai_usage_trim,
    progress_label="Reading AI usage",
    roles=EVIDENCE_ROLES,
    description=(
        "Use for \"what did AI cost\" or \"how much AI did this project use\": calls, tokens and cost for the "
        "company or one project, grouped by feature, model or day, plus the AI Estimating runs and their cost. "
        "Never split by person (that needs the person_evidence card). The call log is kept 90 days."
    ),
    requests=_ai_usage_requests,
    taint_fields=("summary", "message", "subject", "note", "title", "filename", "name"),
    deferred=True,
    keywords=("ai", "usage", "cost", "tokens", "spend", "model", "calls", "budget", "llm"),
))


# ── notification_history ─────────────────────────────────────────────────────


def _notification_limit(args: Mapping[str, Any]) -> int:
    raw = args.get("limit")
    try:
        limit = int(raw) if raw is not None else NOTIFICATION_LIMIT_DEFAULT
    except (TypeError, ValueError):
        limit = NOTIFICATION_LIMIT_DEFAULT
    return max(1, min(NOTIFICATION_LIMIT_MAX, limit))


def _notification_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    pid = require_uuid(args, "project_id")
    # The route takes no user argument and no limit; the trimmer applies the limit.
    return [ToolRequest(route=NOTIFICATION_LOG_GET, path=f"/projects/{pid}/notification-log")]


def _audience(recipients: list[dict]) -> str:
    """The role when every recipient shares one, else a head count. Never a
    name, address or user id."""
    n = len(recipients)
    if n == 0:
        return "nobody recorded"
    if n == 1:
        return "1 person"
    roles = {r.get("role") for r in recipients}
    if len(roles) == 1 and None not in roles and "" not in roles:
        return f"{next(iter(roles))} ({n})"
    return f"{n} people"


def _notification_status(entry: Mapping[str, Any]) -> str:
    counts = dict_of(entry.get("counts"))
    if int(counts.get("failed") or 0) > 0:
        return "failed"
    if int(counts.get("emailed") or 0) > 0:
        return "sent"
    return "posted" if entry.get("source") == "notification" else "unknown"


def _notification_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    body = dict_of(payloads[0] if payloads else None)
    entries = rows_of(body.get("entries"))
    limit = _notification_limit(args)
    shown = entries[:limit]
    rows = []
    for e in shown:
        recipients = rows_of(e.get("recipients"))
        rows.append(compact({
            "at_pt": pt(e.get("at")),
            "channel": "bell" if e.get("source") == "notification" else "email",
            "type": e.get("type"),
            "title": clip(e.get("title"), 120),
            "audience": _audience(recipients),
            "status": _notification_status(e),
            "subject": clip(e.get("subject"), 200),
            "message": clip(e.get("message"), 300),
        }))
    cut = len(entries) > len(shown)
    out: dict[str, Any] = {
        "total": len(entries),
        "truncated": True if (body.get("truncated") or cut) else None,
        "hint": narrowing_hint(len(entries), len(shown), "Raise limit.")
        or ("The log reads at most 500 notices and 500 emails; older ones are not listed." if body.get("truncated")
            else None),
    }
    return _finish(out, rows=rows)


register(ToolSpec(
    name="notification_history",
    namespace="evidence",
    sub_app="bidding",
    route=NOTIFICATION_LOG_GET,
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "project_id": {"type": "string", "description": "The project's id (short or full) from app_search."},
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": nullable({"type": "integer", "description": (
            f"Most notices to return, 1 to {NOTIFICATION_LIMIT_MAX}; null means {NOTIFICATION_LIMIT_DEFAULT}.")}),
    }),
    response_trimmer=_notification_trim,
    progress_label="Reading the project's notices",
    roles=INTERNAL_ROLES,
    description=(
        "Use for \"who was told about this project\" or \"was the estimator notified\": the bells and emails one "
        "project sent, newest first, with the type, who they went to (a role or a head count, never names or "
        "addresses) and whether the email was accepted. It does not say who read them; BDR has no read receipts "
        "for email."
    ),
    requests=_notification_requests,
    taint_fields=("subject", "message"),
    deferred=True,
    keywords=("notification", "notice", "bell", "email", "sent", "alert", "notified", "told"),
))


# ── The sensitive tier: a card with a reason, then the approval-bound POST ───


def _reason_arg() -> dict:
    return {"type": "string", "description": (
        "Why the user needs this evidence, 10 to 500 characters, in plain words. It is only a suggestion: the "
        "user reads and edits it on the card, and what they type is logged with their name.")}


def _reason_proposed(args: Mapping[str, Any]) -> str:
    return " ".join(str(args.get("reason") or "").split())[:REASON_PROPOSED_MAX]


def _row_for_card(field: str, label: str, after: Any) -> dict:
    text = "Empty" if after in (None, "") else str(after)
    return {"field": field, "label": label, "before": "", "after": text, "changed": True}


def _optional_project_id(args: Mapping[str, Any]) -> str | None:
    value = args.get("project_id")
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not is_uuid(value):
        raise ToolArgumentError("project_id must be a project id from app_search (the short id or the full id).")
    return str(value)


def _evidence_row(row: Mapping[str, Any], ctx: TrimContext) -> dict:
    """One event of a person or estimator view, with the keys the delivery
    message reads (at_pt, kind, source, summary, project {id, number},
    read_at_pt)."""
    out: dict[str, Any] = {
        "at_pt": row.get("at_pt") or pt(row.get("at")),
        "source": row.get("source"),
        "kind": row.get("kind"),
        "summary": clip(row.get("summary"), 200),
    }
    project = dict_of(row.get("project"))
    if project.get("id") or project.get("number"):
        out["project"] = compact({
            "id": ctx.short(project.get("id")) if project.get("id") else None,
            "number": project.get("number"),
        })
    for key in _ROW_EXTRA_TEXT:
        if row.get(key) is not None:
            out[key] = clip(row[key], 300 if key == "message" else 200)
    for key in _ROW_EXTRA_PLAIN:
        if row.get(key) is not None:
            out[key] = row[key]
    for key in _ROW_EXTRA_PT:
        if row.get(key):
            out[key] = row[key]
    return compact(out)


def _counts_dict(value: Any) -> dict:
    data = dict_of(value)
    return {str(k): int(v) for k, v in data.items() if isinstance(v, int) and not isinstance(v, bool)}


def _subject_out(value: Any, ctx: TrimContext) -> dict:
    data = dict_of(value)
    ident = data.get("user_id") or data.get("id")
    return compact({
        "id": ctx.short(ident) if ident else None,
        "label": clip(data.get("label") or data.get("full_name"), 120),
        "role": data.get("role"),
    })


def _person_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    body = dict_of(payloads[0] if payloads else None)
    rows = rows_of(body.get("rows"))
    shown = rows[: ctx.limit]
    files = rows_of(body.get("files"))
    report = body.get("xlsx_report_id")
    out: dict[str, Any] = {
        "subject": _subject_out(body.get("subject"), ctx),
        "view": body.get("view"),
        "window": _body_window(body, args),
        "assistant_counts": _counts_dict(body.get("assistant_counts")) or None,
        "total": body.get("total") if isinstance(body.get("total"), int) else None,
        "totals": dict_of(body.get("totals")) or None,
        "retention_note": clip(body.get("retention_note"), 200),
        "next_cursor": body.get("next_cursor"),
        "truncated": True if body.get("truncated") else None,
        "xlsx_report_id": ctx.short(report) if report else None,
        "hint": narrowing_hint(len(rows), len(shown), "Ask for a narrower window or one project."),
    }
    if files:
        out["files"] = [compact({
            "filename": clip(f.get("filename"), 160),
            "category": f.get("category"),
            "project_number": f.get("project_number"),
            "views": f.get("views"),
            "downloads": f.get("downloads"),
            "exports": f.get("exports"),
            "total": f.get("total"),
            "last_at_pt": f.get("last_at_pt"),
        }) for f in files[:15]]
    return _finish(out, rows=[_evidence_row(r, ctx) for r in shown])


def _summary_counts(view: str):
    def result_summary(payload: Any) -> dict:
        """Counts only: no row, name or text reaches the ledger, the
        approval result or the after snapshot (3.2.3)."""
        body = dict_of(payload)
        rows = body.get("rows") if isinstance(body.get("rows"), list) else []
        total = body.get("total") if isinstance(body.get("total"), int) and not isinstance(body.get("total"), bool) \
            else len(rows)
        return {"row_count": max(total, len(rows)), "truncated": bool(body.get("truncated")),
                "view": body.get("view") if body.get("view") in (*VIEWS, "estimator_timeline") else view}

    return result_summary


# person_evidence ------------------------------------------------------------


def _person_preview_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    uid = require_uuid(args, "user_id")
    return [ToolRequest(route=USER_SUBJECT_GET, path=f"/analytics/evidence/subjects/users/{uid}")]


def _person_snapshot(args: Mapping[str, Any], reads: list[Any]) -> tuple[dict, dict]:
    subject = dict_of(reads[0] if reads else None)
    return {"user_id": str(subject.get("id") or ""), "label": str(subject.get("full_name") or "")}, {}


def _person_build(args: Mapping[str, Any], reads: list[Any], ctx: Any) -> Proposal:
    subject = dict_of(reads[0] if reads else None)
    uid = require_uuid(args, "user_id")
    if not subject.get("id") or str(subject["id"]).lower() != uid.lower():
        raise ProposalRefused("No such person. Pick one from list_teammates.")
    view = args.get("view")
    if view not in VIEWS:
        raise ProposalRefused(f"view must be one of {', '.join(VIEWS)}.")
    since, until = _window(args)
    project_id = _optional_project_id(args)
    xlsx = args.get("xlsx") is True
    name = str(subject.get("full_name") or "this person")
    body: dict[str, Any] = {
        "view": view, "since": since, "until": until, "project_id": project_id,
        "limit": PERSON_LIMIT_XLSX if xlsx else PERSON_LIMIT_DEFAULT, "xlsx": xlsx,
    }
    request = ToolRequest(
        route=PERSON_EVIDENCE_POST, path=f"/analytics/evidence/users/{uid}", json=body,
        headers={ASSISTANT_IDEMPOTENCY_HEADER: str(ctx.approval_id)},
    )
    rows = [
        _row_for_card("person", "Person", name),
        _row_for_card("view", "View", VIEW_LABELS[view]),
        _row_for_card("window", "Window", pt_range_label(since, until)),
    ]
    if project_id:
        rows.append(_row_for_card("project", "Project", "One project only"))
    if xlsx:
        rows.append(_row_for_card("spreadsheet", "Spreadsheet", "Yes, up to 500 rows"))
    warnings = [dict(SENSITIVE_WARNING)]
    if subject.get("is_active") is False:
        warnings.append({"code": "inactive", "text": "This person is no longer active."})
    target, gate = _person_snapshot(args, reads)
    preview = {
        "title": f"Read evidence: {VIEW_LABELS[view]} for {name}",
        "tool_label": "Person evidence",
        "project": None,
        "rows": rows,
        "warnings": warnings,
        "reason_required": True,
        "reason_proposed": _reason_proposed(args),
    }
    return Proposal(request=request, project_id=project_id, target=target, gate=gate, preview=preview,
                    summary=f"Evidence card for {name}, {VIEW_LABELS[view].lower()}. It waits for the user's approval "
                            "and a reason they type.")


register(ToolSpec(
    name=TOOL_PERSON_EVIDENCE,
    namespace="evidence",
    sub_app="bidding",
    route=PERSON_EVIDENCE_POST,
    extra_routes=(USER_SUBJECT_GET,),
    effect=EFFECT_SENSITIVE_READ,
    confirm=True,
    schema=schema({
        "user_id": {"type": "string", "description": "The person's id (short or full) from list_teammates."},
        "view": {"type": "string", "enum": list(VIEWS), "description": (
            "activity (what they did across BDR), file_access (files they opened), notifications (notices they "
            "got and when they opened them) or ai_usage (their AI use).")},
        "since": nullable({"type": "string", "description": "First Pacific date, YYYY-MM-DD; null means 90 days ago."}),
        "until": nullable({"type": "string", "description": "Last Pacific date, YYYY-MM-DD, inclusive; null means today."}),
        "project_id": nullable({"type": "string", "description": "Only this project (short or full id); null for all."}),
        "xlsx": nullable({"type": "boolean", "description": "True also builds a spreadsheet of up to 500 rows."}),
        "reason": _reason_arg(),
    }),
    response_trimmer=_person_trim,
    progress_label="Preparing a request to read a person's activity",
    roles=EVIDENCE_ROLES,
    description=(
        "Use only when the user asks what one named person did, opened or was sent. Creates a card naming the "
        "person and the view; the user types the reason and approves, and only then is the person's activity "
        "read and shown in the conversation. Their own rows only: what their assistant did is a separate count. "
        "Try project_timeline first when the question is about a project, not a person."
    ),
    requests=_person_preview_requests,
    max_cards_per_turn=2,
    max_proposals_per_turn=3,
    taint_fields=("summary", "message", "subject", "note", "title", "filename", "name", "before", "after"),
    deferred=True,
    keywords=("person", "who", "activity", "user", "employee", "teammate", "did", "worked", "logins", "emails"),
    write=WriteSpec(
        preview_requests=_person_preview_requests,
        build=_person_build,
        snapshot=_person_snapshot,
        bound_text_fields=("reason",),
        result_summary=_summary_counts("activity"),
        writes_tables=("assistant_evidence_access",),
    ),
))


# estimator_timeline ---------------------------------------------------------


def _estimator_preview_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    pid = require_uuid(args, "project_id")
    return [ToolRequest(route=ESTIMATOR_SUBJECTS_GET, path=f"/analytics/evidence/projects/{pid}/estimator-subjects")]


def _assignments(reads: list[Any]) -> list[dict]:
    return rows_of(dict_of(reads[0] if reads else None).get("assignments"))


def _chosen_assignment(args: Mapping[str, Any], reads: list[Any], short: Any = None) -> dict:
    """The one assignment the card names. A missing or unlisted id is
    refused with the list, so the model can ask which estimator."""
    listed = _assignments(reads)
    wanted = str(args.get("assignment_id") or "").strip().lower()
    found = next((a for a in listed if str(a.get("assignment_id") or "").lower() == wanted), None) if wanted else None
    if found is not None:
        return found
    if not listed:
        raise ProposalRefused("No estimator was ever assigned to this project.")
    lines = []
    for a in listed[:6]:
        ident = short(a.get("assignment_id")) if short is not None else a.get("assignment_id")
        state = "revoked" if a.get("revoked_at") else "active"
        lines.append(f"{ident}: {clip(a.get('name'), 60) or 'an estimator'} ({state})")
    raise ProposalRefused(
        "Name the estimator assignment to read (assignment_id). One card reads one estimator. Assignments: "
        + "; ".join(lines) + "."
    )


_SUMMARY_KEYS = ("assigned_at", "emailed_at", "email_status", "bell_read_at", "first_file_open_at",
                 "first_log_view_at", "file_open_count", "assigned_at_pt", "emailed_at_pt", "bell_read_at_pt",
                 "first_file_open_at_pt", "first_log_view_at_pt")


def _estimator_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    """The estimator's steps oldest first, with the summary block the
    delivery message reads (assigned, emailed, email status, bell read,
    first open, first log view, rounds)."""
    body = dict_of(payloads[0] if payloads else None)
    rows = rows_of(body.get("rows"))
    shown = rows[: ctx.limit]
    summary = dict_of(body.get("summary"))
    rounds = [compact({"round": r.get("round"), "submitted_at": r.get("submitted_at"),
                       "submitted_at_pt": r.get("submitted_at_pt")}) for r in rows_of(summary.get("rounds"))[:20]]
    summary_out = compact({k: summary.get(k) for k in _SUMMARY_KEYS})
    if rounds:
        summary_out["rounds"] = rounds
    out: dict[str, Any] = {
        "view": "estimator_timeline",
        "project": _project_ref(body.get("project"), ctx),
        "subject": _subject_out(body.get("subject"), ctx),
        "summary": summary_out or None,
        "truncated": True if body.get("truncated") else None,
        "hint": narrowing_hint(len(rows), len(shown), "The first rows are shown (oldest first)."),
    }
    return _finish(out, rows=[_evidence_row(r, ctx) for r in shown])


def _estimator_snapshot(args: Mapping[str, Any], reads: list[Any]) -> tuple[dict, dict]:
    project = dict_of(dict_of(reads[0] if reads else None).get("project"))
    chosen = next((a for a in _assignments(reads)
                   if str(a.get("assignment_id") or "").lower() == str(args.get("assignment_id") or "").lower()), {})
    return ({
        "project_id": str(project.get("id") or ""),
        "assignment_id": str(chosen.get("assignment_id") or ""),
        "estimator_id": str(chosen.get("estimator_id") or ""),
        "label": str(chosen.get("name") or ""),
    }, {})


def _estimator_build(args: Mapping[str, Any], reads: list[Any], ctx: Any) -> Proposal:
    pid = require_uuid(args, "project_id")
    data = dict_of(reads[0] if reads else None)
    project = dict_of(data.get("project"))
    if not project.get("id") or str(project["id"]).lower() != pid.lower():
        raise ProposalRefused("No such project.")
    chosen = _chosen_assignment(args, reads, ctx.short)
    aid = str(chosen["assignment_id"])
    name = str(chosen.get("name") or "the estimator")
    request = ToolRequest(
        route=ESTIMATOR_TIMELINE_POST, path=f"/analytics/evidence/projects/{pid}/estimator-timeline",
        json={"assignment_id": aid}, headers={ASSISTANT_IDEMPOTENCY_HEADER: str(ctx.approval_id)},
    )
    state = "Access revoked" if chosen.get("revoked_at") else "Active"
    rows = [
        _row_for_card("estimator", "Estimator", name),
        _row_for_card("project", "Project", f"{project.get('number') or ''} {project.get('name') or ''}".strip()),
        _row_for_card("assignment", "Assignment", f"{pt(chosen.get('created_at')) or 'unknown date'} ({state})"),
        _row_for_card("view", "View", "Everything BDR recorded about this assignment, oldest first"),
    ]
    target, gate = _estimator_snapshot(args, reads)
    preview = {
        "title": f"Read evidence: estimator timeline for {name}",
        "tool_label": "Estimator timeline",
        "project": {"id": pid, "number": project.get("number"), "name": project.get("name")},
        "rows": rows,
        "warnings": [dict(SENSITIVE_WARNING)],
        "reason_required": True,
        "reason_proposed": _reason_proposed(args),
    }
    return Proposal(request=request, project_id=pid, target=target, gate=gate, preview=preview,
                    summary=f"Evidence card for {name} on project {project.get('number') or ''}. It waits for the "
                            "user's approval and a reason they type.")


register(ToolSpec(
    name=TOOL_ESTIMATOR_TIMELINE,
    namespace="evidence",
    sub_app="bidding",
    route=ESTIMATOR_TIMELINE_POST,
    extra_routes=(ESTIMATOR_SUBJECTS_GET,),
    effect=EFFECT_SENSITIVE_READ,
    confirm=True,
    schema=schema({
        "project_id": {"type": "string", "description": "The project's id (short or full) from app_search."},
        "assignment_id": nullable({"type": "string", "description": (
            "The estimator assignment to read (short or full id). Null makes the tool answer with the project's "
            "assignments so you can ask which one; one card reads one estimator.")}),
        "reason": _reason_arg(),
    }),
    response_trimmer=_estimator_trim,
    progress_label="Preparing a request to read an estimator's timeline",
    roles=EVIDENCE_ROLES,
    description=(
        "Use for \"did the estimator get the files, open them, or send their estimate back\": one estimator's "
        "steps on one project, oldest first (assigned, files emailed with the email status, bell read, first file "
        "open, log views, rounds, returned, revoked). Creates a card naming the estimator; the user types the "
        "reason and approves. Read-receipt facts exist only here."
    ),
    requests=_estimator_preview_requests,
    max_cards_per_turn=2,
    max_proposals_per_turn=3,
    taint_fields=("summary", "message", "subject", "note", "title", "filename", "name", "before", "after"),
    deferred=True,
    keywords=("estimator", "assignment", "timeline", "files", "sent", "received", "opened", "returned", "read"),
    write=WriteSpec(
        preview_requests=_estimator_preview_requests,
        build=_estimator_build,
        snapshot=_estimator_snapshot,
        bound_text_fields=("reason",),
        result_summary=_summary_counts("estimator_timeline"),
        writes_tables=("assistant_evidence_access",),
    ),
))

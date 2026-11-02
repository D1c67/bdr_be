"""The schedule tools (docs/ASSISTANT_PHASE3_PLAN.md 3.6): the user's own
routines, through the routines routes.

  schedule_create   POST /assistant/schedules (confirm; the card shows the
                    next three runs; the execute job's idempotency key is
                    the approval id)
  schedule_list     GET /assistant/schedules (read)
  schedule_preview  GET /assistant/schedules/{id}/preview, or
                    GET /assistant/schedules/preview for a recipe (read, a
                    dry run: nothing is sent or saved)
  schedule_pause    POST /assistant/tools/schedules/{id}/pause (housekeeping)
  schedule_resume   POST /assistant/tools/schedules/{id}/resume (housekeeping)
  schedule_end      POST /assistant/tools/schedules/{id}/end (housekeeping)
  schedule_run_now  POST /assistant/tools/schedules/{id}/run-now (housekeeping)

The model never writes a rule or a title (deviation D3): schedule_create
takes a structured cadence and the draft route builds the rule, the title
and the run times from enums. The housekeeping tools run inline under a
ledger row; their routes trust only the turn context that row gives, so in
a tainted turn the end pauses instead and resume and Run now are refused
(deviation D6).

This module never imports app.routers.*: the route literals are its own,
and tests/test_assistant_schedule_tools.py asserts they equal the routes'.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.assistant.recipes import HORIZON_DEFAULT, PERIOD_DEFAULT, RECIPES, WEEK_DEFAULT
from app.assistant.registry import (
    EFFECT_HOUSEKEEPING,
    EFFECT_READ,
    EFFECT_WRITE_INTERNAL,
    LIMIT_PROP,
    RESPONSE_FORMAT_PROP,
    Proposal,
    ProposalRefused,
    ToolRequest,
    ToolSpec,
    TrimContext,
    Trimmer,
    WriteSpec,
    nullable,
    register,
    schema,
)
from app.assistant.rrules import ALL_DAYS, FREQUENCIES
from app.assistant.tools._common import (
    ToolArgumentError,
    compact,
    dict_of,
    is_uuid,
    narrowing_hint,
    pt,
    rows_of,
)
from app.core.roles import INTERNAL_ROLES

# The route side's literals (app/routers/assistant_routines.py); a test
# asserts they are equal.
ASSISTANT_IDEMPOTENCY_HEADER = "X-Assistant-Idempotency-Key"
SCHEDULES_GET = ("GET", "/assistant/schedules")
SCHEDULES_POST = ("POST", "/assistant/schedules")
DRAFT_GET = ("GET", "/assistant/schedules/draft")
PREVIEW_GET = ("GET", "/assistant/schedules/preview")
BY_APPROVAL_GET = ("GET", "/assistant/schedules/by-approval/{approval_id}")
SCHEDULE_PREVIEW_GET = ("GET", "/assistant/schedules/{schedule_id}/preview")
PAUSE_POST = ("POST", "/assistant/tools/schedules/{schedule_id}/pause")
RESUME_POST = ("POST", "/assistant/tools/schedules/{schedule_id}/resume")
END_POST = ("POST", "/assistant/tools/schedules/{schedule_id}/end")
RUN_NOW_POST = ("POST", "/assistant/tools/schedules/{schedule_id}/run-now")

# app/routers/assistant_routines.py DELIVERIES and STATUS_FILTERS, and
# app/assistant/schemas.py ACTIVE_SCHEDULE_CAP (a test asserts they are equal).
DELIVERIES = ("chat", "chat_bell", "chat_bell_email")
STATUSES = ("active", "paused", "ended", "all")
ACTIVE_CAP = 10

DELIVERY_TEXT = {
    "chat": "Chat only",
    "chat_bell": "Chat and the bell",
    "chat_bell_email": "Chat, the bell and an email to you",
}
CAP_TEXT = f"You already have {ACTIVE_CAP} active routines. End or pause one first."
DRAFT_FAILED_TEXT = "The routine could not be checked. Try again."
RUN_QUEUED_TEXT = "Queued. The result arrives in the routine's own conversation within a minute or two."

# Per-turn ceilings of the housekeeping tools (3.7.1): 3 calls, 1 while the
# turn is tainted.
HOUSEKEEPING_CALLS = 3
HOUSEKEEPING_TAINTED_CALLS = 1


def _no_trim(payloads: list[Any], args: Mapping[str, Any], ctx: Any) -> Any:
    """A confirm tool never runs inside a turn, so it has nothing to trim."""
    return payloads


def _schedule_id(args: Mapping[str, Any]) -> str:
    value = args.get("schedule_id")
    if not is_uuid(value):
        raise ToolArgumentError("schedule_id must be a routine id from schedule_list (the short id or the full id).")
    return str(value)


def _project_id(args: Mapping[str, Any]) -> str | None:
    value = args.get("project_id")
    if value is None:
        return None
    if not is_uuid(value):
        raise ToolArgumentError("project_id must be a project id from app_search (the short id or the full id).")
    return str(value)


def _runs_text(runs: Any) -> list[str]:
    return [text for text in (pt(r) for r in runs or () if isinstance(r, str)) if text]


# ── schedule_create ──────────────────────────────────────────────────────────

CADENCE_PROP: dict = {
    "type": "object",
    "description": (
        "When it runs, in Pacific time. weekdays: Monday to Friday, G3 holidays skipped. daily: every day. "
        "weekly: the days listed, every `interval` weeks. monthly: on day_of_month. hourly: every hour from "
        "window_start_hour to window_end_hour on the days listed (weekdays or all seven). Never guess a time: "
        "ask when the user did not say morning or evening."
    ),
    "properties": {
        "frequency": {"type": "string", "enum": list(FREQUENCIES), "description": "How often it repeats."},
        "hour": nullable({"type": "integer", "description": "Hour 0 to 23 (Pacific); null for hourly."}),
        "minute": nullable({"type": "integer", "description": "Minute 0 to 59; null means 0."}),
        "days": {
            "type": "array",
            "items": {"type": "string", "enum": list(ALL_DAYS)},
            "description": "weekly: the days it runs; hourly: MO to FR or all seven; [] for the others.",
        },
        "day_of_month": nullable({"type": "integer", "description": "monthly only: 1 to 28; else null."}),
        "interval": nullable({"type": "integer", "description": "weekly only: every 1 to 4 weeks; else null."}),
        "window_start_hour": nullable({"type": "integer", "description": "hourly only: the first hour; else null."}),
        "window_end_hour": nullable({"type": "integer", "description": "hourly only: the last hour; else null."}),
    },
    "required": sorted(["frequency", "hour", "minute", "days", "day_of_month", "interval",
                        "window_start_hour", "window_end_hour"]),
    "additionalProperties": False,
}


def _cadence(args: Mapping[str, Any]) -> dict:
    cadence = args.get("cadence")
    if not isinstance(cadence, Mapping):
        raise ToolArgumentError("cadence must be an object with frequency, hour, minute and days.")
    return dict(cadence)


def _draft_params(args: Mapping[str, Any]) -> dict:
    """The draft route's flat query parameters (3.3.3); a null is left out."""
    cadence = _cadence(args)
    params: dict[str, Any] = {"recipe": str(args.get("recipe") or "")}
    for key in ("frequency", "hour", "minute", "day_of_month", "interval", "window_start_hour",
                "window_end_hour"):
        if cadence.get(key) is not None:
            params[key] = cadence[key]
    days = cadence.get("days")
    if isinstance(days, (list, tuple)) and days:
        params["days"] = ",".join(str(d) for d in days)
    params["delivery"] = str(args.get("delivery") or "chat_bell")
    params["only_if_changed"] = "true" if args.get("only_if_changed") is True else "false"
    if args.get("horizon_days") is not None:
        params["horizon_days"] = args["horizon_days"]
    pid = _project_id(args)
    if pid:
        params["project_id"] = pid
    # Phase 4 (3.10.1): the month of estimator_vs_bid and the week of
    # quote_readiness; the draft route validates them per recipe.
    for key in ("period", "week"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            params[key] = value.strip()
    return params


def _create_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    return [ToolRequest(route=DRAFT_GET, path=DRAFT_GET[1], params=_draft_params(args))]


def _draft(reads: list[Any]) -> dict:
    return dict_of(reads[0] if reads else None)


def _gate(draft: Mapping[str, Any]) -> dict:
    """What must still hold when the card executes: room under the cap and,
    for a watch, the bid still open. The run times are display only, so the
    card does not go stale as time passes."""
    body = dict_of(draft.get("body"))
    ok = draft.get("ok") is True
    if not ok and draft.get("code") == "watch_project_closed":
        watch = "closed"
    else:
        watch = "open" if ok and body.get("project_id") else None
    return {"active_ok": ok and int(draft.get("active_count") or 0) < ACTIVE_CAP, "watch": watch}


def _create_snapshot(args: Mapping[str, Any], reads: list[Any]) -> tuple[dict, dict]:
    """(target, gate): the normalized body the draft returns. A draft that
    refuses gives a target that matches no card, so the execute job
    supersedes instead of writing."""
    draft = _draft(reads)
    if draft.get("ok") is not True:
        return {"refused": str(draft.get("code") or "draft_failed")}, _gate(draft)
    return dict_of(draft.get("body")), _gate(draft)


def _card_row(field: str, label: str, after: Any) -> dict:
    text = "Empty" if after in (None, "") else str(after)
    return {"field": field, "label": label, "before": "Empty", "after": text, "changed": text != "Empty"}


_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
                "October", "November", "December")


def _period_text(value: Any) -> str:
    """"last month", "this month" or "September 2026" for the card's Month
    row; a missing period reads as the recipe default."""
    text = str(value or PERIOD_DEFAULT).strip().lower()
    if text in ("last_month", "this_month"):
        return text.replace("_", " ")
    year, _, month = text.partition("-")
    if year.isdigit() and month.isdigit() and 1 <= int(month) <= 12:
        return f"{_MONTH_NAMES[int(month) - 1]} {year}"
    return text


def _week_text(value: Any) -> str:
    """"next week", "this week" or "week of 10/26/2026" for the card's Week
    row; a missing week reads as the recipe default."""
    text = str(value or WEEK_DEFAULT).strip().lower()
    if text in ("this_week", "next_week"):
        return text.replace("_", " ")
    year, _, rest = text.partition("-")
    month, _, day = rest.partition("-")
    if year.isdigit() and month.isdigit() and day.isdigit():
        return f"week of {int(month)}/{int(day)}/{year}"
    return text


def _create_build(args: Mapping[str, Any], reads: list[Any], ctx: Any) -> Proposal:
    draft = _draft(reads)
    if draft.get("ok") is not True:
        raise ProposalRefused(str(draft.get("message") or DRAFT_FAILED_TEXT))
    if int(draft.get("active_count") or 0) >= ACTIVE_CAP:
        raise ProposalRefused(CAP_TEXT)
    body = dict_of(draft.get("body"))
    recipe = RECIPES.get(str(body.get("recipe") or ""))
    if recipe is None or not draft.get("title"):
        raise ProposalRefused(DRAFT_FAILED_TEXT)
    title = str(draft["title"])
    project = dict_of(draft.get("project"))
    watch = project.get("number") or (project.get("id") and "The chosen bid")
    rows = [
        _card_row("recipe", "Recipe", recipe.label),
        _card_row("cadence", "Repeats", draft.get("cadence_text")),
        _card_row("delivery", "Delivery", DELIVERY_TEXT.get(str(body.get("delivery")), body.get("delivery"))),
        _card_row("only_if_changed", "Only if changed", "Yes" if body.get("only_if_changed") else "No"),
    ]
    # A recipe that watches nothing (estimator_vs_bid, bids due today) has no
    # Watching row at all, rather than an empty one.
    if watch:
        rows.append(_card_row("watch", "Watching", watch))
    if recipe.accepts_horizon and not body.get("project_id"):
        rows.append(_card_row("horizon_days", "Days ahead", body.get("horizon_days") or HORIZON_DEFAULT))
    # Phase 4 (3.10.1): the month of estimator_vs_bid, the week of quote_readiness.
    if recipe.accepts_period:
        rows.append(_card_row("period", "Month", _period_text(body.get("period"))))
    if recipe.accepts_week:
        rows.append(_card_row("week", "Week", _week_text(body.get("week"))))
    runs = [r for r in draft.get("next_runs") or () if isinstance(r, str)][:3]
    warnings = [{"code": str(w.get("code") or ""), "text": str(w.get("text") or "")}
                for w in draft.get("warnings") or () if isinstance(w, Mapping)]
    preview = {
        "v": 1,
        "title": title,
        "tool_label": "New routine",
        "project": ({"id": str(project["id"]), "number": project.get("number"), "name": project.get("name")}
                    if project.get("id") else None),
        "rows": rows,
        "runs": runs,
        "items": [],
        "warnings": warnings,
        "similar": [],
    }
    target, gate = _create_snapshot(args, reads)
    request = ToolRequest(route=SCHEDULES_POST, path=SCHEDULES_POST[1], json=body,
                          headers={ASSISTANT_IDEMPOTENCY_HEADER: str(ctx.approval_id)})
    first = _runs_text(runs[:1])
    summary = f"New routine: {title}." + (f" First run {first[0]}." if first else "")
    return Proposal(request=request, project_id=body.get("project_id"), target=target, gate=gate,
                    preview=preview, summary=summary)


def _created(args: Mapping[str, Any], payload: Any) -> dict | None:
    row = dict_of(payload)
    if not row.get("id"):
        return None
    return {"kind": "schedule", "id": str(row["id"]), "label": row.get("title"),
            "href": f"/assistant?tab=routines&schedule={row['id']}"}


def _recover(args: Mapping[str, Any], approval_id: str) -> ToolRequest:
    return ToolRequest(route=BY_APPROVAL_GET, path=f"/assistant/schedules/by-approval/{approval_id}")


def _created_summary(payload: Any) -> dict:
    row = dict_of(payload)
    return {"id": row.get("id"), "title": row.get("title"), "status": row.get("status")}


register(ToolSpec(
    name="schedule_create",
    namespace="schedule",
    sub_app="bidding",
    route=SCHEDULES_POST,
    # The recover read is declared: _send_all refuses any undeclared route.
    extra_routes=(DRAFT_GET, BY_APPROVAL_GET),
    effect=EFFECT_WRITE_INTERNAL,
    confirm=True,
    schema=schema({
        "recipe": {
            "type": "string", "enum": sorted(RECIPES),
            "description": (
                "due_today: bids due today. due_this_week: bids due from today to Sunday. missing_quotes: "
                "bids missing vendor quotes in the next horizon_days (or one watched bid). "
                "labor_not_entered: bids with no labor entered in the next horizon_days (or one watched bid). "
                "estimator_vs_bid: last month's bids, the estimator's price against the lowest price we sent "
                "(Executive, IT Admin, Estimating Admin and Accountant only). quote_readiness: the bids due in "
                "a week and whether each has its key vendor quotes."
            ),
        },
        "cadence": CADENCE_PROP,
        "delivery": {"type": "string", "enum": list(DELIVERIES),
                     "description": "chat, chat_bell (default) or chat_bell_email (an email to the user too)."},
        "only_if_changed": {"type": "boolean",
                            "description": "True: a run whose bids did not change since the last one stays quiet."},
        "horizon_days": nullable({"type": "integer", "description": (
            "missing_quotes and labor_not_entered only: days ahead, 1 to 30; null means 7.")}),
        "project_id": nullable({"type": "string", "description": (
            "missing_quotes and labor_not_entered only: watch this one bid (short or full id) until it is "
            "submitted or closed; null for none.")}),
        "period": nullable({"type": "string", "description": (
            "estimator_vs_bid only: the month to compare, YYYY-MM, last_month or this_month; null means "
            "last_month.")}),
        "week": nullable({"type": "string", "description": (
            "quote_readiness only: this_week, next_week or the Monday date (YYYY-MM-DD); null means "
            "next_week.")}),
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_no_trim,
    progress_label="Preparing a routine",
    roles=INTERNAL_ROLES,
    description=(
        "Propose a recurring report for the user (a routine). Six recipes: bids due today, bids due this week, "
        "bids missing vendor quotes, bids with no labor entered, estimator vs our price and key quotes for the "
        "bid week. Creates an approval card naming the recipe, the schedule in words and the next three runs; "
        "nothing is set up until the user approves it. At most 10 routines can be active."
    ),
    requests=_create_requests,
    max_cards_per_turn=1,
    write=WriteSpec(
        preview_requests=_create_requests,
        build=_create_build,
        snapshot=_create_snapshot,
        creates="schedule",
        created_ref=_created,
        result_summary=_created_summary,
        recover_request=_recover,
        writes_tables=("assistant_schedules",),
    ),
))


# ── schedule_list ────────────────────────────────────────────────────────────


def _routine(row: Mapping[str, Any], ctx: TrimContext) -> dict:
    """One routine as the model sees it. Every field is server-built (the
    title from enums and the project number), so none is a taint field."""
    detailed = ctx.response_format == "detailed"
    last = dict_of(row.get("last_run"))
    project = dict_of(row.get("project"))
    return compact({
        "id": ctx.short(row.get("id")),
        "title": row.get("title"),
        "recipe": row.get("recipe"),
        "status": row.get("status"),
        "paused_reason": row.get("paused_reason"),
        "ended_reason": row.get("ended_reason"),
        "repeats": row.get("cadence_text"),
        "next_runs": _runs_text(row.get("next_runs")) if detailed else _runs_text(row.get("next_runs"))[:1],
        "delivery": row.get("delivery"),
        "only_if_changed": True if row.get("only_if_changed") else None,
        "watching": project.get("number"),
        "last_run": compact({"status": last.get("status"), "at": pt(last.get("finished_at")) if detailed else None})
        if last else None,
        "unread": int(row.get("unread_deliveries") or 0) or None,
    })


def _list_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    status = args.get("status") if args.get("status") in STATUSES else "all"
    return [ToolRequest(route=SCHEDULES_GET, path=SCHEDULES_GET[1], params={"status": status})]


def _list_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    rows = rows_of(dict_of(payloads[0] if payloads else None).get("items"))
    shown = rows[: ctx.limit]
    result = compact({
        "active": sum(1 for r in rows if r.get("status") == "active"),
        "active_cap": ACTIVE_CAP,
        "hint": narrowing_hint(len(rows), len(shown), "Raise limit or pass a status."),
    })
    # An empty list is the answer ("no routines"), not noise to compact away.
    result["routines"] = [_routine(r, ctx) for r in shown]
    return result


register(ToolSpec(
    name="schedule_list",
    namespace="schedule",
    sub_app=None,
    route=SCHEDULES_GET,
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "status": nullable({"type": "string", "enum": list(STATUSES),
                            "description": "active, paused, ended or all; null means all."}),
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_list_trim,
    progress_label="Reading your routines",
    roles=INTERNAL_ROLES,
    description=(
        "The user's routines (recurring reports), newest first, with short ids to pass to schedule_pause, "
        "schedule_end, schedule_resume, schedule_run_now and schedule_preview."
    ),
    requests=_list_requests,
))


# ── schedule_preview ─────────────────────────────────────────────────────────


def _preview_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    if args.get("schedule_id") is not None:
        sid = _schedule_id(args)
        return [ToolRequest(route=SCHEDULE_PREVIEW_GET, path=f"/assistant/schedules/{sid}/preview")]
    recipe = args.get("recipe")
    if recipe not in RECIPES:
        raise ToolArgumentError("Pass schedule_id for an existing routine, or a recipe to preview.")
    params: dict[str, Any] = {"recipe": recipe}
    if args.get("horizon_days") is not None:
        params["horizon_days"] = args["horizon_days"]
    pid = _project_id(args)
    if pid:
        params["project_id"] = pid
    return [ToolRequest(route=PREVIEW_GET, path=PREVIEW_GET[1], params=params)]


def _preview_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    """The dry run as the model sees it: the fixed count sentence, then one
    entry per bid with the project label under `name` (a taint field: the
    label carries the project's human-authored name)."""
    body = dict_of(payloads[0] if payloads else None)
    blocks = [b for b in body.get("blocks") or () if isinstance(b, Mapping)]
    table = next((b for b in blocks if b.get("type") == "table"), None)
    links = [b for b in blocks if b.get("type") == "project_link"]
    table_rows = [cells for cells in dict_of(table).get("rows") or () if isinstance(cells, list)]
    bids = []
    for i, cells in enumerate(table_rows):
        link = links[i] if i < len(links) else {}
        bids.append(compact({
            "id": ctx.short(link.get("project_id")) if link.get("project_id") else None,
            "name": cells[0] if cells else None,
            "due": cells[1] if len(cells) > 1 else None,
            "missing": cells[2] if len(cells) > 2 else None,
        }))
    more = next((b.get("text") for b in blocks if b.get("type") == "text"), None)
    result = compact({
        "content": body.get("content"),
        "window": dict_of(body.get("window")).get("label"),
        "rows": body.get("rows"),
        "more": more,
        "would_deliver": body.get("would_deliver"),
        "suppressed": body.get("suppressed"),
        "note": body.get("note"),
    })
    result["bids"] = bids[: ctx.limit]
    return result


register(ToolSpec(
    name="schedule_preview",
    namespace="schedule",
    sub_app="bidding",
    route=SCHEDULE_PREVIEW_GET,
    extra_routes=(PREVIEW_GET,),
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "schedule_id": nullable({"type": "string", "description": "An existing routine's id (short or full); "
                                 "null to preview a recipe instead."}),
        "recipe": nullable({"type": "string", "enum": sorted(RECIPES),
                            "description": "The recipe to preview when there is no routine yet; else null."}),
        "horizon_days": nullable({"type": "integer", "description": "With recipe: days ahead, 1 to 30; else null."}),
        "project_id": nullable({"type": "string", "description": "With recipe: one bid to watch; else null."}),
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_preview_trim,
    progress_label="Previewing a routine",
    roles=INTERNAL_ROLES,
    description=(
        "A dry run of a routine or a recipe: what it would post right now and where it would send it. "
        "Nothing is sent or saved. Use it only when the user asks about a routine. For a one-off question "
        "(estimator vs our price, key quotes for a week) use coded_report through find_tools, and whats_due "
        "for what is due."
    ),
    requests=_preview_requests,
    taint_fields=("name",),
))


# ── pause, resume, end, Run now (housekeeping) ───────────────────────────────

SCHEDULE_ID_SCHEMA = schema({
    "schedule_id": {"type": "string", "description": "The routine's id (short or full) from schedule_list."},
})


def _action_requests(route: tuple[str, str]):
    def requests(args: Mapping[str, Any]) -> list[ToolRequest]:
        sid = _schedule_id(args)
        return [ToolRequest(route=route, path=route[1].replace("{schedule_id}", sid))]

    return requests


def _schedule_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    row = dict_of(payloads[0] if payloads else None)
    return compact({
        "id": ctx.short(row.get("id")),
        "title": row.get("title"),
        "status": row.get("status"),
        "paused_reason": row.get("paused_reason"),
        "ended_reason": row.get("ended_reason"),
        "next_run": next(iter(_runs_text(row.get("next_runs"))), None),
        # The tool end in a tainted turn paused instead: say it as it is.
        "note": row.get("note"),
    })


def _run_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    run = dict_of(payloads[0] if payloads else None)
    return compact({
        "run_id": ctx.short(run.get("id")),
        "schedule_id": ctx.short(run.get("schedule_id")),
        "status": run.get("status"),
        "message": RUN_QUEUED_TEXT,
    })


def _housekeeping(name: str, route: tuple[str, str], label: str, description: str,
                  trimmer: Trimmer = _schedule_trim) -> ToolSpec:
    return register(ToolSpec(
        name=name,
        namespace="schedule",
        sub_app="bidding",
        route=route,
        effect=EFFECT_HOUSEKEEPING,
        confirm=False,
        schema=SCHEDULE_ID_SCHEMA,
        response_trimmer=trimmer,
        progress_label=label,
        roles=INTERNAL_ROLES,
        description=description,
        requests=_action_requests(route),
        max_proposals_per_turn=HOUSEKEEPING_CALLS,
        max_tainted_calls_per_turn=HOUSEKEEPING_TAINTED_CALLS,
    ))


_housekeeping(
    "schedule_pause", PAUSE_POST, "Pausing a routine",
    "Pause one of the user's routines; it sends nothing until resumed. Reversible.",
)
_housekeeping(
    "schedule_resume", RESUME_POST, "Resuming a routine",
    "Resume one of the user's paused routines from its next run. Refused in a conversation that read outside "
    "content: then the user resumes it on the Routines tab.",
)
_housekeeping(
    "schedule_end", END_POST, "Ending a routine",
    "End one of the user's routines for good (\"stop sending that report\"); its history stays. In a "
    "conversation that read outside content it is paused instead and the answer's note says so: tell the user "
    "that End on the Routines tab stops it for good.",
)
_housekeeping(
    "schedule_run_now", RUN_NOW_POST, "Running a routine now",
    "Run one of the user's routines once now, through its usual channels. Refused in a conversation that read "
    "outside content, and when it ran in the last few minutes.",
    trimmer=_run_trim,
)

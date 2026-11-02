"""The report tools (docs/ASSISTANT_PHASE4_PLAN.md 3.9, 3.10, 3.11, 3.17):

  run_report     GET one of the eleven /analytics report routes, inline
                 (read; bidding; INTERNAL roles; deferred)
  coded_report   GET /assistant/recipes/{recipe}/rows for the owner's two
                 coded reports, estimator_vs_bid and quote_readiness (read)
  quotes_summary GET /projects/{id}/vendor-selection, optionally the
                 pricing summary (read)
  build_report   POST /assistant/tools/reports: one .xlsx the user downloads
                 from the chat (housekeeping, namespace `report`)

Every trimmer is a whitelist (`REPORT_KEEP` for run_report): the keys that may
reach the model, per report, at most `limit` rows. A key a route adds later is
dropped until someone lists it here. For a role outside
ACTUAL_BID_VIEWER_ROLES the whitelist leaves out every amount (the V gates
below) and `strip_bid_amounts` runs after it as the second guard. Project,
GC and vendor names, labor notes and the like are free text: run_report names
the any-text marker "*" in its taint_fields, so a result that carries any of
it taints the turn (the whitelist rule of 3.8, enforced by the executor).

This module never imports app.routers.* or app.services.*: the route
literals and the per-report parameter map below are its own, and
tests/test_assistant_report_tools.py asserts them against the live routes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.assistant import recipes as _recipes
from app.assistant.recipes import RecipeError, clean_period, clean_week
from app.assistant.registry import (
    EFFECT_HOUSEKEEPING,
    EFFECT_READ,
    LIMIT_PROP,
    RESPONSE_FORMAT_PROP,
    ToolRequest,
    ToolSpec,
    TrimContext,
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
    parse_date,
    pt,
    require_uuid,
    rows_of,
    strip_bid_amounts,
)
from app.core.roles import ACTUAL_BID_VIEWER_ROLES, INTERNAL_ROLES, Role

# ── The route side's literals ────────────────────────────────────────────────

REPORT_NAMES = (
    "summary", "overview", "send_out", "estimator", "quotes", "cycle_time", "win_loss", "gc_spread",
    "bid_invitations", "labor_engineer", "calling_in",
)
REPORT_ROUTES: dict[str, tuple[str, str]] = {
    "summary": ("GET", "/analytics/summary"),
    "overview": ("GET", "/analytics/overview"),
    "send_out": ("GET", "/analytics/send-out"),
    "estimator": ("GET", "/analytics/estimator"),
    "quotes": ("GET", "/analytics/quotes"),
    "cycle_time": ("GET", "/analytics/cycle-time"),
    "win_loss": ("GET", "/analytics/win-loss"),
    "gc_spread": ("GET", "/analytics/gc-spread"),
    "bid_invitations": ("GET", "/analytics/bid-invitations"),
    "labor_engineer": ("GET", "/analytics/labor-engineer"),
    "calling_in": ("GET", "/analytics/calling-in"),
}
RECIPE_ROWS_ROUTE = ("GET", "/assistant/recipes/{recipe}/rows")
VENDOR_SELECTION_GET = ("GET", "/projects/{project_id}/vendor-selection")
PRICING_SUMMARY_GET = ("GET", "/projects/{project_id}/pricing-summary")
BUILD_REPORT_POST = ("POST", "/assistant/tools/reports")

# Query parameters each report route takes (route-side names; a test pins
# them against the route signatures). `summary` takes none (analytics.py:67);
# `labor_engineer` takes search, limit and offset only; `calling_in` takes
# the rolling range with start and end (it also reads date_from and
# date_to, which this tool maps to start and end); `bid_invitations` takes
# the calendar range; the rest take the rolling range, a window, a project
# and a status.
_WINDOWED = ("range", "date_from", "date_to", "project_id", "status")
REPORT_PARAMS: dict[str, tuple[str, ...]] = {
    "summary": (),
    "overview": _WINDOWED,
    "send_out": _WINDOWED,
    "estimator": _WINDOWED,
    "quotes": _WINDOWED,
    "cycle_time": _WINDOWED,
    "win_loss": _WINDOWED,
    "gc_spread": _WINDOWED,
    "bid_invitations": ("range", "date_from", "date_to"),
    "labor_engineer": ("search", "limit", "offset"),
    "calling_in": ("range", "start", "end"),
}
# The tool argument that feeds each route parameter.
_ARG_FOR_PARAM = {"start": "date_from", "end": "date_to"}

RANGE_ROLLING = ("day", "week", "month", "quarter", "year", "custom")
RANGE_CALENDAR = ("today", "wtd", "mtd", "ytd", "yesterday", "last_week", "last_month", "last_year",
                  "past_5_years", "custom")
RANGE_WORDS = ("day", "week", "month", "quarter", "year", "custom", "today", "wtd", "mtd", "ytd", "yesterday",
               "last_week", "last_month", "last_year", "past_5_years")
STATUS_WORDS = ("active", "sent", "won", "lost", "no_award", "declined", "abandoned", "failed_to_bid")
SEARCH_MAX = 100


def ranges_for(report: str) -> tuple[str, ...]:
    """The range words the report's route accepts; () when it takes none."""
    if "range" not in REPORT_PARAMS.get(report, ()):
        return ()
    return RANGE_CALENDAR if report == "bid_invitations" else RANGE_ROLLING


# ── run_report: the precheck and the one request ─────────────────────────────

_ARG_NAMES = ("range", "date_from", "date_to", "project_id", "status", "search")


def _given(args: Mapping[str, Any], key: str) -> Any:
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return value.strip() if isinstance(value, str) else value


def _taken_args(report: str) -> set[str]:
    taken = {_ARG_FOR_PARAM.get(p, p) for p in REPORT_PARAMS[report]}
    taken.discard("limit")
    taken.discard("offset")
    return taken


def report_problem(args: Mapping[str, Any]) -> str | None:
    """Why this run_report call cannot be sent, or None. Refuses an argument
    the report does not take, a range of the wrong vocabulary, a custom
    range without both dates and dates with a named range."""
    report = args.get("report")
    if report not in REPORT_ROUTES:
        return f"report must be one of {', '.join(REPORT_NAMES)}."
    taken = _taken_args(report)
    for key in _ARG_NAMES:
        if _given(args, key) is not None and key not in taken:
            if taken:
                return f"The {report} report does not take '{key}'. It takes: {', '.join(sorted(taken))}."
            return f"The {report} report takes no filters. Leave {key} empty."
    range_, start, end = _given(args, "range"), _given(args, "date_from"), _given(args, "date_to")
    words = ranges_for(report)
    if range_ is not None and range_ not in words:
        return f"range '{range_}' is not for the {report} report. Use one of: {', '.join(words)}."
    if (start is None) != (end is None):
        return "A custom window needs both date_from and date_to."
    if start is not None:
        if range_ is not None and range_ != "custom":
            return "date_from and date_to go with range custom only."
        try:
            a, b = parse_date(start, "date_from"), parse_date(end, "date_to")
        except ToolArgumentError as exc:
            return str(exc)
        if a > b:
            return "date_from must be on or before date_to."
    elif range_ == "custom":
        return "A custom range needs date_from and date_to."
    if _given(args, "project_id") is not None and not is_uuid(args.get("project_id")):
        return "project_id must be a project id from app_search (the short id or the full id)."
    status = _given(args, "status")
    if status is not None and status not in STATUS_WORDS:
        return f"status must be one of {', '.join(STATUS_WORDS)}."
    search = _given(args, "search")
    if search is not None and len(str(search)) > SEARCH_MAX:
        return f"search is at most {SEARCH_MAX} characters."
    return None


def _report_precheck(args: Mapping[str, Any]) -> tuple[str, str] | None:
    problem = report_problem(args)
    return ("bad_args", problem) if problem else None


def _report_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    problem = report_problem(args)
    if problem:
        raise ToolArgumentError(problem)
    report = str(args["report"])
    route = REPORT_ROUTES[report]
    params: dict[str, Any] = {}
    range_, start, end = _given(args, "range"), _given(args, "date_from"), _given(args, "date_to")
    if report == "labor_engineer":
        if _given(args, "search") is not None:
            params["search"] = str(_given(args, "search"))
        params["limit"] = common_args(args)[1]
    elif REPORT_PARAMS[report]:
        if start is not None and range_ is None:
            range_ = "custom"
        if range_ is not None:
            params["range"] = range_
        if start is not None:
            low, high = ("start", "end") if report == "calling_in" else ("date_from", "date_to")
            params[low], params[high] = parse_date(start, "date_from"), parse_date(end, "date_to")
        if _given(args, "project_id") is not None and "project_id" in REPORT_PARAMS[report]:
            params["project_id"] = require_uuid(args, "project_id")
        if _given(args, "status") is not None and "status" in REPORT_PARAMS[report]:
            params["status"] = str(_given(args, "status"))
    return [ToolRequest(route=route, path=route[1], params=params)]


# ── REPORT_KEEP: the whitelist trimmer ───────────────────────────────────────


@dataclass(frozen=True)
class _Gate:
    """A kept key that only some callers get: `viewer` (roles that may see
    bid amounts) and/or `detail` (response_format detailed)."""

    inner: Any
    viewer: bool = False
    detail: bool = False


def _viewer(inner: Any) -> _Gate:
    return _Gate(inner, viewer=True)


def _detail(inner: Any) -> _Gate:
    return _Gate(inner, detail=True)


def _viewer_detail(inner: Any) -> _Gate:
    return _Gate(inner, viewer=True, detail=True)


_META = {"range": True, "date_from": True, "date_to": True, "project_id": True, "anchor": True}
_REF = {"project_id": True, "number": True, "name": True, "current_stage": True}
_TREND = [{"label": True, "value": True}]
_BREAK = {"key": True, "label": True, "count": True, "avg_time_to_bid_hours": True,
          "total_bid_amount": _viewer(True)}
_BREAK_TIME = {"key": True, "label": True, "avg_hours": True, "samples": True}

REPORT_KEEP: dict[str, dict[str, Any]] = {
    "summary": {
        "total_projects": True, "by_current_stage": True, "submitted": True, "declined": True,
        "time_in_stage": [{"stage": True, "label": True, "avg_hours": True, "samples": True}],
        "outcomes": {"won": True, "lost": True, "no_award": True, "win_rate": True},
    },
    "overview": {
        "meta": _META, "projects_bid": True, "avg_time_to_bid_hours": True, "on_time_rate": True,
        "win_rate": True, "total_bid_amount": _viewer(True),
        "kpis": {
            "win_rate": True, "won": True, "lost": True, "no_award": True, "used_us_rate": True,
            "estimator_on_time_rate": True, "vendor_response_rate": True, "quote_coverage_rate": True,
            "ai_extraction_rate": True, "avg_quotes_per_project": True, "decline_rate": True,
            "bid_accuracy_pct": _viewer(True),
        },
        "breakdowns": _detail({"wage_type": [_BREAK], "est_value_band": [_BREAK], "labor_time": [_BREAK]}),
        "trend": _detail(_TREND),
        "value_trend": _viewer_detail(_TREND),
    },
    "send_out": {
        "meta": _META, "benchmark": True, "benchmark_redacted": True, "total": True, "on_time": True,
        "late": True, "no_benchmark": True, "trend": _detail(_TREND),
        "projects": [{**_REF, "submitted_at": True, "benchmark_at": True, "status": True}],
    },
    "estimator": {
        "meta": _META, "total": True, "on_time": True, "late": True, "incomplete": True,
        "avg_turnaround_hours": True,
        "projects": [{**_REF, "sent_to_estimator_at": True, "returned_at": True, "due_at": True,
                      "turnaround_hours": True, "status": True}],
    },
    "quotes": {
        "meta": _META, "total": True, "coverage_complete_count": True, "avg_coverage_pct": True,
        "quotes_on_time": True, "quotes_late": True, "manual_count": True, "ai_extracted_count": True,
        "ai_extraction_rate": True, "vendor_response_rate": True,
        "projects": [{**_REF, "coverage_complete": True, "coverage_pct": True, "quotes_on_time": True,
                      "quotes_late": True, "manual_count": True, "ai_extracted_count": True,
                      "vendor_response_rate": True}],
    },
    "cycle_time": {
        "meta": _META, "avg_end_to_end_hours": True,
        "time_in_stage": [{"stage": True, "label": True, "avg_hours": True, "samples": True}],
        "breakdowns": _detail({"wage_type": [_BREAK_TIME], "est_value_band": [_BREAK_TIME],
                               "labor_time": [_BREAK_TIME]}),
    },
    "win_loss": {
        "meta": _META, "won": True, "lost": True, "no_award": True, "win_rate": True,
        "bid_accuracy": _viewer({"avg_variance_pct": True, "samples": True}),
        "projects": [{**_REF, "result": True, "variance_pct": _viewer(True)}],
    },
    "gc_spread": {
        "meta": _META, "gc_send_count": True, "jobs_with_sends": True, "multi_gc_jobs": True,
        "total_bid_amount": _viewer(True), "avg_abs_spread_pct": _viewer(True),
        "by_gc": [{"gc_id": True, "gc_name": True, "times_bid": True, "multi_gc_jobs": True,
                   "total_amount": _viewer(True), "avg_signed_pct": _viewer(True),
                   "times_higher": _viewer(True), "times_lower": _viewer(True)}],
        "projects": [{**_REF, "gc_count": True, "avg_spread_pct": _viewer(True),
                      "max_abs_spread_pct": _viewer(True), "total_amount": _viewer(True)}],
    },
    "bid_invitations": {
        "meta": {"range": True, "date_from": True, "date_to": True, "open_ended": True, "bid_dates_visible": True},
        "summary": True,
        "rows": [{
            "project_id": True, "number": True, "name": True, "status": True, "invitation_at": True,
            "wage_label": True, "labor_note": True,
            "gcs": [{"gc_id": True, "name": True, "needs_by": True, "sent_at": True, "on_time": True}],
            "estimator_returned_at": True, "quotes_requested": True, "quotes_received": True,
            "sent_at": True, "on_time": True, "bid_at": _viewer(True), "bid_at_is_internal": _viewer(True),
        }],
    },
    "labor_engineer": {
        "meta": {"total": True, "limit": True, "offset": True, "actual_deltas_visible": True},
        "rows": [{
            "project_id": True, "number": True, "name": True, "internal_bid_at": True,
            "quotes_complete_at": True, "last_quoted_category": True, "missing_quote_categories": True,
            "category_count": True, "first_opened_at": True, "project_open_count": True,
            "labor_first_saved_at": True, "markup_completed_at": True, "sent_out_at": True,
            "sent_status": True, "sent_delta_seconds": True, "sent_after_actual_seconds": _viewer(True),
            "details_opened": True, "details_open_count": True,
        }],
    },
    "calling_in": {
        "rounds": True,
        "by_gc": [{"gc_id": True, "gc_name": True, "called": True, "missed": True, "median_hours_to_call": True}],
        "calls": [{"project_id": True, "project_number": True, "project_name": True, "gc_id": True,
                   "gc_name": True, "round": True, "called_at": True, "called_by_name": True,
                   "hours_to_call": True, "attempts_before": True}],
        "missed": [{"project_id": True, "project_number": True, "project_name": True, "gc_id": True,
                    "gc_name": True, "round": True, "window_closed_at": True}],
    },
}

# Lists that are structure (a trend, the stages), not rows: kept whole up to
# this many entries. Every other list is a row list and is cut at `limit`.
_STRUCTURE_LISTS = frozenset({"trend", "value_trend", "time_in_stage", "wage_type", "est_value_band", "labor_time",
                              "gcs"})
STRUCTURE_CAP = 60
_TEXT_CAP = 200
_PLAIN_DEPTH = 3


def is_amount_viewer(role: Any) -> bool:
    try:
        return Role(getattr(role, "value", role)) in ACTUAL_BID_VIEWER_ROLES
    except ValueError:
        return False


def _plain(value: Any, key: str, ctx: TrimContext, depth: int = 0) -> Any:
    """A kept value: scalars as they are (strings clipped, ids shortened),
    small dicts and lists of scalars copied."""
    if isinstance(value, str):
        if (key == "id" or key.endswith("_id")) and is_uuid(value):
            return ctx.short(value)
        return clip(value, _TEXT_CAP) or None
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if depth >= _PLAIN_DEPTH:
        return None
    if isinstance(value, Mapping):
        return compact({str(k): _plain(v, str(k), ctx, depth + 1) for k, v in value.items()})
    if isinstance(value, list):
        return [_plain(v, key, ctx, depth + 1) for v in value[:STRUCTURE_CAP]]
    return None


_DROP = object()


class _Cuts:
    """The row lists that were cut at the limit: (key, shown, total)."""

    def __init__(self) -> None:
        self.items: list[tuple[str, int, int]] = []


def _apply(value: Any, node: Any, key: str, ctx: TrimContext, flags: tuple[bool, bool], cuts: _Cuts) -> Any:
    viewer, detailed = flags
    if isinstance(node, _Gate):
        if (node.viewer and not viewer) or (node.detail and not detailed):
            return _DROP
        return _apply(value, node.inner, key, ctx, flags, cuts)
    if node is True:
        return _plain(value, key, ctx)
    if isinstance(node, dict):
        if not isinstance(value, Mapping):
            return _DROP
        out: dict[str, Any] = {}
        for name, sub in node.items():
            if name not in value:
                continue
            got = _apply(value[name], sub, name, ctx, flags, cuts)
            if got is not _DROP and got is not None:
                out[name] = got
        return out
    if isinstance(node, list):
        if not isinstance(value, list):
            return _DROP
        cap = STRUCTURE_CAP if key in _STRUCTURE_LISTS else ctx.limit
        items = [v for v in value if isinstance(v, Mapping)]
        if len(items) > cap:
            cuts.items.append((key, cap, len(items)))
        return [_apply(v, node[0], key, ctx, flags, cuts) for v in items[:cap]]
    return _DROP


def trim_report(report: str, body: Any, ctx: TrimContext) -> dict:
    """One report's payload through its whitelist, for the caller's role and
    response format. `strip_bid_amounts` runs last as the second guard."""
    flags = (is_amount_viewer(ctx.role), ctx.response_format == "detailed")
    cuts = _Cuts()
    out = _apply(dict_of(body), REPORT_KEEP[report], "", ctx, flags, cuts)
    out = dict(out) if isinstance(out, dict) else {}
    out = {"report": report, **out}
    if cuts.items:
        key, shown, total = cuts.items[0]
        out["hint"] = narrowing_hint(total, shown, "Narrow the range or a project, or raise limit.")
    cleaned = strip_bid_amounts(out, ctx.role)
    # An empty row list is the answer ("no bids in that range"), so only None and empty dicts go.
    return {k: v for k, v in cleaned.items() if v is not None and v != {}}


def _report_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    report = args.get("report")
    if report not in REPORT_KEEP:
        return {"error": "Unknown report."}
    return trim_report(str(report), payloads[0] if payloads else None, ctx)


register(ToolSpec(
    name="run_report",
    namespace="bidding",
    sub_app="bidding",
    route=REPORT_ROUTES["overview"],
    extra_routes=tuple(REPORT_ROUTES.values()),
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "report": {"type": "string", "enum": list(REPORT_NAMES), "description": (
            "summary: pipeline counts by stage and win rate. overview: bids, on-time and win rates, time to bid. "
            "send_out: sent on time vs late. estimator: estimator turnaround. quotes: vendor quote coverage. "
            "cycle_time: time in each stage. win_loss: results. gc_spread: how we priced each GC against the "
            "others. bid_invitations: each bid with its GCs, sends and quote counts. labor_engineer: the labor "
            "engineer's timeline per bid. calling_in: GC follow-up calls.")},
        "range": nullable({"type": "string", "enum": list(RANGE_WORDS), "description": (
            "Rolling reports (overview, send_out, estimator, quotes, cycle_time, win_loss, gc_spread, "
            "calling_in): day, week, month, quarter, year or custom. bid_invitations: today, wtd, mtd, ytd, "
            "yesterday, last_week, last_month, last_year, past_5_years or custom. summary and labor_engineer "
            "take none. Null uses the report's default.")}),
        "date_from": nullable({"type": "string", "description": "custom range start, YYYY-MM-DD; null otherwise."}),
        "date_to": nullable({"type": "string", "description": "custom range end, YYYY-MM-DD, inclusive; null otherwise."}),
        "project_id": nullable({"type": "string", "description": (
            "One project (short or full id) for the rolling reports except calling_in; null for all.")}),
        "status": nullable({"type": "string", "enum": list(STATUS_WORDS), "description": (
            "Only bids in this state (rolling reports except calling_in); null for all.")}),
        "search": nullable({"type": "string", "description": (
            "labor_engineer only: a project number or name fragment; null for all.")}),
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_report_trim,
    progress_label="Running a report",
    roles=INTERNAL_ROLES,
    description=(
        "Use for the numbers behind the Analytics pages: one of eleven reports for a date range, answered inline "
        "as headline figures and a table. Each report takes only its own filters and range words (see the "
        "arguments). Dollar figures appear only for roles that may see bid amounts. For a spreadsheet file use "
        "build_report instead. When rows are cut, narrow the range rather than quoting a partial total."
    ),
    requests=_report_requests,
    precheck=_report_precheck,
    taint_fields=("*", "name", "project_name", "gc_name", "vendor_name", "labor_note", "gcs"),
    deferred=True,
    keywords=("report", "analytics", "numbers", "win", "rate", "stats", "statistics", "performance", "cycle",
              "overview", "calling", "invitations", "gc", "spread"),
))


# ── coded_report ─────────────────────────────────────────────────────────────

CODED_RECIPES = ("estimator_vs_bid", "quote_readiness")
CODED_ROW_CAP = 25
CODED_CATEGORY_CAP = 12


def _coded_problem(args: Mapping[str, Any]) -> str | None:
    recipe = args.get("recipe")
    if recipe not in CODED_RECIPES:
        return f"recipe must be one of {', '.join(CODED_RECIPES)}."
    period, week = _given(args, "period"), _given(args, "week")
    try:
        if period is not None:
            if recipe != "estimator_vs_bid":
                return "period goes with estimator_vs_bid only."
            clean_period(period)
        if week is not None:
            if recipe != "quote_readiness":
                return "week goes with quote_readiness only."
            clean_week(week)
    except RecipeError as exc:
        return str(exc)
    return None


def _coded_precheck(args: Mapping[str, Any]) -> tuple[str, str] | None:
    problem = _coded_problem(args)
    return ("bad_args", problem) if problem else None


def _coded_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    problem = _coded_problem(args)
    if problem:
        raise ToolArgumentError(problem)
    recipe = str(args["recipe"])
    params: dict[str, Any] = {}
    if _given(args, "period") is not None:
        params["period"] = clean_period(_given(args, "period"))
    if _given(args, "week") is not None:
        params["week"] = clean_week(_given(args, "week"))
    return [ToolRequest(route=RECIPE_ROWS_ROUTE, path=f"/assistant/recipes/{recipe}/rows", params=params)]


def _coded_category(cat: Mapping[str, Any]) -> dict:
    return compact({
        "category": clip(cat.get("category"), 60),
        "vendor_emails": cat.get("vendor_emails"),
        "quotes_received": cat.get("quotes_received"),
        "covered": cat.get("covered"),
        "counts_toward_key": cat.get("counts_toward_key"),
    })


def _coded_extra(recipe: str, extra: Mapping[str, Any], viewer: bool, detailed: bool) -> dict:
    if recipe == "estimator_vs_bid":
        if not viewer:
            return compact({"code": extra.get("code"), "message": clip(extra.get("message"), 200)})
        return compact({
            "estimator_base": extra.get("estimator_base"),
            "estimator_name": clip(extra.get("estimator_name"), 80),
            "round": extra.get("round"),
            "our_min": extra.get("our_min"),
            "gc_count": extra.get("gc_count"),
            "difference": extra.get("difference"),
            "difference_pct": extra.get("difference_pct"),
            "verdict": extra.get("verdict"),
            "code": extra.get("code"),
            "message": clip(extra.get("message"), 200),
            "revised_after_send": True if extra.get("revised_after_send") else None,
            "first_sent_at_pt": pt(extra.get("first_sent_at")),
            "alternates_note": clip(extra.get("alternates_note"), 200),
        })
    categories = rows_of(extra.get("categories"))
    out = compact({
        "required": extra.get("required"),
        "covered": extra.get("covered"),
        "key_quotes": extra.get("key_quotes"),
        "complete": extra.get("complete"),
        "internal_due": extra.get("internal_due"),
        "bid_date": extra.get("bid_date") if viewer else None,
        "estimator_returned_at_pt": pt(extra.get("estimator_returned_at")),
        "estimator_returned_text": clip(extra.get("estimator_returned_text"), 120),
    })
    if detailed and categories:
        out["categories"] = [_coded_category(c) for c in categories[:CODED_CATEGORY_CAP]]
    return out


def _evb_month(body: Mapping[str, Any], rows: list[dict], viewer: bool) -> dict:
    """The whole month of estimator_vs_bid, whatever the row cap: the counts
    sentence and verdict tally over every row, the sent-early count and, for
    an amount viewer, the route's like-for-like and all-rows totals."""
    verdicts = [str(dict_of(r.get("extra")).get("verdict") or "") for r in rows]
    tally = {"higher": verdicts.count("We bid higher"), "lower": verdicts.count("We bid lower"),
             "same": verdicts.count("Same price")}
    tally["not_compared"] = len(rows) - sum(tally.values())
    out: dict[str, Any] = {
        "summary": clip(_recipes.message_for("estimator_vs_bid", {**dict(body), "rows": rows})[0], 300),
        "verdicts": tally,
        "sent_early_count": len(rows_of(body.get("sent_early"))) or None,
    }
    totals = dict_of(body.get("totals"))
    if viewer and totals:
        like, every = dict_of(totals.get("like_for_like")), dict_of(totals.get("all_rows"))
        out["totals"] = compact({
            "like_for_like": compact({k: like.get(k) for k in ("count", "estimator_total", "our_total",
                                                               "difference", "difference_pct")}) or None,
            "all_rows": compact({"count": every.get("count"), "estimator_total": every.get("estimator_total"),
                                 "our_total": every.get("our_total"), "note": clip(every.get("note"), 200)}) or None,
        }) or None
    return out


def _coded_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    body = dict_of(payloads[0] if payloads else None)
    recipe = str(body.get("recipe") or args.get("recipe") or "")
    viewer = is_amount_viewer(ctx.role)
    window = dict_of(body.get("window"))
    rows = rows_of(body.get("rows"))
    cap = min(ctx.limit, CODED_ROW_CAP)
    shown = rows[:cap]
    items = []
    for r in shown:
        project = dict_of(r.get("project"))
        item = compact({
            "project": compact({
                "id": ctx.short(project.get("id")) if project.get("id") else None,
                "number": project.get("number"),
                "name": clip(project.get("name"), 120),
            }),
            "missing_categories": [clip(c, 60) for c in r.get("missing_categories") or []][:CODED_CATEGORY_CAP]
            if recipe == "quote_readiness" else None,
        })
        item.update(_coded_extra(recipe, dict_of(r.get("extra")), viewer, ctx.response_format == "detailed"))
        items.append(item)
    # Past CODED_ROW_CAP a higher limit shows nothing more.
    how = ("Build the spreadsheet with build_report for every row." if ctx.limit >= CODED_ROW_CAP
           else "Raise limit, or build the spreadsheet with build_report.")
    out: dict[str, Any] = {
        "recipe": recipe,
        "window": clip(window.get("label"), 80),
        "when": clip(window.get("phrase"), 60),
        "count": len(rows),
        "hint": narrowing_hint(len(rows), len(shown), how),
    }
    if recipe == "estimator_vs_bid":
        out.update(_evb_month(body, rows, viewer))
    result = compact(strip_bid_amounts(out, ctx.role))
    # An empty list is the answer ("nothing due that week"), not noise to drop.
    result["rows"] = items
    return result


register(ToolSpec(
    name="coded_report",
    namespace="bidding",
    sub_app="bidding",
    route=RECIPE_ROWS_ROUTE,
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "recipe": {"type": "string", "enum": list(CODED_RECIPES), "description": (
            "estimator_vs_bid: for a month, the estimator's base bid price against the lowest price we sent a GC "
            "(roles that may see bid amounts only). quote_readiness: for a week, whether each bid has its key "
            "vendor quotes, and when the estimator returned the estimate to us.")},
        "period": nullable({"type": "string", "description": (
            "estimator_vs_bid only: YYYY-MM, last_month or this_month; null means last_month.")}),
        "week": nullable({"type": "string", "description": (
            "quote_readiness only: this_week, next_week or the Monday date (YYYY-MM-DD); null means next_week.")}),
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_coded_trim,
    progress_label="Reading the report",
    roles=INTERNAL_ROLES,
    description=(
        "Use for the owner's two reports: \"how did the estimator's price compare with ours last month\" and "
        "\"does every bid next week have its key quotes\". Rows come with a verdict or a key quotes count. "
        "estimator_vs_bid refuses roles that may not see bid amounts; say so rather than guess. For a spreadsheet "
        "use build_report."
    ),
    requests=_coded_requests,
    precheck=_coded_precheck,
    taint_fields=("name",),
    deferred=True,
    keywords=("estimator", "price", "compare", "quote", "readiness", "key", "week", "month", "verdict", "bid"),
))


# ── quotes_summary ───────────────────────────────────────────────────────────

CANDIDATE_CAP = 5
PRICING_KEYS = ("materials_amount", "labor_amount", "markup_amount", "bid_price")


def _quotes_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    pid = require_uuid(args, "project_id")
    requests = [ToolRequest(route=VENDOR_SELECTION_GET, path=f"/projects/{pid}/vendor-selection")]
    if args.get("include_pricing") is True:
        requests.append(ToolRequest(route=PRICING_SUMMARY_GET, path=f"/projects/{pid}/pricing-summary",
                                    optional=True))
    return requests


def _candidate(q: Mapping[str, Any], detailed: bool) -> dict:
    return compact({
        "vendor_name": clip(q.get("vendor_name"), 80),
        "contact_name": clip(q.get("contact_name"), 80) if detailed else None,
        "amount": q.get("amount"),
        "tax_included": q.get("tax_included"),
        "total": q.get("total"),
        "origin": q.get("origin"),
        "received_at_pt": pt(q.get("received_at")),
        "late": True if q.get("received_after_submission") else None,
        "selected": True if q.get("is_selected") else None,
        "filename": clip(q.get("quote_file_name"), 120) if detailed else None,
        "notes": clip(q.get("notes"), 200) if detailed else None,
    })


def _category(rfq: Mapping[str, Any], ctx: TrimContext) -> dict:
    detailed = ctx.response_format == "detailed"
    quotes = rows_of(rfq.get("quotes"))
    selected_id = rfq.get("selected_quote_id")
    winner = next((q for q in quotes if selected_id and q.get("id") == selected_id), None)
    shown = [q for q in quotes[:CANDIDATE_CAP]]
    return compact({
        "rfq_id": ctx.short(rfq.get("rfq_id")) if rfq.get("rfq_id") else None,
        "category": clip(rfq.get("category_name"), 80),
        "general": True if rfq.get("is_general") else None,
        "section": rfq.get("pricing_section"),
        "sent": bool(rfq.get("was_sent")),
        "winner": _candidate(winner, detailed) if winner else None,
        "quote_count": len(quotes),
        "late_count": sum(1 for q in quotes if q.get("received_after_submission")) or None,
        "candidates": [_candidate(q, detailed) for q in shown],
    })


def _quotes_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    cats = rows_of(payloads[0] if payloads else None)
    shown = cats[: ctx.limit]
    viewer = is_amount_viewer(ctx.role)
    out: dict[str, Any] = {
        "categories": len(cats),
        "priced": sum(1 for c in cats if c.get("selected_quote_id")),
        "hint": narrowing_hint(len(cats), len(shown), "Raise limit."),
    }
    categories = [_category(c, ctx) for c in shown]
    if args.get("include_pricing") is True:
        pricing = dict_of(payloads[1] if len(payloads) > 1 else None)
        if viewer and pricing:
            out["pricing"] = compact({k: pricing.get(k) for k in PRICING_KEYS})
        elif not viewer:
            out["pricing_note"] = "The pricing summary is not shown for your role."
    result = compact(strip_bid_amounts(out, ctx.role))
    result["rows"] = categories
    return result


register(ToolSpec(
    name="quotes_summary",
    namespace="bidding",
    sub_app="bidding",
    route=VENDOR_SELECTION_GET,
    extra_routes=(PRICING_SUMMARY_GET,),
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "project_id": {"type": "string", "description": "The project's id (short or full) from app_search."},
        "include_pricing": nullable({"type": "boolean", "description": (
            "True adds the project's pricing summary (materials, labor, markup, bid price) for roles that may see "
            "bid amounts; null or false leaves it out.")}),
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_quotes_trim,
    progress_label="Reading the vendor quotes",
    roles=INTERNAL_ROLES,
    description=(
        "Use for \"what are the quotes on this bid\" or \"who is winning switchgear\": per material category, the "
        "selected vendor and up to five candidates cheapest first (vendor, amount, tax, origin, when received, "
        "late flag). Vendor quote amounts are shown to every internal role; our own pricing summary only to roles "
        "that may see bid amounts."
    ),
    requests=_quotes_requests,
    taint_fields=("vendor_name", "contact_name", "notes", "filename"),
    deferred=True,
    keywords=("quotes", "quote", "vendor", "vendors", "winner", "selected", "cheapest", "category", "pricing"),
))


# ── build_report ─────────────────────────────────────────────────────────────

# The specs the route builds (assistant_reports.SPEC_ROLES minus the
# approval-bound person_evidence) and the body fields each takes.
BUILD_SPECS = ("bid_invitations", "quote_readiness", "estimator_vs_bid", "project_timeline",
               "project_file_access", "change_history", "ai_usage")
BUILD_PARAMS: dict[str, tuple[str, ...]] = {
    "bid_invitations": ("range", "date_from", "date_to"),
    "quote_readiness": ("week",),
    "estimator_vs_bid": ("period",),
    "project_timeline": ("project_id", "date_from", "date_to"),
    "project_file_access": ("project_id", "date_from", "date_to"),
    "change_history": ("project_id", "date_from", "date_to"),
    "ai_usage": ("project_id", "date_from", "date_to"),
}
BUILD_PROJECT_REQUIRED = ("project_timeline", "project_file_access", "change_history")
_BUILD_ARGS = ("range", "date_from", "date_to", "week", "period", "project_id")
PREVIEW_ROW_CAP = 10


def _build_problem(args: Mapping[str, Any]) -> str | None:
    spec = args.get("spec")
    if spec not in BUILD_SPECS:
        return f"spec must be one of {', '.join(BUILD_SPECS)}."
    taken = BUILD_PARAMS[spec]
    for key in _BUILD_ARGS:
        if _given(args, key) is not None and key not in taken:
            return f"The {spec} report does not take '{key}'. It takes: {', '.join(taken)}."
    range_, start, end = _given(args, "range"), _given(args, "date_from"), _given(args, "date_to")
    if spec == "bid_invitations":
        if range_ is not None and range_ not in RANGE_CALENDAR:
            return f"range '{range_}' is not for bid_invitations. Use one of: {', '.join(RANGE_CALENDAR)}."
        if range_ == "custom" and (start is None or end is None):
            return "A custom range needs date_from and date_to."
        if range_ != "custom" and range_ is not None and start is not None:
            return "date_from and date_to go with range custom only."
    if (start is None) != (end is None):
        return "A date window needs both date_from and date_to."
    if start is not None:
        try:
            a, b = parse_date(start, "date_from"), parse_date(end, "date_to")
        except ToolArgumentError as exc:
            return str(exc)
        if a > b:
            return "date_from must be on or before date_to."
    if spec in BUILD_PROJECT_REQUIRED and _given(args, "project_id") is None:
        return f"The {spec} report needs a project_id from app_search."
    if _given(args, "project_id") is not None and not is_uuid(args.get("project_id")):
        return "project_id must be a project id from app_search (the short id or the full id)."
    try:
        if _given(args, "period") is not None:
            clean_period(_given(args, "period"))
        if _given(args, "week") is not None:
            clean_week(_given(args, "week"))
    except RecipeError as exc:
        return str(exc)
    return None


def _build_precheck(args: Mapping[str, Any]) -> tuple[str, str] | None:
    problem = _build_problem(args)
    return ("bad_args", problem) if problem else None


def _build_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    problem = _build_problem(args)
    if problem:
        raise ToolArgumentError(problem)
    body: dict[str, Any] = {"spec": str(args["spec"])}
    for key in ("range", "week", "period"):
        value = _given(args, key)
        if value is not None:
            body[key] = clip(value, 20) if key != "range" else value
    for key in ("date_from", "date_to"):
        if _given(args, key) is not None:
            body[key] = parse_date(args[key], key)
    if _given(args, "project_id") is not None:
        body["project_id"] = require_uuid(args, "project_id")
    return [ToolRequest(route=BUILD_REPORT_POST, path=BUILD_REPORT_POST[1], json=body)]


def _preview_row(row: Mapping[str, Any]) -> dict:
    return {str(k): (clip(v, 200) if isinstance(v, str) else v)
            for k, v in row.items() if isinstance(v, (str, int, float, bool)) or v is None}


def _build_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    """The workbook's id (never a URL: the link is minted at the click),
    its name, the counts and the first rows, amounts stripped per role."""
    body = dict_of(payloads[0] if payloads else None)
    rows = [_preview_row(r) for r in rows_of(body.get("preview_rows"))[:PREVIEW_ROW_CAP]]
    out: dict[str, Any] = {
        "report_id": ctx.short(body.get("report_id")) if body.get("report_id") else None,
        "title": clip(body.get("title"), 160),
        "filename": clip(body.get("filename"), 160),
        "spec": body.get("spec"),
        "row_count": body.get("row_count"),
        "truncated": True if body.get("truncated") else None,
        "totals": _preview_row(dict_of(body.get("totals"))) or None,
        "expires_at_pt": clip(body.get("expires_at_pt"), 60),
        "hint": "The file is attached under your answer for the user to download. Do not describe a link.",
    }
    out["preview_rows"] = rows
    return compact(strip_bid_amounts(out, ctx.role))


register(ToolSpec(
    name="build_report",
    namespace="report",
    sub_app="bidding",
    route=BUILD_REPORT_POST,
    effect=EFFECT_HOUSEKEEPING,
    confirm=False,
    schema=schema({
        "spec": {"type": "string", "enum": list(BUILD_SPECS), "description": (
            "bid_invitations: every bid with its GCs, sends and quote counts (range). quote_readiness: key quotes "
            "per bid for a week (week). estimator_vs_bid: estimator price against ours for a month (period; roles "
            "that may see bid amounts). project_timeline, project_file_access, change_history: one project's "
            "history (project_id; Executive and IT Admin). ai_usage: AI calls and cost (Executive and IT Admin).")},
        "range": nullable({"type": "string", "enum": list(RANGE_CALENDAR), "description": (
            "bid_invitations only: today, wtd, mtd, ytd, yesterday, last_week, last_month, last_year, "
            "past_5_years or custom; null means wtd.")}),
        "date_from": nullable({"type": "string", "description": (
            "Window start, YYYY-MM-DD (custom bid_invitations, or the evidence specs); null otherwise.")}),
        "date_to": nullable({"type": "string", "description": "Window end, YYYY-MM-DD, inclusive; null otherwise."}),
        "week": nullable({"type": "string", "description": (
            "quote_readiness only: this_week, next_week or the Monday date; null means next_week.")}),
        "period": nullable({"type": "string", "description": (
            "estimator_vs_bid only: YYYY-MM, last_month or this_month; null means last_month.")}),
        "project_id": nullable({"type": "string", "description": (
            "The project (short or full id) for project_timeline, project_file_access, change_history and "
            "optionally ai_usage; null otherwise.")}),
        "response_format": RESPONSE_FORMAT_PROP,
    }),
    response_trimmer=_build_trim,
    progress_label="Building a spreadsheet",
    roles=INTERNAL_ROLES,
    description=(
        "Build one spreadsheet (.xlsx) of a report for the user to download; it appears under your answer and "
        "stays available for 7 days. Use it when the user asks for a file, a spreadsheet or an export; answer "
        "simple questions inline with run_report or coded_report instead. You get the counts and the first rows, "
        "never a link. Per-person evidence is not available here."
    ),
    requests=_build_requests,
    precheck=_build_precheck,
    max_proposals_per_turn=2,
    max_tainted_calls_per_turn=1,
    taint_fields=("name", "project_name", "gc_name", "vendor_name", "labor_note", "gcs", "project"),
    deferred=True,
    keywords=("spreadsheet", "xlsx", "excel", "export", "file", "download", "workbook", "report", "sheet"),
))

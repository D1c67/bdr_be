"""Routine recipes: the catalog and its pure helpers
(docs/ASSISTANT_PHASE3_PLAN.md 3.2.1).

A recipe is code over the due digest's membership and readiness rules; it
runs no model (deviation D4). Titles are built from enums and the project
number only, never model text, so no schedule field carries tainted text
into the prompt, a bell or an email (deviation D3). Message content holds
counts only; project names go into the table and project_link blocks,
never into `content` (`_history` replays content only).

Pure: nothing here reads the database. The data side is
app/services/assistant_recipes.py.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.assistant import rrules
from app.assistant.registry import (
    EFFECT_READ,
    ToolRequest,
    ToolSpec,
    TrimContext,
    nullable,
    schema,
    validate,
)
from app.core.roles import ACTUAL_BID_VIEWER_ROLES, INTERNAL_ROLES, Role


@dataclass(frozen=True)
class Recipe:
    key: str
    label: str                    # "Bids due today"
    sub_app: str                  # "bidding" for all four
    accepts_horizon: bool         # horizon_days param
    accepts_watch: bool           # project_id param (a project watch)
    digest_overlap: bool          # due_today
    reminders_overlap: bool       # due_this_week, missing_quotes
    # Phase 4 (docs/ASSISTANT_PHASE4_PLAN.md 3.10.1): who may run the recipe
    # and which extra parameter it takes. The defaults keep the four phase 3
    # entries valid without edits.
    roles: frozenset[Role] = INTERNAL_ROLES
    accepts_period: bool = False  # estimator_vs_bid: YYYY-MM | last_month | this_month
    accepts_week: bool = False    # quote_readiness: this_week | next_week | ISO Monday


RECIPES = {
    "due_today":         Recipe("due_today", "Bids due today", "bidding", False, False, True, False),
    "due_this_week":     Recipe("due_this_week", "Bids due this week", "bidding", False, False, False, True),
    "missing_quotes":    Recipe("missing_quotes", "Bids missing vendor quotes", "bidding", True, True, False, True),
    "labor_not_entered": Recipe("labor_not_entered", "Bids with no labor entered", "bidding", True, True, False, False),
    # The owner's two coded reports (docs/ASSISTANT_PHASE4_PLAN.md 3.10).
    "estimator_vs_bid":  Recipe("estimator_vs_bid", "Estimator vs our price", "bidding", False, False, False, False,
                                roles=ACTUAL_BID_VIEWER_ROLES, accepts_period=True),
    "quote_readiness":   Recipe("quote_readiness", "Key quotes for the bid week", "bidding", False, False, False,
                                False, accepts_week=True),
}
PHASE4_RECIPES = ("estimator_vs_bid", "quote_readiness")
HORIZON_DEFAULT, HORIZON_MIN, HORIZON_MAX = 7, 1, 30
MAX_TABLE_ROWS = 25
MAX_PROJECT_LINKS = 20

TABLE_COLUMNS = ["Project", "Due (Pacific)", "Missing"]

PERIOD_DEFAULT = "last_month"
PERIOD_WORDS = ("last_month", "this_month")
WEEK_DEFAULT = "next_week"
WEEK_WORDS = ("this_week", "next_week")
_PERIOD_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
PERIOD_YEARS = (2000, 2100)


def _qr_columns(show_bid: bool) -> list[str]:
    if show_bid:
        return ["Project", "Bid date", "Internal due", "Key quotes", "Estimator returned"]
    return ["Project", "Internal due", "Key quotes", "Estimator returned"]


def table_columns(recipe: str, role: Any = None) -> list[str]:
    """The chat table's columns per recipe. quote_readiness has no "Bid date"
    column for a role outside ACTUAL_BID_VIEWER_ROLES (the actual date is
    confidential; docs/ASSISTANT_PHASE4_PLAN.md D17)."""
    get_recipe(recipe)
    if recipe == "estimator_vs_bid":
        return ["Project", "Sent (Pacific)", "Estimator", "Our price", "Difference", "Verdict"]
    if recipe == "quote_readiness":
        return _qr_columns(role in ACTUAL_BID_VIEWER_ROLES)
    return list(TABLE_COLUMNS)


# A watch title names the one bid: "Labor not entered on 26.10.7267".
_WATCH_LABELS = {
    "missing_quotes": "Vendor quotes missing on",
    "labor_not_entered": "Labor not entered on",
}
# The "nothing to report" sentence per recipe, and per watch.
_EMPTY_TEXT = {
    "due_today": "No bids are due today.",
    "due_this_week": "No bids are due this week.",
    "missing_quotes": "No bids in this window are missing vendor quotes.",
    "labor_not_entered": "No bids in this window are missing labor.",
}
_WATCH_EMPTY_TEXT = {
    "missing_quotes": "The watched bid has a vendor quote in every category.",
    "labor_not_entered": "The watched bid has its labor entered.",
}
# Project numbers are BDR-assigned ("26.10.7267"); anything else is dropped
# from a title rather than carried into it.
_NUMBER_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class RecipeError(ValueError):
    """Recipe parameters the server refuses. `code` is "bad_params", or
    "recipe_not_found" for an unknown recipe key."""

    code = "bad_params"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


def get_recipe(recipe: str) -> Recipe:
    found = RECIPES.get(recipe) if isinstance(recipe, str) else None
    if found is None:
        raise RecipeError("There is no routine recipe with that name.", code="recipe_not_found")
    return found


def clean_period(value: Any) -> str:
    """`YYYY-MM`, `last_month` or `this_month` (case and surrounding spaces
    ignored). Raises RecipeError(code="period_invalid")."""
    text = value.strip().lower() if isinstance(value, str) else ""
    if text in PERIOD_WORDS:
        return text
    match = _PERIOD_RE.match(text)
    if match and PERIOD_YEARS[0] <= int(match.group(1)) <= PERIOD_YEARS[1]:
        return text
    raise RecipeError("The month must be YYYY-MM, last_month or this_month.", code="period_invalid")


def clean_week(value: Any) -> str:
    """`this_week`, `next_week` or an ISO date that is a Monday. Raises
    RecipeError(code="week_invalid")."""
    text = value.strip().lower() if isinstance(value, str) else ""
    if text in WEEK_WORDS:
        return text
    try:
        monday = date.fromisoformat(text) if len(text) == 10 else None
    except ValueError:
        monday = None
    if monday is None or monday.weekday() != 0:
        raise RecipeError("The week must be this_week, next_week or the date of a Monday (YYYY-MM-DD).",
                          code="week_invalid")
    return monday.isoformat()


def resolve_period(period: str | None, today: date) -> tuple[date, date]:
    """(first day of the month, first day of the next month) for a cleaned
    period; None means PERIOD_DEFAULT. `today` is the Pacific date."""
    value = period or PERIOD_DEFAULT
    if value == "this_month":
        first = today.replace(day=1)
    elif value == "last_month":
        first = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    else:
        year, month = value.split("-")
        first = date(int(year), int(month), 1)
    following = date(first.year + (1 if first.month == 12 else 0), first.month % 12 + 1, 1)
    return first, following


def resolve_week(week: str | None, today: date) -> date:
    """The Monday of the week for a cleaned `week`; None means WEEK_DEFAULT.
    `today` is the Pacific date."""
    value = week or WEEK_DEFAULT
    monday = today - timedelta(days=today.weekday())
    if value == "this_week":
        return monday
    if value == "next_week":
        return monday + timedelta(days=7)
    return date.fromisoformat(value)


def period_label(first: date) -> str:
    """"September 2026"."""
    return f"{first.strftime('%B')} {first.year}"


def validate_params(recipe: str, horizon_days: int | None, project_id: str | None, *,
                    period: str | None = None, week: str | None = None) -> dict:
    """{} or {"horizon_days": n} or {"project_id": uuid} (a watch replaces
    the horizon), or {"period": ...} or {"week": ...} for the two phase 4
    recipes (docs/ASSISTANT_PHASE4_PLAN.md 3.10.1; a missing period or week
    stays out of the dict and the data side applies PERIOD_DEFAULT or
    WEEK_DEFAULT). Raises RecipeError for a parameter the recipe does not
    accept, a horizon outside HORIZON_MIN..HORIZON_MAX, a bad uuid, a bad
    period ("period_invalid") or a bad week ("week_invalid"). A horizon
    passed next to a watch is dropped: the watch reads one bid, not a
    window."""
    spec = get_recipe(recipe)
    period = None if period in (None, "") else period
    week = None if week in (None, "") else week
    if period is not None and not spec.accepts_period:
        raise RecipeError(f"{spec.label} does not take a month.")
    if week is not None and not spec.accepts_week:
        raise RecipeError(f"{spec.label} does not take a week.")
    if project_id not in (None, ""):
        if not spec.accepts_watch:
            raise RecipeError(f"{spec.label} cannot watch one project.")
        try:
            return {"project_id": str(uuid.UUID(str(project_id)))}
        except (ValueError, TypeError, AttributeError):
            raise RecipeError("The project id must be a full project id.") from None
    if period is not None or week is not None:
        if horizon_days is not None:
            raise RecipeError(f"{spec.label} does not take a number of days.")
        return {"period": clean_period(period)} if period is not None else {"week": clean_week(week)}
    if horizon_days is None:
        return {}
    if not spec.accepts_horizon:
        raise RecipeError(f"{spec.label} does not take a number of days.")
    if isinstance(horizon_days, bool) or not isinstance(horizon_days, int):
        raise RecipeError(f"The number of days must be a whole number from {HORIZON_MIN} to {HORIZON_MAX}.")
    if not HORIZON_MIN <= horizon_days <= HORIZON_MAX:
        raise RecipeError(f"The number of days must be from {HORIZON_MIN} to {HORIZON_MAX}.")
    return {"horizon_days": horizon_days}


def title_for(recipe: str, cadence: Any, project_number: str | None = None) -> str:
    """For example "Bids due today, weekdays at 7:00 AM" or "Labor not entered on
    26.10.7267, weekdays at 8:00 AM". `cadence` is an rrules.Cadence. Built
    from enums and the project number only."""
    spec = get_recipe(recipe)
    when = rrules.short_text(cadence)
    number = _NUMBER_SAFE.sub("", str(project_number or ""))[:40]
    if number and recipe in _WATCH_LABELS:
        return f"{_WATCH_LABELS[recipe]} {number}, {when}"
    return f"{spec.label}, {when}"


# ── result hash ──────────────────────────────────────────────────────────────


def _row_key(row: Mapping[str, Any], recipe: str | None = None) -> list:
    project = row.get("project") or {}
    extra = row.get("extra") or {}
    if recipe == "estimator_vs_bid":
        # (project id, estimator base, our min, verdict): a reworded message
        # or a moved date is no change; a new price is.
        return [str(project.get("id") or ""), extra.get("estimator_base"), extra.get("our_min"),
                extra.get("verdict")]
    if recipe == "quote_readiness":
        return [str(project.get("id") or ""), extra.get("covered"), extra.get("required"),
                bool(extra.get("estimator_returned_at"))]
    checks = row.get("checks") or {}
    missing = row.get("missing_categories")
    return [
        str(project.get("id") or ""),
        bool(checks.get("has_quotes")),
        bool(checks.get("has_labor")),
        bool(checks.get("has_markups")),
        sorted(str(m) for m in missing) if missing is not None else None,
    ]


def result_hash(recipe: str, rows: list[dict]) -> str:
    """sha256 over canonical JSON of the recipe key and the sorted rows'
    keys. The phase 3 recipes hash (project_id, has_quotes, has_labor,
    has_markups, sorted missing categories); estimator_vs_bid hashes
    (project id, estimator base, our min, verdict) and quote_readiness
    (project id, covered, required, returned). No dates, no greeting, no
    HTML, so an unchanged set is no_change even when the window rolls."""
    keys = sorted((_row_key(r, recipe) for r in rows or ()), key=lambda k: json.dumps(k))
    body = json.dumps({"recipe": recipe, "rows": keys}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ── message ──────────────────────────────────────────────────────────────────


def project_label(project: Mapping[str, Any]) -> str:
    """The digest's label: "Name (#26.10.7267)"; a redacted name falls back."""
    label = str(project.get("name") or "").strip() or "Untitled project"
    if project.get("number"):
        label = f"{label} (#{project['number']})"
    return label


def due_text(value: Any) -> str:
    """Fri, Oct 23, 2:00 PM (Pacific), or empty when there is no date."""
    if not value:
        return ""
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        return ""
    local = dt.astimezone(rrules.PT)
    return f"{local.strftime('%a, %b')} {local.day}, {rrules.clock_text(local.hour, local.minute)}"


def short_date(value: Any) -> str:
    """10/23/2026 in Pacific time, or empty when there is no date."""
    if not value:
        return ""
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        return ""
    local = dt.astimezone(rrules.PT)
    return f"{local.month}/{local.day}/{local.year}"


def _whole_dollars(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def money_text(value: Any, *, signed: bool = False) -> str:
    """$12,511,190 (whole dollars; the workbook keeps the cents), "" for no
    value. `signed` adds a plus sign to a positive amount."""
    amount = _whole_dollars(value)
    if amount is None:
        return ""
    sign = "-" if amount < 0 else ("+" if signed and amount > 0 else "")
    return f"{sign}${abs(amount):,}"


def _extra(row: Mapping[str, Any]) -> Mapping[str, Any]:
    extra = row.get("extra")
    return extra if isinstance(extra, Mapping) else {}


def _evb_cells(row: Mapping[str, Any]) -> list[str]:
    extra = _extra(row)
    code = extra.get("code")
    base = money_text(extra.get("estimator_base"))
    if not base:
        base = "Not read yet" if code == "not_loaded_yet" else "No price"
    ours = money_text(extra.get("our_min")) or "No price"
    diff = ""
    if extra.get("difference") not in (None, ""):
        diff = money_text(extra["difference"], signed=True)
        if extra.get("difference_pct") not in (None, ""):
            try:
                diff += f" ({Decimal(str(extra['difference_pct'])):+.1f}%)"
            except (InvalidOperation, ValueError):
                pass
    verdict = str(extra.get("verdict") or extra.get("message") or "")[:120]
    return [project_label(row.get("project") or {}), due_text(extra.get("first_sent_at")), base, ours, diff, verdict]


def _qr_cells(row: Mapping[str, Any], show_bid: bool) -> list[str]:
    extra = _extra(row)
    project = row.get("project") or {}
    cells = [project_label(project)]
    if show_bid:
        cells.append(str(extra.get("bid_date") or ""))
    cells.append(str(extra.get("internal_due") or due_text(project.get("internal_bid_at"))))
    cells.append(str(extra.get("key_quotes") or "n/a"))
    cells.append(short_date(extra.get("estimator_returned_at")) or "Not yet")
    return cells


def table_for(recipe: str, rows: list[dict]) -> tuple[list[str], list[list[str]]]:
    """(columns, cell rows) of the two phase 4 recipes, built from the rows
    only (the route's payload keeps rows, window and watch). A "Bid date"
    column appears for quote_readiness only when a row carries `bid_date`,
    which the data side sets for viewer roles alone."""
    if recipe == "estimator_vs_bid":
        return table_columns(recipe), [_evb_cells(r) for r in rows]
    show_bid = any("bid_date" in _extra(r) for r in rows)
    return _qr_columns(show_bid), [_qr_cells(r, show_bid) for r in rows]


def missing_text(recipe: str, row: Mapping[str, Any]) -> str:
    """The Missing cell: the categories for missing_quotes ("No RFQ yet"
    when the project has none), labor for labor_not_entered, and the three
    digest checks for the due recipes."""
    checks = row.get("checks") or {}
    if recipe == "missing_quotes":
        categories = row.get("missing_categories")
        if categories is None:
            return "No RFQ yet"
        return ", ".join(str(c) for c in categories) or "Vendor quotes"
    if recipe == "labor_not_entered":
        return "Labor"
    missing = []
    if not checks.get("has_quotes"):
        missing.append("Vendor quotes")
    if not checks.get("has_labor"):
        missing.append("Labor")
    if not checks.get("has_markups"):
        missing.append("Markups")
    return ", ".join(missing) or "Nothing"


def _rows(payload: Mapping[str, Any] | None) -> list[dict]:
    rows = (payload or {}).get("rows") or []
    return [r for r in rows if isinstance(r, Mapping)]


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _evb_content(payload: Mapping[str, Any], rows: list[dict]) -> str:
    label = str((payload.get("window") or {}).get("label") or "that month")
    if not rows:
        return f"No bids were sent in {label}."
    verdicts = [str(_extra(r).get("verdict") or "") for r in rows]
    higher = verdicts.count("We bid higher")
    lower = verdicts.count("We bid lower")
    same = verdicts.count("Same price")
    compared = higher + lower + same
    if not compared:
        return f"{len(rows)} {_plural(len(rows), 'bid', 'bids')} sent; none could be compared yet."
    text = f"{compared} {_plural(compared, 'bid', 'bids')} compared; we bid higher on {higher}."
    if lower:
        text += f" We bid lower on {lower}."
    if same:
        text += f" Same price on {same}."
    left = len(rows) - compared
    if left:
        text += f" {left} could not be compared."
    return text


def _qr_content(payload: Mapping[str, Any], rows: list[dict]) -> str:
    phrase = str((payload.get("window") or {}).get("phrase") or "that week")
    if not rows:
        return f"No bids are due {phrase}."
    n = len(rows)
    ready = sum(1 for r in rows if _extra(r).get("complete"))
    return (f"{n} {_plural(n, 'bid', 'bids')} {phrase}; {ready} "
            f"{_plural(ready, 'has', 'have')} every key quote.")


def _content(recipe: str, payload: Mapping[str, Any], rows: list[dict]) -> str:
    n = len(rows)
    if recipe == "estimator_vs_bid":
        return _evb_content(payload, rows)
    if recipe == "quote_readiness":
        return _qr_content(payload, rows)
    if not n:
        if (payload.get("watch") or {}).get("project_id") and recipe in _WATCH_EMPTY_TEXT:
            return _WATCH_EMPTY_TEXT[recipe]
        return _EMPTY_TEXT[recipe]
    if recipe in ("due_today", "due_this_week"):
        checks = [r.get("checks") or {} for r in rows]
        quotes = sum(1 for c in checks if not c.get("has_quotes"))
        labor = sum(1 for c in checks if not c.get("has_labor"))
        markups = sum(1 for c in checks if not c.get("has_markups"))
        return (f"{RECIPES[recipe].label}: {n}. Missing vendor quotes: {quotes}. "
                f"Missing labor: {labor}. Missing markups: {markups}.")
    return f"{RECIPES[recipe].label}: {n}."


def message_for(recipe: str, payload: dict) -> tuple[str, list[dict]]:
    """(content, blocks). content is one fixed sentence of counts only, for
    example "Bids due today: 3. Missing vendor quotes: 1. Missing labor: 2.
    Missing markups: 0." or "No bids are due today." The phase 4 recipes read
    "14 bids compared; we bid higher on 14." and "6 bids next week; 4 have
    every key quote."; content never carries an amount or a name. Names go
    only into one table block (columns Project, Due (Pacific), Missing, or
    `table_columns` for the phase 4 recipes; at most MAX_TABLE_ROWS rows,
    then a text block "And N more.") and up to 20 project_link blocks."""
    get_recipe(recipe)
    payload = payload or {}
    rows = _rows(payload)
    content = _content(recipe, payload, rows)
    if not rows:
        return content, []
    shown = rows[:MAX_TABLE_ROWS]
    if recipe in PHASE4_RECIPES:
        columns, cells = table_for(recipe, shown)
        blocks: list[dict] = [{"type": "table", "columns": columns, "rows": cells}]
    else:
        blocks = [{
            "type": "table",
            "columns": list(TABLE_COLUMNS),
            "rows": [
                [project_label(r.get("project") or {}),
                 due_text((r.get("project") or {}).get("internal_bid_at")),
                 missing_text(recipe, r)]
                for r in shown
            ],
        }]
    if len(rows) > MAX_TABLE_ROWS:
        blocks.append({"type": "text", "text": f"And {len(rows) - MAX_TABLE_ROWS} more."})
    for r in rows[:MAX_PROJECT_LINKS]:
        project = r.get("project") or {}
        if project.get("id"):
            blocks.append({"type": "project_link", "project_id": str(project["id"]),
                           "label": project_label(project)})
    return content, blocks


def bell_text(title: str, payload: dict) -> str:
    """Fixed text, for example: Your routine is ready: Bids due today (3)."""
    return f"Your routine is ready: {title} ({len(_rows(payload))})."


# ── the scheduler's read spec ────────────────────────────────────────────────

RECIPE_ROWS_ROUTE = ("GET", "/assistant/recipes/{recipe}/rows")


def _recipe_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    recipe = get_recipe(str(args.get("recipe") or "")).key
    params: dict[str, Any] = {}
    for key in ("horizon_days", "project_id", "period", "week", "at"):
        if args.get(key) not in (None, ""):
            params[key] = args[key]
    return [ToolRequest(route=RECIPE_ROWS_ROUTE, path=f"/assistant/recipes/{recipe}/rows", params=params)]


def _recipe_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    # Never offered, so never trimmed for a model; the scheduler reads the
    # raw payload through Executor.read.
    return payloads[0] if payloads else None


# A validated, never registered read spec: the scheduler builds an Executor
# over it (docs/ASSISTANT_PHASE3_PLAN.md 3.4.4). It is not in the registry,
# so no turn can offer it or reach its route.
RECIPE_READ_SPEC = validate(ToolSpec(
    name="recipe_rows",
    namespace="schedule",
    sub_app="bidding",
    route=RECIPE_ROWS_ROUTE,
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "recipe": {"type": "string", "enum": sorted(RECIPES), "description": "The routine recipe."},
        "horizon_days": nullable({"type": "integer", "description": "Days ahead to look."}),
        "project_id": nullable({"type": "string", "description": "The watched project's full id."}),
        "period": nullable({"type": "string", "description": "estimator_vs_bid month: YYYY-MM, last_month or this_month."}),
        "week": nullable({"type": "string", "description": "quote_readiness week: this_week, next_week or a Monday date."}),
        "at": nullable({"type": "string", "description": "The occurrence instant, ISO UTC."}),
    }),
    response_trimmer=_recipe_trim,
    progress_label="Reading the routine's bids",
    roles=INTERNAL_ROLES,
    description="The rows of one routine recipe at one instant. Used by the scheduler only.",
    requests=_recipe_requests,
    taint_fields=("name",),
    offered=False,
))

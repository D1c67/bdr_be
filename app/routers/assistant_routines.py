"""Assistant routines: recipes, schedules, runs and the holiday list
(docs/ASSISTANT_PHASE3_PLAN.md 3.2.3, 3.3, 3.6 and 5.3).

Who may call what:

  both       the reads (list, one routine, its runs, the draft, the two
             previews, the recipe rows): the person in the Routines tab and
             the assistant through the shim;
  assistant  POST /assistant/schedules, approval-bound only (the execute
             job of an approved schedule_create card: its idempotency header
             must equal the token's approval id, anything else is a bare
             404), the by-approval recover read, and the housekeeping tool
             routes under /assistant/tools/schedules, which trust only the
             turn context the begun ledger row gives (3.7.3);
  human      pause, resume, end, Run now, seen and the holiday list: each
             calls _refuse_assistant_principal and the firewall deny-lists
             it (app/assistant/firewall.py).

The model never writes a rule or a title: the cadence is structured
(rrules.parse_cadence), the rule is rrules.to_rrule and the title is
recipes.title_for (deviation D3). Every transition is one update fenced on
the status it was read with; a lost fence answers 409
schedule_state_changed with the current row. In a tainted turn the tool
end pauses instead (ending is irreversible), and resume and Run now are
refused (deviation D6).

Declaration order matters: Starlette and firewall.resolve_path_format take
the first full match, so every literal /assistant/schedules/... route sits
above the /assistant/schedules/{schedule_id} routes.

Plain def handlers: the Supabase SDK is synchronous. Every assistant_* row
goes through app/assistant/repo.py; the recipe data comes from
app/services/assistant_recipes.py, imported on first use.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.assistant import recipes, repo, rrules
from app.assistant.schemas import (
    ACTIVE_SCHEDULE_CAP,
    PREVIEW_NOTE,
    CadenceIn,
    HolidaysIn,
    HolidaysOut,
    RecipeRowOut,
    RecipeRowsOut,
    ScheduleCreateIn,
    ScheduleDraftOut,
    ScheduleLastRunOut,
    ScheduleListOut,
    ScheduleOut,
    SchedulePreviewOut,
    ScheduleProjectOut,
    ScheduleRunOut,
    ScheduleRunsOut,
    SchedulesSeenIn,
    SchedulesSeenOut,
    ScheduleWarningOut,
)
from app.assistant.tools._common import is_uuid, strip_bid_amounts
from app.core import supabase_client
from app.core.config import get_settings
from app.core.deps import CurrentUser, require_internal, require_role
from app.core.error_codes import RateLimitScope
from app.core.features import SubApp, enabled_map, require_feature
from app.core.ratelimit import rate_limit
from app.core.roles import ACTUAL_BID_VIEWER_ROLES, Role
from app.routers.assistant import (
    _before_cursor,
    _detail_error,
    _parse_ts,
    _project_refs,
    _refuse_assistant_principal,
    _require_assistant,
    _require_assistant_principal,
    _trusted_turn,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assistant", tags=["assistant"], dependencies=[Depends(require_internal)])

# Route literals the phase 3 tool specs repeat (app/assistant/tools/schedules.py,
# pinned by a test).
RECIPE_ROWS_ROUTE = "/assistant/recipes/{recipe}/rows"
SCHEDULES_ROUTE = "/assistant/schedules"
SCHEDULE_DRAFT_ROUTE = "/assistant/schedules/draft"
SCHEDULE_PREVIEW_ROUTE = "/assistant/schedules/preview"
SCHEDULE_BY_APPROVAL_ROUTE = "/assistant/schedules/by-approval/{approval_id}"
SCHEDULE_ROUTE = "/assistant/schedules/{schedule_id}"
SCHEDULE_ID_PREVIEW_ROUTE = "/assistant/schedules/{schedule_id}/preview"
TOOL_PAUSE_ROUTE = "/assistant/tools/schedules/{schedule_id}/pause"
TOOL_RESUME_ROUTE = "/assistant/tools/schedules/{schedule_id}/resume"
TOOL_END_ROUTE = "/assistant/tools/schedules/{schedule_id}/end"
TOOL_RUN_NOW_ROUTE = "/assistant/tools/schedules/{schedule_id}/run-now"

# Error codes (5.3 and 5.4).
SCHEDULE_NOT_FOUND = "schedule_not_found"
SCHEDULE_CAP_REACHED = "schedule_cap_reached"
SCHEDULE_STATE_CHANGED = "schedule_state_changed"
SCHEDULE_NOT_ACTIVE = "schedule_not_active"
SCHEDULE_BUSY = "schedule_busy"
BAD_CADENCE = "bad_cadence"
BAD_PARAMS = "bad_params"
RECIPE_NOT_FOUND = "recipe_not_found"
RECIPE_UNAVAILABLE = "recipe_unavailable"
WATCH_PROJECT_CLOSED = "watch_project_closed"
SCHEDULER_OFF = "scheduler_off"
RUN_NOW_TOO_SOON = "run_now_too_soon"
TAINTED_TURN = "tainted_turn"
HOLIDAYS_INVALID = "holidays_invalid"

DELIVERIES = ("chat", "chat_bell", "chat_bell_email")
BELL_DELIVERIES = ("chat_bell", "chat_bell_email")
PAUSED_STATUSES = ("paused", "paused_feature_off")
STATUS_FILTERS = {
    "active": ("active",),
    "paused": PAUSED_STATUSES,
    "ended": ("ended",),
    "all": None,
}
RUNS_PAGE_MAX = 50
# The tool end in a tainted turn (deviation D6).
PAUSED_INSTEAD_NOTE = "Paused. End it from Routines to stop it for good."
# The recipe route's `at` is clamped to this far from now (3.2.3).
AT_CLAMP = timedelta(hours=24)
HOLIDAYS_MAX = 60
HOLIDAYS_SPAN_DAYS = 2 * 366
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DIGEST_OVERLAP_TEXT = (
    "The 6:03 AM daily digest email also lists these bids. On days it reaches you, this routine stays quiet."
)
REMINDERS_OVERLAP_TEXT = "BDR's own due-date reminders will also arrive."
CAP_NEAR_TEXT = f"This is your last free routine: at most {ACTIVE_SCHEDULE_CAP} can be active."

_MESSAGES = {
    SCHEDULE_NOT_FOUND: "That routine was not found.",
    SCHEDULE_CAP_REACHED: f"You already have {ACTIVE_SCHEDULE_CAP} active routines. End or pause one first.",
    SCHEDULE_STATE_CHANGED: "The routine changed since it was shown. Look at it again.",
    SCHEDULE_NOT_ACTIVE: "Only an active routine, or one you paused, can run now.",
    SCHEDULE_BUSY: "This routine is running right now. Try again when it finishes.",
    RECIPE_UNAVAILABLE: "This routine's reports are not available to you right now.",
    WATCH_PROJECT_CLOSED: "That bid was submitted, closed or removed, so there is nothing to watch.",
    SCHEDULER_OFF: "Routines are not running on this server right now, so Run now would never start.",
    RUN_NOW_TOO_SOON: "This routine ran a moment ago. Try again in a few minutes.",
    TAINTED_TURN: "This conversation read outside content, so this change is done from the Routines tab.",
}

# The preview reads the same bids as a run: bound it with the catch-all
# per-principal budget (the assistant has its own, smaller bucket).
preview_rate_limit = rate_limit(RateLimitScope.DEFAULT, lambda: get_settings().default_rate_limit_per_min)


def _now() -> datetime:
    """The route clock (one seam for the tests)."""
    return datetime.now(timezone.utc)


def _data():
    """app/services/assistant_recipes (the recipe data), on first use."""
    from app.services import assistant_recipes

    return assistant_recipes


def _projects():
    """app/routers/projects: the approval-bound key check and the role
    redaction, on first use."""
    from app.routers import projects

    return projects


def _error(status_code: int, code: str, message: str | None = None, **detail: Any) -> HTTPException:
    return _detail_error(status_code, code, message or _MESSAGES.get(code, "That request was refused."), **detail)


def _schedule_id(value: str) -> str:
    value = (value or "").strip()
    if not is_uuid(value):
        raise _error(status.HTTP_404_NOT_FOUND, SCHEDULE_NOT_FOUND)
    return value


def _holidays(user: CurrentUser) -> list[Any]:
    return list((repo.settings_row(user.id) or {}).get("holidays") or [])


def _sub_app_on(user: CurrentUser, sub_app: str | None) -> bool:
    return not sub_app or bool(enabled_map(user.role).get(sub_app, False))


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _floor_minute(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def _needs_anchor(cadence: rrules.Cadence) -> bool:
    """Every-n-weeks rules take their week parity from dtstart."""
    return cadence.frequency == "weekly" and int(cadence.interval or 1) > 1


def _week_anchor(value: datetime) -> datetime:
    """Monday 00:00 Pacific of the week `value` falls in, as UTC: the same
    for every draft read that week, so the card's target stays stable."""
    local = value.astimezone(rrules.PT)
    monday = (local - timedelta(days=local.weekday())).date()
    return datetime(monday.year, monday.month, monday.day, tzinfo=rrules.PT).astimezone(timezone.utc)


def _create_dtstart(plan: "_Plan", anchor: datetime | None, now: datetime) -> datetime:
    """The stored dtstart: the draft's week anchor for an every-n-weeks rule
    (so the routine runs on the weeks the card showed), else the creation
    minute. An anchor in the future, or older than last week's, is refused
    (a valid card is at most an hour old)."""
    if not _needs_anchor(plan.cadence):
        return _floor_minute(now)
    if anchor is None:
        return _week_anchor(now)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    # Last week's Monday, computed in Pacific (a DST change makes it 7 days
    # plus or minus an hour back in UTC).
    if anchor > now or anchor < _week_anchor(now - timedelta(days=7)):
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_CADENCE,
                     "This routine's preview is out of date. Ask for it again.")
    return anchor


# ── Shapes ───────────────────────────────────────────────────────────────────


def _cadence_of(rule: str) -> rrules.Cadence | None:
    try:
        return rrules.from_rrule(rule)
    except rrules.CadenceError:
        return None


def _next_runs(row: dict, holidays: list[Any], now: datetime) -> list[str]:
    """Three UTC instants for an active row, so the frontend never expands
    a rule; [] for any other row or a rule that no longer expands."""
    if row.get("status") != "active":
        return []
    dtstart = _parse_ts(row.get("dtstart")) or _parse_ts(row.get("created_at")) or now
    try:
        return [_iso(t) for t in rrules.next_runs(str(row.get("rrule") or ""), dtstart, now, holidays, n=3)]
    except (rrules.CadenceError, ValueError):
        return []


def _last_run_out(run: dict | None) -> ScheduleLastRunOut | None:
    if not run or not run.get("id"):
        return None
    return ScheduleLastRunOut(id=str(run["id"]), status=str(run.get("status") or ""),
                              scheduled_for=str(run.get("scheduled_for") or ""), finished_at=run.get("finished_at"))


def _schedule_out(row: dict, *, holidays: list[Any], now: datetime, last: dict | None = None,
                  project: dict | None = None, note: str | None = None) -> ScheduleOut:
    rule = str(row.get("rrule") or "")
    cadence = _cadence_of(rule)
    params = row.get("params") if isinstance(row.get("params"), dict) else {}
    project_id = row.get("project_id") or params.get("project_id")
    return ScheduleOut(
        id=str(row["id"]),
        title=str(row.get("title") or ""),
        recipe=str(row.get("recipe") or ""),
        status=str(row.get("status") or ""),
        paused_reason=row.get("paused_reason"),
        ended_reason=row.get("ended_reason"),
        cadence=CadenceIn(**cadence.as_dict()) if cadence else None,
        cadence_text=rrules.cadence_text(cadence) if cadence else None,
        rrule=rule,
        delivery=str(row.get("delivery") or "chat"),
        only_if_changed=bool(row.get("only_if_changed")),
        horizon_days=params.get("horizon_days"),
        project=ScheduleProjectOut(**(project or {"id": str(project_id)})) if project_id else None,
        conversation_id=row.get("conversation_id"),
        next_runs=_next_runs(row, holidays, now),
        last_run=_last_run_out(last),
        unread_deliveries=int(row.get("unread_deliveries") or 0),
        consecutive_failures=int(row.get("consecutive_failures") or 0),
        created_at=str(row.get("created_at") or ""),
        ended_at=row.get("ended_at"),
        note=note,
    )


def _watched(row: dict) -> str | None:
    params = row.get("params") if isinstance(row.get("params"), dict) else {}
    value = row.get("project_id") or params.get("project_id")
    return str(value) if value else None


def _outs(user: CurrentUser, rows: list[dict]) -> list[ScheduleOut]:
    """ScheduleOut for each row: the last runs and the watched projects in
    one read each."""
    if not rows:
        return []
    now, holidays = _now(), _holidays(user)
    last = repo.last_runs(user.id, [str(r["id"]) for r in rows])
    projects = _project_refs(user, [_watched(r) for r in rows])
    return [
        _schedule_out(r, holidays=holidays, now=now, last=last.get(str(r["id"])),
                      project=projects.get(_watched(r) or ""))
        for r in rows
    ]


def _out(user: CurrentUser, row: dict, note: str | None = None) -> ScheduleOut:
    out = _outs(user, [row])[0]
    return out.model_copy(update={"note": note}) if note else out


def _run_out(run: dict) -> ScheduleRunOut:
    return ScheduleRunOut(
        id=str(run["id"]),
        schedule_id=str(run.get("schedule_id") or ""),
        kind=str(run.get("kind") or "scheduled"),
        scheduled_for=str(run.get("scheduled_for") or ""),
        status=str(run.get("status") or ""),
        started_at=run.get("started_at"),
        finished_at=run.get("finished_at"),
        delivered=run.get("delivered") if isinstance(run.get("delivered"), dict) else None,
        message_id=run.get("message_id"),
        error_kind=run.get("error_kind"),
        error=run.get("error"),
        stats=run.get("stats") if isinstance(run.get("stats"), dict) else None,
    )


def _load(user: CurrentUser, schedule_id: str) -> dict:
    row = repo.get_schedule(user.id, _schedule_id(schedule_id))
    if row is None:
        raise _error(status.HTTP_404_NOT_FOUND, SCHEDULE_NOT_FOUND)
    return row


def _state_changed(user: CurrentUser, schedule_id: str) -> HTTPException:
    """409 schedule_state_changed carrying the row as it is now."""
    row = repo.get_schedule(user.id, schedule_id)
    if row is None:
        return _error(status.HTTP_404_NOT_FOUND, SCHEDULE_NOT_FOUND)
    return _error(status.HTTP_409_CONFLICT, SCHEDULE_STATE_CHANGED, schedule=_out(user, row).model_dump())


# ── The plan of a new routine (draft and create share it) ───────────────────


class _Refused(Exception):
    def __init__(self, status_code: int, code: str, message: str | None = None):
        super().__init__(code)
        self.status_code, self.code = status_code, code
        self.message = message or _MESSAGES.get(code, "That request was refused.")


@dataclass(frozen=True)
class _Plan:
    recipe: recipes.Recipe
    cadence: rrules.Cadence
    rule: str
    params: dict
    delivery: str
    only_if_changed: bool
    project: dict | None
    title: str

    def body(self, anchor: datetime | None = None) -> ScheduleCreateIn:
        return ScheduleCreateIn(
            recipe=self.recipe.key, cadence=CadenceIn(**self.cadence.as_dict()), delivery=self.delivery,
            only_if_changed=self.only_if_changed, horizon_days=self.params.get("horizon_days"),
            project_id=self.params.get("project_id"), anchor=anchor,
            period=self.params.get("period"), week=self.params.get("week"),
        )


def _plan(user: CurrentUser, recipe_key: Any, cadence: Any, delivery: Any, only_if_changed: bool,
          horizon_days: Any, project_id: Any, *, period: Any = None, week: Any = None) -> _Plan:
    """Judge a new routine: the recipe, the cadence, the delivery, the
    params, the recipe's roles and the sub-app for the caller's role and,
    for a watch, the project. Raises _Refused with the route's status and
    code."""
    try:
        recipe = recipes.get_recipe(recipe_key)
    except recipes.RecipeError as exc:
        raise _Refused(status.HTTP_404_NOT_FOUND, exc.code, str(exc)) from None
    if not _role_allows(user, recipe):
        raise _Refused(status.HTTP_409_CONFLICT, RECIPE_UNAVAILABLE)
    try:
        parsed = rrules.parse_cadence(cadence)
        rule = rrules.to_rrule(parsed)
    except rrules.CadenceError as exc:
        raise _Refused(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_CADENCE, str(exc)) from None
    if delivery not in DELIVERIES:
        raise _Refused(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_PARAMS,
                       "Delivery must be chat, chat_bell or chat_bell_email.")
    try:
        params = recipes.validate_params(recipe.key, horizon_days, project_id, period=period, week=week)
    except recipes.RecipeError as exc:
        raise _Refused(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code, str(exc)) from None
    if not _sub_app_on(user, recipe.sub_app):
        raise _Refused(status.HTTP_409_CONFLICT, RECIPE_UNAVAILABLE)
    project = None
    if "project_id" in params:
        pid = params["project_id"]
        if _data().watch_state(supabase_client.get_supabase(), pid) != "open":
            raise _Refused(status.HTTP_409_CONFLICT, WATCH_PROJECT_CLOSED)
        project = _project_refs(user, [pid]).get(pid) or {"id": pid, "number": None, "name": None}
    title = recipes.title_for(recipe.key, parsed, (project or {}).get("number"))
    return _Plan(recipe, parsed, rule, params, delivery, bool(only_if_changed), project, title)


def _warnings(user: CurrentUser, recipe: recipes.Recipe, active_count: int) -> list[ScheduleWarningOut]:
    """The overlap notices of 3.4.7, plus cap_near at one below the cap.
    An Accountant gets no digest, so no digest notice."""
    out = []
    if recipe.digest_overlap and user.role != Role.ACCOUNTANT:
        out.append(ScheduleWarningOut(code="digest_overlap", text=DIGEST_OVERLAP_TEXT))
    if recipe.reminders_overlap:
        out.append(ScheduleWarningOut(code="reminders_overlap", text=REMINDERS_OVERLAP_TEXT))
    if active_count == ACTIVE_SCHEDULE_CAP - 1:
        out.append(ScheduleWarningOut(code="cap_near", text=CAP_NEAR_TEXT))
    return out


# ── Recipe rows (3.2.3) ──────────────────────────────────────────────────────


def _int_param(value: str | None, label: str) -> int | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    if not text.isdigit():
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_PARAMS, f"{label} must be a whole number.")
    return int(text)


def _clamped_at(value: str | None, now: datetime) -> datetime:
    if value is None or not value.strip():
        return now
    at = _parse_ts(value.strip())
    if at is None:
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_PARAMS, "at must be an ISO 8601 time.")
    return min(max(at, now - AT_CLAMP), now + AT_CLAMP)


def _redacted_extra(user: CurrentUser, extra: Any) -> dict | None:
    """A row's `extra` for the caller's role (docs/ASSISTANT_PHASE4_PLAN.md
    3.10.3): a role outside ACTUAL_BID_VIEWER_ROLES loses every bid amount
    key at any depth (strip_bid_amounts, actual_bid_at included) and the
    `bid_date` the data side derives from actual_bid_at. Defense in depth:
    the recipe role gate already keeps estimator_vs_bid from them."""
    if not isinstance(extra, dict):
        return None
    if user.role in ACTUAL_BID_VIEWER_ROLES:
        return extra
    out = strip_bid_amounts(extra, user.role)
    out.pop("bid_date", None)
    return out


def _redacted(user: CurrentUser, payload: dict) -> dict:
    """The rows, and estimator_vs_bid's `extra.sent_early` rows (served as
    `sent_early`), each project redacted per role and each row's `extra`
    without bid amounts for non-viewers."""
    redact = _projects().redact_for_role

    def rows_of(items: Any) -> list[dict]:
        out = []
        for r in items or []:
            if not isinstance(r, dict):
                continue
            row = {**r, "project": redact(dict(r.get("project") or {}), user.role)}
            if "extra" in row:
                row["extra"] = _redacted_extra(user, row.get("extra"))
            out.append(row)
        return out

    out = {**payload, "rows": rows_of(payload.get("rows"))}
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    if isinstance(extra.get("sent_early"), list):
        out["sent_early"] = rows_of(extra["sent_early"])
    return out


def _role_allows(user: CurrentUser, recipe: recipes.Recipe) -> bool:
    """The recipe's own role gate (3.10.1): estimator_vs_bid is for
    ACTUAL_BID_VIEWER_ROLES only; the other recipes take every internal
    role."""
    return user.role in recipe.roles


def _rows_kwargs(recipe: str, *, period: Any, week: Any, role: Any) -> dict:
    """The phase 4 keywords of assistant_recipes.rows_for: `period`, `week`
    and the caller's `role` go to the two coded recipes only (the phase 3
    recipes take none of them, so their calls stay as they were)."""
    if recipe not in recipes.PHASE4_RECIPES:
        return {}
    return {"period": period, "week": week, "role": role}


def _recipe_error_status(code: str) -> int:
    if code == RECIPE_NOT_FOUND:
        return status.HTTP_404_NOT_FOUND
    if code == RECIPE_UNAVAILABLE:
        return status.HTTP_409_CONFLICT
    return status.HTTP_422_UNPROCESSABLE_CONTENT


def _digest_sent_today(user: CurrentUser, recipe: str, at: datetime) -> bool:
    """The precedence stub (3.4.7): the caller's own 6:03 AM digest row for
    the Pacific day of `at`. Accountants never get the digest."""
    if recipe != "due_today" or user.role == Role.ACCOUNTANT:
        return False
    try:
        return _data().digest_sent(supabase_client.get_supabase(), user.id, at.astimezone(rrules.PT).date())
    except Exception:  # noqa: BLE001 - an unreadable log never suppresses a delivery
        logger.exception("assistant: digest log could not be read for %s", user.id)
        return False


@router.get(RECIPE_ROWS_ROUTE.removeprefix("/assistant"), response_model=RecipeRowsOut,
            dependencies=[Depends(require_feature(SubApp.BIDDING))])
def recipe_rows(
    recipe: str,
    horizon_days: str | None = Query(default=None, max_length=8),
    project_id: str | None = Query(default=None, max_length=64),
    at: str | None = Query(default=None, max_length=64),
    period: str | None = Query(default=None, max_length=10),
    week: str | None = Query(default=None, max_length=10),
    user: CurrentUser = Depends(require_internal),
) -> RecipeRowsOut:
    """One recipe's bids at one instant, each project redacted per role.
    The scheduler reads it through the shim as the owner's principal
    (allowlisted only by recipes.RECIPE_READ_SPEC, never by a turn); the
    coded_report tool reads it in a turn; the previews call the service
    directly. Phase 4 (3.10.3): `period` (estimator_vs_bid) and `week`
    (quote_readiness), the recipe's role gate (409 recipe_unavailable) and
    each row's `extra`, without bid amounts for non-viewers. estimator_vs_bid
    also serves the month's `totals` and the `sent_early` rows (from the
    payload's `extra.sent_early`), redacted the same way."""
    _require_assistant(user)
    now = _now()
    at_utc = _clamped_at(at, now)
    horizon = _int_param(horizon_days, "horizon_days")
    try:
        spec = recipes.get_recipe(recipe)
        if not _role_allows(user, spec):
            raise recipes.RecipeError(_MESSAGES[RECIPE_UNAVAILABLE], code=RECIPE_UNAVAILABLE)
        clean_period, clean_week = (period or "").strip() or None, (week or "").strip() or None
        extra = _rows_kwargs(spec.key, period=clean_period, week=clean_week, role=user.role)
        if not extra and (clean_period or clean_week):
            # The phase 3 recipes still refuse a month or a week they do not take.
            recipes.validate_params(spec.key, horizon, (project_id or "").strip() or None,
                                    period=clean_period, week=clean_week)
        payload = _data().rows_for(supabase_client.get_supabase(), recipe, at_utc=at_utc,
                                   horizon_days=horizon, project_id=(project_id or "").strip() or None, **extra)
    except recipes.RecipeError as exc:
        raise _error(_recipe_error_status(exc.code), exc.code, str(exc)) from None
    payload = _redacted(user, payload)

    def _out(r: dict) -> RecipeRowOut:
        return RecipeRowOut(project=r.get("project") or {}, checks=r.get("checks") or {},
                            severity=r.get("severity"), missing_categories=r.get("missing_categories"),
                            extra=r.get("extra") if isinstance(r.get("extra"), dict) else None)

    totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else None
    if totals is not None and user.role not in ACTUAL_BID_VIEWER_ROLES:
        totals = strip_bid_amounts(totals, user.role)
    sent_early = payload.get("sent_early")
    return RecipeRowsOut(
        recipe=recipe,
        window=payload.get("window") or {},
        rows=[_out(r) for r in payload["rows"]],
        watch=payload.get("watch"),
        digest_sent_today=_digest_sent_today(user, recipe, at_utc),
        totals=totals,
        sent_early=[_out(r) for r in sent_early] if isinstance(sent_early, list) else None,
    )


# ── Schedules: literal routes first (declaration order, see above) ──────────


@router.get("/schedules", response_model=ScheduleListOut)
def list_schedules(
    status_filter: str = Query(default="all", alias="status", pattern="^(active|paused|ended|all)$"),
    user: CurrentUser = Depends(require_internal),
) -> ScheduleListOut:
    """The caller's routines, newest first, at most 50."""
    _require_assistant(user)
    rows = repo.list_schedules(user.id, status=STATUS_FILTERS[status_filter], limit=50)
    return ScheduleListOut(items=_outs(user, rows))


def _parse_bool(value: str | None) -> bool | None:
    if value is None or not value.strip():
        return False
    text = value.strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    return None


# Declared above /schedules/{schedule_id}: "draft" is never read as an id.
@router.get("/schedules/draft", response_model=ScheduleDraftOut)
def schedule_draft(
    recipe: str = Query(default="", max_length=40),
    frequency: str | None = Query(default=None, max_length=20),
    hour: str | None = Query(default=None, max_length=4),
    minute: str | None = Query(default=None, max_length=4),
    days: str | None = Query(default=None, max_length=40),
    day_of_month: str | None = Query(default=None, max_length=4),
    interval: str | None = Query(default=None, max_length=4),
    window_start_hour: str | None = Query(default=None, max_length=4),
    window_end_hour: str | None = Query(default=None, max_length=4),
    delivery: str = Query(default="chat_bell", max_length=20),
    only_if_changed: str | None = Query(default=None, max_length=8),
    horizon_days: str | None = Query(default=None, max_length=8),
    project_id: str | None = Query(default=None, max_length=64),
    period: str | None = Query(default=None, max_length=10),
    week: str | None = Query(default=None, max_length=10),
    user: CurrentUser = Depends(require_internal),
) -> ScheduleDraftOut:
    """The schedule_create card's preview read (3.3.3). Never writes; every
    refusal comes back as ok false with its code and one sentence."""
    _require_assistant(user)
    cadence = {k: v for k, v in (
        ("frequency", frequency), ("hour", hour), ("minute", minute), ("days", days),
        ("day_of_month", day_of_month), ("interval", interval),
        ("window_start_hour", window_start_hour), ("window_end_hour", window_end_hour),
    ) if v is not None}
    flag = _parse_bool(only_if_changed)
    try:
        if flag is None:
            raise _Refused(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_PARAMS, "only_if_changed must be true or false.")
        horizon = None
        if horizon_days is not None and horizon_days.strip():
            if not horizon_days.strip().isdigit():
                raise _Refused(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_PARAMS,
                               "The number of days must be a whole number.")
            horizon = int(horizon_days.strip())
        plan = _plan(user, recipe, cadence, delivery, flag, horizon, (project_id or "").strip() or None,
                     period=(period or "").strip() or None, week=(week or "").strip() or None)
        now = _now()
        anchor = _week_anchor(now) if _needs_anchor(plan.cadence) else None
        runs = rrules.next_runs(plan.rule, anchor or _floor_minute(now), now, _holidays(user), n=3)
    except _Refused as exc:
        return ScheduleDraftOut(ok=False, code=exc.code, message=exc.message)
    except rrules.CadenceError as exc:
        return ScheduleDraftOut(ok=False, code=BAD_CADENCE, message=str(exc))
    active = repo.count_active_schedules(user.id)
    return ScheduleDraftOut(
        ok=True,
        body=plan.body(anchor),
        title=plan.title,
        rrule=plan.rule,
        cadence_text=rrules.cadence_text(plan.cadence),
        next_runs=[_iso(t) for t in runs],
        warnings=_warnings(user, plan.recipe, active),
        active_count=active,
        active_cap=ACTIVE_SCHEDULE_CAP,
        project=ScheduleProjectOut(**plan.project) if plan.project else None,
    )


def _preview(user: CurrentUser, recipe_key: str, params: dict, delivery: str,
             schedule_id: str | None) -> SchedulePreviewOut:
    """A dry run (3.3.7): the recipe read in-process with the caller's
    role, the message a run would post, the channels it would use. Writes
    no run, no message and no bell."""
    try:
        recipe = recipes.get_recipe(recipe_key)
    except recipes.RecipeError as exc:
        raise _error(status.HTTP_404_NOT_FOUND, exc.code, str(exc)) from None
    if not _role_allows(user, recipe) or not _sub_app_on(user, recipe.sub_app):
        raise _error(status.HTTP_409_CONFLICT, RECIPE_UNAVAILABLE)
    now = _now()
    try:
        payload = _data().rows_for(supabase_client.get_supabase(), recipe.key, at_utc=now,
                                   horizon_days=params.get("horizon_days"), project_id=params.get("project_id"),
                                   **_rows_kwargs(recipe.key, period=params.get("period"), week=params.get("week"),
                                                  role=user.role))
    except recipes.RecipeError as exc:
        raise _error(_recipe_error_status(exc.code), exc.code, str(exc)) from None
    payload = _redacted(user, payload)
    content, blocks = recipes.message_for(recipe.key, payload)
    n = len(payload["rows"])
    suppressed = "digest_sent" if _digest_sent_today(user, recipe.key, now) else None
    would = {
        "chat": True,
        "bell": delivery in BELL_DELIVERIES and n > 0,
        "email": delivery == "chat_bell_email" and n > 0 and bool(get_settings().ms_client_id),
    }
    if suppressed:
        would = {k: False for k in would}
    return SchedulePreviewOut(
        schedule_id=schedule_id, recipe=recipe.key, window=payload.get("window") or {}, content=content,
        blocks=blocks, rows=n, would_deliver=would, suppressed=suppressed, note=PREVIEW_NOTE,
    )


@router.get("/schedules/preview", response_model=SchedulePreviewOut,
            dependencies=[Depends(preview_rate_limit)])
def preview_recipe(
    recipe: str = Query(default="", max_length=40),
    horizon_days: str | None = Query(default=None, max_length=8),
    project_id: str | None = Query(default=None, max_length=64),
    delivery: str = Query(default="chat_bell", max_length=20),
    period: str | None = Query(default=None, max_length=10),
    week: str | None = Query(default=None, max_length=10),
    user: CurrentUser = Depends(require_internal),
) -> SchedulePreviewOut:
    """A dry run of a recipe that has no routine yet."""
    _require_assistant(user)
    horizon = _int_param(horizon_days, "horizon_days")
    try:
        params = recipes.validate_params(recipe, horizon, (project_id or "").strip() or None,
                                         period=(period or "").strip() or None, week=(week or "").strip() or None)
    except recipes.RecipeError as exc:
        code_status = status.HTTP_404_NOT_FOUND if exc.code == RECIPE_NOT_FOUND else status.HTTP_422_UNPROCESSABLE_CONTENT
        raise _error(code_status, exc.code, str(exc)) from None
    if delivery not in DELIVERIES:
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_PARAMS,
                     "Delivery must be chat, chat_bell or chat_bell_email.")
    return _preview(user, recipe, params, delivery, None)


@router.get("/schedules/by-approval/{approval_id}", response_model=ScheduleOut)
def schedule_by_approval(approval_id: str, user: CurrentUser = Depends(require_internal)) -> ScheduleOut:
    """The schedule an approved card created: schedule_create's recover
    read after a create answered 5xx. The assistant's own; 404 for a human,
    for a key that is not a uuid and for another user's card."""
    _require_assistant_principal(user)
    _require_assistant(user)
    key = (approval_id or "").strip()
    row = repo.schedule_by_approval(user.id, key) if is_uuid(key) else None
    if row is None:
        raise _error(status.HTTP_404_NOT_FOUND, SCHEDULE_NOT_FOUND)
    return _out(user, row)


@router.post("/schedules/seen", response_model=SchedulesSeenOut)
def schedules_seen(body: SchedulesSeenIn, user: CurrentUser = Depends(require_internal)) -> SchedulesSeenOut:
    """unread_deliveries = 0 and last_seen_at = now() on the caller's rows
    among these ids (the Routines tab, the other seen signal)."""
    _refuse_assistant_principal(user)
    _require_assistant(user)
    return SchedulesSeenOut(updated=repo.mark_schedules_seen(user.id, body.ids))


@router.post("/schedules", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED)
def create_schedule(
    body: ScheduleCreateIn, request: Request, response: Response, user: CurrentUser = Depends(require_internal),
) -> ScheduleOut:
    """The schedule_create card's write (3.3.2): approval-bound only (the
    idempotency header must equal the token's approval id; a human, a turn
    principal and a differing header get the bare 404). A replay of the
    same card answers 200 with the row it made. Both occurrence slots are
    filled here; the first delivery creates the conversation."""
    _require_assistant(user)
    key = _projects()._approval_bound_key(request, user)
    existing = repo.schedule_by_approval(user.id, key)
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return _out(user, existing)
    try:
        plan = _plan(user, body.recipe, body.cadence.model_dump(), body.delivery, body.only_if_changed,
                     body.horizon_days, body.project_id, period=body.period, week=body.week)
    except _Refused as exc:
        raise _error(exc.status_code, exc.code, exc.message) from None
    now = _now()
    dtstart = _create_dtstart(plan, body.anchor, now)
    slots = rrules.occurrences(plan.rule, dtstart, now, 2, _holidays(user))
    if not slots:
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_CADENCE, rrules.NEVER_RUNS_TEXT)
    row = {
        "title": plan.title,
        "recipe": plan.recipe.key,
        "params": plan.params,
        "rrule": plan.rule,
        "timezone": "America/Los_Angeles",
        "status": "active",
        "sub_app": plan.recipe.sub_app,
        "delivery": plan.delivery,
        "only_if_changed": plan.only_if_changed,
        "project_id": plan.params.get("project_id"),
        "approval_id": key,
        "dtstart": _iso(dtstart),
        "next_run_at": _iso(slots[0]),
        "after_next_run_at": _iso(slots[1]) if len(slots) > 1 else None,
    }
    try:
        created = repo.insert_schedule(user.id, row)
    except repo.ScheduleExists:
        # The approval index: another attempt of the same card won the race.
        existing = repo.schedule_by_approval(user.id, key)
        if existing is None:
            raise
        response.status_code = status.HTTP_200_OK
        return _out(user, existing)
    except repo.ScheduleRefused as exc:
        if exc.code == "schedule_cap_active":
            raise _error(status.HTTP_409_CONFLICT, SCHEDULE_CAP_REACHED) from None
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_CADENCE,
                     "This schedule is not one BDR can run.") from None
    return _out(user, created)


# ── Schedules: the {schedule_id} routes ─────────────────────────────────────


@router.get("/schedules/{schedule_id}", response_model=ScheduleOut)
def read_schedule(schedule_id: str, user: CurrentUser = Depends(require_internal)) -> ScheduleOut:
    _require_assistant(user)
    return _out(user, _load(user, schedule_id))


@router.get("/schedules/{schedule_id}/runs", response_model=ScheduleRunsOut)
def list_schedule_runs(
    schedule_id: str,
    before: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=20, ge=1, le=RUNS_PAGE_MAX),
    user: CurrentUser = Depends(require_internal),
) -> ScheduleRunsOut:
    """The routine's runs, newest occurrence first; `next_before` is the
    cursor for the next page (the Activity list's shape)."""
    _require_assistant(user)
    row = _load(user, schedule_id)
    runs = repo.list_runs(user.id, str(row["id"]), before=_before_cursor(before), limit=limit)
    next_before = str(runs[-1].get("scheduled_for")) if runs and len(runs) >= limit else None
    return ScheduleRunsOut(items=[_run_out(r) for r in runs], next_before=next_before)


@router.get("/schedules/{schedule_id}/preview", response_model=SchedulePreviewOut,
            dependencies=[Depends(preview_rate_limit)])
def preview_schedule(schedule_id: str, user: CurrentUser = Depends(require_internal)) -> SchedulePreviewOut:
    """A dry run of an existing routine: nothing is sent or saved."""
    _require_assistant(user)
    row = _load(user, schedule_id)
    params = row.get("params") if isinstance(row.get("params"), dict) else {}
    return _preview(user, str(row.get("recipe") or ""), params, str(row.get("delivery") or "chat"), str(row["id"]))


# ── Transitions (the human routes and the tool routes share them) ───────────


def _pause(user: CurrentUser, row: dict) -> dict:
    """active or paused_feature_off to paused (reason user). A paused row
    answers as it is; an ended one is a changed state."""
    current = row.get("status")
    if current == "paused":
        return row
    if current not in ("active", "paused_feature_off"):
        raise _state_changed(user, str(row["id"]))
    updated = repo.update_schedule(
        user.id, str(row["id"]), {"status": "paused", "paused_reason": "user", "paused_at": _iso(_now())},
        expect_status=current,
    )
    if updated is None:
        raise _state_changed(user, str(row["id"]))
    return updated


def _resume(user: CurrentUser, row: dict) -> dict:
    """paused or paused_feature_off to active with both slots recomputed
    from now. The recipe's sub-app must be on for the caller's role (409
    recipe_unavailable); the cap may refuse (409 schedule_cap_reached), and
    the row then stays paused. Resuming clears the failure and unread
    counts, so the routine does not pause again at its next delivery."""
    current = row.get("status")
    if current == "active":
        return row
    if current not in PAUSED_STATUSES:
        raise _state_changed(user, str(row["id"]))
    recipe = recipes.RECIPES.get(str(row.get("recipe") or ""))
    if recipe is not None and not _role_allows(user, recipe):
        raise _error(status.HTTP_409_CONFLICT, RECIPE_UNAVAILABLE)
    if not _sub_app_on(user, row.get("sub_app") or (recipe.sub_app if recipe else None)):
        raise _error(status.HTTP_409_CONFLICT, RECIPE_UNAVAILABLE)
    now = _now()
    dtstart = _parse_ts(row.get("dtstart")) or _floor_minute(now)
    try:
        slots = rrules.occurrences(str(row.get("rrule") or ""), dtstart, now, 2, _holidays(user))
    except rrules.CadenceError as exc:
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_CADENCE, str(exc)) from None
    if not slots:
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_CADENCE, rrules.NEVER_RUNS_TEXT)
    patch = {
        "status": "active", "paused_reason": None, "paused_at": None, "dtstart": _iso(dtstart),
        "next_run_at": _iso(slots[0]), "after_next_run_at": _iso(slots[1]) if len(slots) > 1 else None,
        "consecutive_failures": 0, "unread_deliveries": 0,
    }
    try:
        updated = repo.update_schedule(user.id, str(row["id"]), patch, expect_status=current,
                                       expect={"paused_reason": row.get("paused_reason")})
    except repo.ScheduleRefused as exc:
        if exc.code == "schedule_cap_active":
            raise _error(status.HTTP_409_CONFLICT, SCHEDULE_CAP_REACHED) from None
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_CADENCE,
                     "This schedule is not one BDR can run.") from None
    if updated is None:
        raise _state_changed(user, str(row["id"]))
    return updated


def _end(user: CurrentUser, row: dict) -> dict:
    """Any status but ended to ended (reason user); its history stays. An
    ended row answers as it is."""
    current = row.get("status")
    if current == "ended":
        return row
    updated = repo.update_schedule(user.id, str(row["id"]), {
        "status": "ended", "ended_at": _iso(_now()), "ended_reason": "user",
        "paused_reason": None, "paused_at": None,
    }, expect_status=current)
    if updated is None:
        raise _state_changed(user, str(row["id"]))
    return updated


def _user_label(user: CurrentUser) -> str:
    """The name the run row records (the claim RPC's rule: the full name,
    else the email, else the id)."""
    human = repo.human_profile(user.id) or {}
    return str((human.get("full_name") or "").strip() or human.get("email") or user.email or user.id)


def _run_now(user: CurrentUser, row: dict) -> dict:
    """One Run now row (3.3.5) the next claim picks up. It delivers through
    the routine's channels, ignores only_if_changed and never moves
    next_run_at."""
    if not get_settings().assistant_scheduler_enabled:
        raise _error(status.HTTP_409_CONFLICT, SCHEDULER_OFF)
    if not (row.get("status") == "active" or (row.get("status") == "paused" and row.get("paused_reason") == "user")):
        raise _error(status.HTTP_409_CONFLICT, SCHEDULE_NOT_ACTIVE)
    schedule_id = str(row["id"])
    now = _now()
    if repo.open_run(user.id, schedule_id) is not None:
        raise _error(status.HTTP_409_CONFLICT, SCHEDULE_BUSY)
    last = repo.last_run(user.id, schedule_id)
    created = _parse_ts((last or {}).get("created_at"))
    spacing = int(get_settings().assistant_run_now_min_seconds)
    if created is not None and (now - created).total_seconds() < spacing:
        raise _error(status.HTTP_429_TOO_MANY_REQUESTS, RUN_NOW_TOO_SOON)
    try:
        return repo.insert_run(user.id, {
            "schedule_id": schedule_id,
            "user_label": _user_label(user),
            "kind": "run_now",
            "status": "claimed",
            "claimed_by": None,
            # Scheduled slots are whole minutes: a non-zero microsecond part
            # keeps a click at hh:00:00 off a slot's occurrence key.
            "scheduled_for": _iso(now.replace(microsecond=now.microsecond or 1)),
        })
    except repo.RunExists:
        raise _error(status.HTTP_429_TOO_MANY_REQUESTS, RUN_NOW_TOO_SOON) from None
    except repo.RunBusy:
        # An overlapping Run now inserted first (0176 A3b).
        raise _error(status.HTTP_409_CONFLICT, SCHEDULE_BUSY) from None


@router.post("/schedules/{schedule_id}/pause", response_model=ScheduleOut)
def pause_schedule(schedule_id: str, user: CurrentUser = Depends(require_internal)) -> ScheduleOut:
    _refuse_assistant_principal(user)
    _require_assistant(user)
    return _out(user, _pause(user, _load(user, schedule_id)))


@router.post("/schedules/{schedule_id}/resume", response_model=ScheduleOut)
def resume_schedule(schedule_id: str, user: CurrentUser = Depends(require_internal)) -> ScheduleOut:
    _refuse_assistant_principal(user)
    _require_assistant(user)
    return _out(user, _resume(user, _load(user, schedule_id)))


@router.post("/schedules/{schedule_id}/end", response_model=ScheduleOut)
def end_schedule(schedule_id: str, user: CurrentUser = Depends(require_internal)) -> ScheduleOut:
    _refuse_assistant_principal(user)
    _require_assistant(user)
    return _out(user, _end(user, _load(user, schedule_id)))


@router.post("/schedules/{schedule_id}/run-now", response_model=ScheduleRunOut,
             status_code=status.HTTP_202_ACCEPTED)
def run_schedule_now(schedule_id: str, user: CurrentUser = Depends(require_internal)) -> ScheduleRunOut:
    _refuse_assistant_principal(user)
    _require_assistant(user)
    return _run_out(_run_now(user, _load(user, schedule_id)))


# ── Housekeeping tool routes (3.6; the assistant's own, turn principals) ────


def _tainted_refusal() -> HTTPException:
    return _error(status.HTTP_409_CONFLICT, TAINTED_TURN)


@router.post(TOOL_PAUSE_ROUTE.removeprefix("/assistant"), response_model=ScheduleOut)
def tool_pause_schedule(schedule_id: str, request: Request,
                        user: CurrentUser = Depends(require_internal)) -> ScheduleOut:
    """schedule_pause: allowed in a tainted turn (it only reduces activity
    and is reversible)."""
    _trusted_turn(request, user, "schedule_pause")
    _require_assistant(user)
    return _out(user, _pause(user, _load(user, schedule_id)))


@router.post(TOOL_RESUME_ROUTE.removeprefix("/assistant"), response_model=ScheduleOut)
def tool_resume_schedule(schedule_id: str, request: Request,
                         user: CurrentUser = Depends(require_internal)) -> ScheduleOut:
    """schedule_resume: refused in a tainted turn (the user resumes in
    Routines)."""
    turn = _trusted_turn(request, user, "schedule_resume")
    _require_assistant(user)
    if turn.get("tainted"):
        raise _tainted_refusal()
    return _out(user, _resume(user, _load(user, schedule_id)))


@router.post(TOOL_END_ROUTE.removeprefix("/assistant"), response_model=ScheduleOut)
def tool_end_schedule(schedule_id: str, request: Request,
                      user: CurrentUser = Depends(require_internal)) -> ScheduleOut:
    """schedule_end ("stop sending that report"). An untainted turn ends
    the routine. Ending is irreversible, so a tainted turn pauses it with
    reason user instead and says so in `note` (deviation D6)."""
    turn = _trusted_turn(request, user, "schedule_end")
    _require_assistant(user)
    row = _load(user, schedule_id)
    if turn.get("tainted") and row.get("status") != "ended":
        return _out(user, _pause(user, row), note=PAUSED_INSTEAD_NOTE)
    return _out(user, _end(user, row))


@router.post(TOOL_RUN_NOW_ROUTE.removeprefix("/assistant"), response_model=ScheduleRunOut,
             status_code=status.HTTP_202_ACCEPTED)
def tool_run_schedule_now(schedule_id: str, request: Request,
                          user: CurrentUser = Depends(require_internal)) -> ScheduleRunOut:
    """schedule_run_now: refused in a tainted turn; the limits of 3.3.5."""
    turn = _trusted_turn(request, user, "schedule_run_now")
    _require_assistant(user)
    if turn.get("tainted"):
        raise _tainted_refusal()
    return _run_out(_run_now(user, _load(user, schedule_id)))


# ── Holidays (3.3.8; Executive and IT Admin, human only) ────────────────────

require_holiday_admin = require_role(Role.EXECUTIVE, Role.IT_ADMIN)


def _holidays_out(row: dict | None) -> HolidaysOut:
    row = row or {}
    return HolidaysOut(holidays=sorted(str(d) for d in row.get("holidays") or []), updated_at=row.get("updated_at"))


def _holiday_dates(values: list[str], today: date) -> list[date]:
    """ISO dates only, distinct, at most 60, each within 2 years of today
    (Pacific) either way; sorted ascending. Raises 422 holidays_invalid."""
    def refuse(message: str) -> HTTPException:
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, HOLIDAYS_INVALID, message)

    if len(values) > HOLIDAYS_MAX:
        raise refuse(f"The holiday list holds at most {HOLIDAYS_MAX} dates.")
    out: list[date] = []
    for value in values:
        text = str(value).strip()
        try:
            day = date.fromisoformat(text) if _ISO_DATE.match(text) else None
        except ValueError:
            day = None
        if day is None:
            raise refuse("Each holiday must be a date written as YYYY-MM-DD.")
        if abs((day - today).days) > HOLIDAYS_SPAN_DAYS:
            raise refuse("Each holiday must be within 2 years of today.")
        if day in out:
            raise refuse("Each holiday can be listed once.")
        out.append(day)
    return sorted(out)


@router.get("/admin/holidays", response_model=HolidaysOut)
def get_holidays(user: CurrentUser = Depends(require_holiday_admin)) -> HolidaysOut:
    _refuse_assistant_principal(user)
    _require_assistant(user)
    return _holidays_out(repo.settings_row(user.id))


@router.put("/admin/holidays", response_model=HolidaysOut)
def put_holidays(body: HolidaysIn, user: CurrentUser = Depends(require_holiday_admin)) -> HolidaysOut:
    """Replace the G3 holiday list weekday routines skip. Refill and the
    claim use it from the next poll; a slot already filled is skipped at
    run time when its Pacific date became a holiday."""
    _refuse_assistant_principal(user)
    _require_assistant(user)
    dates = _holiday_dates(body.holidays, _now().astimezone(rrules.PT).date())
    before = {str(d) for d in (repo.settings_row(user.id) or {}).get("holidays") or []}
    saved = repo.update_holidays(user.id, dates)
    after = {d.isoformat() for d in dates}
    try:
        from app.services import notifications

        notifications.audit(user.id, "assistant.holidays_updated", "assistant_settings", "1", {
            "added": sorted(after - before), "removed": sorted(before - after), "count": len(after),
        })
    except Exception:  # noqa: BLE001 - the list is saved; the audit row is best effort
        logger.exception("assistant: holiday audit failed")
    return _holidays_out(saved)

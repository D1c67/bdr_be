"""Assistant report files (docs/ASSISTANT_PHASE4_PLAN.md 3.9.2, 3.9.3, 3.24).

A report is an .xlsx in the project-files bucket at
`assistant-reports/{user_id}/{report_id}.xlsx` (migration 0180 A4 pins the
path in `assistant_reports_path_chk`), one `assistant_reports` row through
`repo.insert_report`, `expires_at = now() + ASSISTANT_REPORT_TTL_DAYS`, audit
`assistant.report_build` (entity `assistant_report`, payload `{spec, params,
rows}`). Caps: `ASSISTANT_REPORT_MAX_ROWS` rows (truncated written in the
banner), 10 MB file. The response to the model never carries a URL.

Data comes from in-process service calls with the caller's role (the same
functions the GET routes call; no shim hop). The evidence specs read
`app/services/evidence.py` (WI-3) and `file_access.access_report`; the
recipe specs read `app/services/assistant_recipes.py`.

Errors are `ReportError`; `code` and `status_code` tell the route what to
answer: `report_spec_invalid` 422, `report_spec_forbidden` 403,
`report_params_invalid` 422, `report_not_found` 404, `report_too_large` 413.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.core.config import get_settings
from app.core.roles import ACTUAL_BID_VIEWER_ROLES, EVIDENCE_ROLES, INTERNAL_ROLES
from app.services import assistant_report_xlsx as xlsx

logger = logging.getLogger(__name__)

# Who may build and download each spec (3.9.2, 3.9.3). The build route
# refuses `person_evidence` always (only build_person_report writes it); the
# download route checks the caller's live role against this table.
SPEC_ROLES = {
    "bid_invitations": INTERNAL_ROLES,
    "quote_readiness": INTERNAL_ROLES,
    "estimator_vs_bid": ACTUAL_BID_VIEWER_ROLES,
    "project_timeline": EVIDENCE_ROLES,
    "project_file_access": EVIDENCE_ROLES,
    "change_history": EVIDENCE_ROLES,
    "ai_usage": EVIDENCE_ROLES,
    "person_evidence": EVIDENCE_ROLES,
}

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_FILE_BYTES = 10 * 1024 * 1024
PREVIEW_ROWS = 10
MAX_PAGES = 100                      # timeline pages of 200 rows
PAGE_ROWS = 200
STORAGE_PREFIX = "assistant-reports"

# The body fields each spec takes. `date_from` and `date_to` are Pacific
# dates for the evidence specs (`since` and `until` of the evidence
# service). `field` and `group_by` are not in the route body today; the
# service accepts them for a programmatic caller.
SPEC_PARAMS = {
    "bid_invitations": ("range", "date_from", "date_to"),
    "quote_readiness": ("week",),
    "estimator_vs_bid": ("period",),
    "project_timeline": ("project_id", "date_from", "date_to"),
    "project_file_access": ("project_id", "date_from", "date_to"),
    "change_history": ("project_id", "date_from", "date_to", "field"),
    "ai_usage": ("project_id", "date_from", "date_to", "group_by"),
    "person_evidence": ("view", "date_from", "date_to", "project_id", "subject_user_id"),
}

_FILENAME_BAD = re.compile(r"[^A-Za-z0-9 ._()-]")
_AMOUNT_HEADER = re.compile(r"amount|price|difference|cost|total", re.I)


class ReportError(ValueError):
    """A refused report build. `code` is one of report_spec_invalid,
    report_spec_forbidden, report_params_invalid, report_not_found,
    report_too_large; `status_code` is the HTTP status the route answers."""

    _STATUS = {
        "report_spec_invalid": 422,
        "report_spec_forbidden": 403,
        "report_params_invalid": 422,
        "report_not_found": 404,
        "report_too_large": 413,
    }

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status_code = self._STATUS.get(code, 500)


class _Built:
    """One report ready to upload."""

    def __init__(self, *, title: str, filename: str, content: bytes, row_count: int, truncated: bool,
                 totals: dict, preview_rows: list[dict], params: dict) -> None:
        self.title = title
        self.filename = filename
        self.content = content
        self.row_count = row_count
        self.truncated = truncated
        self.totals = totals
        self.preview_rows = preview_rows
        self.params = params


# ── small helpers ────────────────────────────────────────────────────────────


def _role_name(user: Any) -> str:
    role = getattr(user, "role", None)
    return str(getattr(role, "value", role))


def _viewer(user: Any) -> bool:
    return user.role in ACTUAL_BID_VIEWER_ROLES


def live_role_may_read(row: Mapping[str, Any], role: Any) -> bool:
    """Whether a person with the live `role` may still read a stored report
    (3.9.3): the spec's role set, and for a workbook built while its owner
    could see actual bid dates (params.bid_dates), ACTUAL_BID_VIEWER_ROLES
    too, so a demoted owner never fetches dates the new role is denied."""
    if role is None or role not in SPEC_ROLES.get(str(row.get("spec")), frozenset()):
        return False
    params = row.get("params") if isinstance(row.get("params"), Mapping) else {}
    return not params.get("bid_dates") or role in ACTUAL_BID_VIEWER_ROLES


def _max_rows() -> int:
    return int(get_settings().assistant_report_max_rows)


def _pacific_span(date_from: datetime, date_to: datetime | None, today: date | None = None) -> str:
    """The Pacific calendar dates a resolved range covers, inclusive, for a
    filename: "2026-10-19 to 2026-10-24", or one date for a single day. `date_to`
    is the exclusive end (None = open-ended, which runs to today)."""
    start = date_from.astimezone(xlsx.PT).date()
    if date_to is None:
        end = today or datetime.now(xlsx.PT).date()
    else:
        end = date_to.astimezone(xlsx.PT).date() - timedelta(days=1)
    if end <= start:
        return start.isoformat()
    return f"{start.isoformat()} to {end.isoformat()}"


def safe_filename(name: str) -> str:
    """A filename the assistant_reports check accepts (`^[A-Za-z0-9 ._()-]{1,120}\\.xlsx$`)."""
    stem = _FILENAME_BAD.sub("", name.removesuffix(".xlsx"))
    stem = re.sub(r"\s+", " ", stem).strip(" .") or "Report"
    return stem[:110].rstrip(" .") + ".xlsx"


def _clean_params(spec: str, params: dict | None) -> dict:
    """The non-empty params the spec takes. A param the spec does not take
    is refused so a wrong argument is not silently ignored."""
    allowed = SPEC_PARAMS[spec]
    clean: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if key not in allowed:
            raise ReportError("report_params_invalid", f"The {spec} report does not take '{key}'.")
        clean[key] = value.strip() if isinstance(value, str) else value
    return clean


def _date_param(clean: dict, key: str) -> date | None:
    value = clean.get(key)
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ReportError("report_params_invalid", f"{key} must be a date (YYYY-MM-DD).") from None


def _project_id(clean: dict, spec: str) -> str:
    value = clean.get("project_id")
    if not value:
        raise ReportError("report_params_invalid", f"The {spec} report needs a project_id.")
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise ReportError("report_params_invalid", "project_id must be a full project id.") from None


def _json_params(clean: dict) -> dict:
    out: dict[str, Any] = {}
    for key, value in clean.items():
        out[key] = value.isoformat() if isinstance(value, (date, datetime)) else value
    return out


def _banner(window: str, row_count: int, truncated: bool, extra: str = "") -> str:
    parts = [window, f"{row_count} row{'s' if row_count != 1 else ''}"]
    if truncated:
        parts.append(f"truncated: only the first {row_count} rows are included")
    if extra:
        parts.append(extra)
    parts.append("built " + xlsx.pacific(datetime.now(timezone.utc)) + " PT")
    return "  |  ".join(p for p in parts if p)


def _plain(value: Any, kind: str) -> Any:
    """A cell value as the model may read it (no formula guard, no number
    format)."""
    if kind in ("date", "datetime"):
        return xlsx.pacific(value, xlsx.DATE_FORMAT if kind == "date" else xlsx.DATETIME_FORMAT)
    if kind in ("money", "pct", "int"):
        number = xlsx._number(value)
        if number is None:
            return None
        return int(number) if kind == "int" else number
    return xlsx.clean_text(value)[:300]


def preview_from_sheet(columns: list[tuple[str, int, str]], rows: list[list], *, viewer: bool) -> list[dict]:
    """The first PREVIEW_ROWS rows of a sheet as {header: value}. For a role
    outside ACTUAL_BID_VIEWER_ROLES every money and percent column, and any
    header that names an amount, price, difference, cost or total, is
    dropped."""
    keep = [i for i, (header, _, kind) in enumerate(columns)
            if viewer or (kind not in ("money", "pct") and not _AMOUNT_HEADER.search(header))]
    return [{columns[i][0]: _plain(row[i], columns[i][2]) for i in keep} for row in rows[:PREVIEW_ROWS]]


def _generic_sheet(name: str, rows: list[dict], *, skip: tuple[str, ...] = ()) -> xlsx.SheetSpec:
    """A sheet from a list of dicts of unknown shape: scalar values and the
    `label` or `name` of a small dict; ids, emails and nested structures are
    left out; keys ending `_at` (or `at`) are Pacific times."""
    keys: list[str] = []
    for row in rows:
        for key in row:
            if (key in keys or key in skip or key == "id" or key.endswith("_id") or key == "ref"
                    or "email" in key.lower()):
                continue
            keys.append(key)

    def value_of(row: dict, key: str) -> Any:
        value = row.get(key)
        if isinstance(value, dict):
            return value.get("label") or value.get("name") or ""
        if isinstance(value, (list, tuple)):
            return json.dumps(value, default=str)[:300]
        return value

    keys = [k for k in keys if any(value_of(r, k) not in (None, "") for r in rows)]
    columns: list[tuple[str, int, str]] = []
    for key in keys:
        values = [value_of(r, key) for r in rows if value_of(r, key) is not None]
        if key == "at" or key.endswith("_at"):
            kind = "datetime"
        elif values and all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            kind = "int"
        else:
            kind = "text"
        columns.append((key.replace("_", " ").capitalize(), 16 if kind != "text" else 26, kind))
    return (name, columns, [[value_of(r, k) for k in keys] for r in rows])


def _rows_in(result: dict, preferred: tuple[str, ...] = ("rows",)) -> list[dict]:
    for key in preferred:
        value = result.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    for value in result.values():
        if isinstance(value, list) and value and all(isinstance(r, dict) for r in value):
            return list(value)
    return []


def _result_sheets(result: dict, *, skip: tuple[str, ...] = ()) -> list[xlsx.SheetSpec]:
    """One sheet per list of dicts in an evidence result, plus a "Summary"
    sheet of its scalar top-level values."""
    sheets: list[xlsx.SheetSpec] = []
    scalars = [[k.replace("_", " ").capitalize(), v] for k, v in result.items()
               if k not in skip and isinstance(v, (str, int, float, bool)) and not isinstance(v, bool)]
    for key, value in result.items():
        if key in skip:
            continue
        if isinstance(value, list) and value and all(isinstance(r, dict) for r in value):
            sheets.append(_generic_sheet(key.replace("_", " ").capitalize(), value))
    if scalars:
        sheets.append(("Summary", [("Measure", 30, "text"), ("Value", 30, "text")], scalars))
    return sheets


_EVENT_COLUMNS = [("When (Pacific)", 19, "datetime"), ("Source", 14, "text"), ("Kind", 18, "text"),
                  ("Who", 24, "text"), ("What happened", 56, "wrap"), ("Why this is shown", 48, "wrap")]


def _event_row(r: dict) -> list:
    actor = r.get("actor") if isinstance(r.get("actor"), dict) else {}
    return [r.get("at"), r.get("source"), r.get("kind"), actor.get("label") or actor.get("name") or "",
            r.get("summary"), r.get("why")]


def _event_sheet(name: str, rows: list[dict]) -> xlsx.SheetSpec:
    """The common evidence row shape ({at, source, kind, actor, summary, why})
    as a fixed-column sheet; any other shape falls back to a generic one."""
    if rows and not all("summary" in r for r in rows):
        return _generic_sheet(name, rows)
    return (name, list(_EVENT_COLUMNS), [_event_row(r) for r in rows])


def _project_text(project: dict) -> str:
    return f"{project.get('number') or ''} {project.get('name') or ''}".strip()


def _evidence():
    from app.services import evidence

    return evidence


def _map_evidence_error(exc: Exception) -> ReportError:
    code = getattr(exc, "code", "")
    if code == "not_found":
        return ReportError("report_not_found", "That project was not found.")
    return ReportError("report_params_invalid", str(exc) or "The date window is not valid.")


# ── the specs ────────────────────────────────────────────────────────────────


def _build_bid_invitations(sb: Any, user: Any, clean: dict) -> _Built:
    from app.services import bid_invitations as bi
    from app.services.bid_invitations_excel import build_bid_invitations_workbook

    range_ = str(clean.get("range") or "wtd")
    if range_ not in bi.RANGE_NAMES:
        raise ReportError("report_params_invalid", f"Unknown range '{range_}'.")
    try:
        date_from, date_to = bi.resolve_range(range_, clean.get("date_from"), clean.get("date_to"))
    except ValueError as exc:
        raise ReportError("report_params_invalid", str(exc)) from None
    payload = bi.report(date_from, date_to, _role_name(user), range_)
    rows = payload["rows"]
    cap = _max_rows()
    truncated = len(rows) > cap
    if truncated:
        payload = {**payload, "rows": rows[:cap]}
    content = xlsx.strip_em_dashes(build_bid_invitations_workbook(payload))
    viewer = _viewer(user)
    preview = []
    for row in payload["rows"][:PREVIEW_ROWS]:
        item = {
            "project": _project_text(row),
            "status": row.get("status"),
            "gcs": xlsx.clean_text(" / ".join(g.get("name") or "" for g in row.get("gcs") or []))[:200],
            "quotes": f"{row.get('quotes_received', 0)}/{row.get('quotes_requested', 0)}",
            "estimator_returned": xlsx.pacific(row.get("estimator_returned_at")),
            "sent_to_gc": xlsx.pacific(row.get("sent_at")),
            "on_time": row.get("on_time"),
        }
        if viewer and row.get("bid_at"):
            item["bid_at"] = xlsx.pacific(row["bid_at"])
        preview.append(item)
    name = f"Bid Invitations {_pacific_span(date_from, date_to)}"
    params: dict[str, Any] = {"range": range_}
    if range_ == "custom":
        params.update({"date_from": clean.get("date_from"), "date_to": clean.get("date_to")})
    if viewer:
        params["bid_dates"] = True   # the workbook holds actual bid dates (live_role_may_read)
    return _Built(title=f"Bid Invitations: {range_.replace('_', ' ')}", filename=safe_filename(name),
                  content=content, row_count=len(payload["rows"]), truncated=truncated,
                  totals=dict(payload["summary"]), preview_rows=preview, params=params)


def _build_quote_readiness(sb: Any, user: Any, clean: dict) -> _Built:
    from app.assistant import recipes
    from app.services import assistant_recipes as data

    try:
        week = recipes.clean_week(clean["week"]) if "week" in clean else None
        payload = data.quote_readiness(sb, week, datetime.now(timezone.utc), user.role)
    except recipes.RecipeError as exc:
        raise ReportError("report_params_invalid", str(exc)) from None
    viewer = _viewer(user)
    rows = payload["rows"][:_max_rows()]
    truncated = len(payload["rows"]) > len(rows)
    label = payload["window"]["label"]
    monday = datetime.fromisoformat(payload["window"]["start"]).astimezone(xlsx.PT).date()

    columns: list[tuple[str, int, str]] = [("Project", 38, "wrap")]
    if viewer:
        columns.append(("Bid date (Pacific)", 19, "datetime"))
    columns += [("Internal due (Pacific)", 19, "datetime"), ("Key quotes", 11, "text"),
                ("Missing key categories", 32, "wrap"), ("Estimator returned the estimate to us", 24, "datetime")]
    main: list[list] = []
    detail: list[list] = []
    for row in rows:
        project, extra = row["project"], row["extra"]
        line = [_project_text(project)]
        if viewer:
            line.append(project.get("actual_bid_at"))
        line += [project.get("internal_bid_at"), extra["key_quotes"], ", ".join(row.get("missing_categories") or []),
                 extra.get("estimator_returned_at")]
        main.append(line)
        for cat in extra["categories"]:
            detail.append([_project_text(project), cat["category"], cat["vendor_emails"], cat["first_sent_at"],
                           cat["quotes_received"], "Yes" if cat["covered"] else "No",
                           "Yes" if cat["counts_toward_key"] else "No (excluded)"])
    detail_columns = [("Project", 38, "wrap"), ("Category", 24, "text"), ("Vendor emails sent", 12, "int"),
                      ("First sent (Pacific)", 19, "datetime"), ("Quotes back", 11, "int"), ("Covered", 10, "text"),
                      ("Counts toward key quotes", 16, "text")]
    key = "Key quotes exclude: " + ", ".join(c.title() for c in data.KEY_QUOTE_EXCLUDED) + "."
    complete = sum(1 for r in rows if r["extra"]["complete"])
    title = f"Key Quotes: week of {monday.month}/{monday.day}/{monday.year}"
    content = xlsx.build_workbook(title, _banner(label, len(rows), truncated, key),
                                  [("Key Quotes", columns, main), ("Category detail", detail_columns, detail)])
    return _Built(title=title, filename=safe_filename(f"Key Quotes - week of {monday.isoformat()}"), content=content,
                  row_count=len(rows), truncated=truncated,
                  totals={"bids": len(rows), "with_every_key_quote": complete,
                          "no_required_categories": sum(1 for r in rows if r["extra"]["required"] == 0)},
                  preview_rows=preview_from_sheet(columns, main, viewer=viewer),
                  params={"week": monday.isoformat(), **({"bid_dates": True} if viewer else {})})


_EVB_COLUMNS = [("Project", 38, "wrap"), ("Sent to GCs (Pacific)", 19, "datetime"), ("Estimator", 18, "text"),
                ("Round", 7, "int"), ("Estimator price", 17, "money"), ("Our price (lowest sent)", 17, "money"),
                ("Difference", 16, "money"), ("Difference %", 11, "pct"), ("Verdict", 15, "text"),
                ("GCs bid", 8, "int"), ("Notes", 44, "wrap")]


def _evb_line(row: dict) -> list:
    extra = row["extra"]
    notes = []
    if extra.get("message"):
        notes.append(str(extra["message"]))
    if extra.get("revised_after_send"):
        notes.append("The estimator uploaded a newer round after we first sent this bid.")
    if extra.get("alternates_note"):
        notes.append(str(extra["alternates_note"]))
    return [_project_text(row["project"]), extra.get("first_sent_at"), extra.get("estimator_name"),
            extra.get("round"), extra.get("estimator_base"), extra.get("our_min"), extra.get("difference"),
            extra.get("difference_pct"), extra.get("verdict"), extra.get("gc_count"), "\n".join(notes)]


def _build_estimator_vs_bid(sb: Any, user: Any, clean: dict) -> _Built:
    from app.assistant import recipes
    from app.services import assistant_recipes as data

    try:
        period = recipes.clean_period(clean["period"]) if "period" in clean else None
        payload = data.estimator_vs_bid(sb, period, datetime.now(timezone.utc), user.role)
    except recipes.RecipeError as exc:
        if exc.code == "recipe_unavailable":
            raise ReportError("report_spec_forbidden", str(exc)) from None
        raise ReportError("report_params_invalid", str(exc)) from None
    rows = payload["rows"][:_max_rows()]
    truncated = len(payload["rows"]) > len(rows)
    early = payload["extra"]["sent_early"]
    label = payload["window"]["label"]
    totals = payload["totals"]
    lfl, every = totals["like_for_like"], totals["all_rows"]
    main = [_evb_line(r) for r in rows]
    total_columns = [("Measure", 40, "text"), ("Bids", 8, "int"), ("Estimator total", 18, "money"),
                     ("Our total", 18, "money"), ("Difference", 18, "money"), ("Difference %", 12, "pct"),
                     ("Note", 52, "wrap")]
    total_rows = [
        ["Like-for-like (bids with both prices)", lfl["count"], lfl["estimator_total"], lfl["our_total"],
         lfl["difference"], lfl["difference_pct"], "Our price is the lowest price sent to any GC."],
        ["All rows (not like-for-like)", every["count"], every["estimator_total"], every["our_total"], None, None,
         every["note"]],
    ]
    sheets: list[xlsx.SheetSpec] = [("Compared", list(_EVB_COLUMNS), main), ("Totals", total_columns, total_rows)]
    if early:
        sheets.append(("Sent early, due next month", list(_EVB_COLUMNS), [_evb_line(r) for r in early]))
    title = f"Estimator vs Our Price: {label}"
    extra_note = f"{len(early)} bid(s) sent in the month but due the next are on their own sheet and outside the totals."
    content = xlsx.build_workbook(title, _banner(label, len(rows), truncated, extra_note if early else ""), sheets)
    first = datetime.fromisoformat(payload["window"]["start"]).astimezone(xlsx.PT).date()
    return _Built(title=title, filename=safe_filename(f"Estimator vs Our Price - {label}"), content=content,
                  row_count=len(rows), truncated=truncated, totals={**totals, "sent_early": len(early)},
                  preview_rows=preview_from_sheet(_EVB_COLUMNS, main, viewer=_viewer(user)),
                  params={"period": f"{first.year}-{first.month:02d}"})


def _timeline_pages(sb: Any, user: Any, project_id: str, since: date | None, until: date | None) -> dict:
    evidence = _evidence()
    cap = _max_rows()
    rows: list[dict] = []
    cursor = None
    truncated = False
    project: dict = {}
    for _ in range(MAX_PAGES):
        page = evidence.project_timeline(sb, project_id, role=user.role, since=since, until=until, limit=PAGE_ROWS,
                                         cursor=cursor)
        project = project or page.get("project") or {}
        rows.extend(r for r in page.get("rows") or [] if isinstance(r, dict))
        truncated = truncated or bool(page.get("truncated"))
        cursor = page.get("next_cursor")
        if not cursor:
            break
        if len(rows) >= cap:
            truncated = True
            break
    else:
        truncated = True
    if len(rows) > cap:
        rows, truncated = rows[:cap], True
    return {"project": project, "rows": rows, "truncated": truncated}


def _window_text(since: date | None, until: date | None) -> str:
    if since is None and until is None:
        return "default window"

    def fmt(d: date | None) -> str:
        return f"{d.month}/{d.day}/{d.year}" if d else "open"

    return f"{fmt(since)} to {fmt(until)}"


def _build_project_timeline(sb: Any, user: Any, clean: dict) -> _Built:
    project_id = _project_id(clean, "project_timeline")
    since, until = _date_param(clean, "date_from"), _date_param(clean, "date_to")
    evidence = _evidence()
    try:
        result = _timeline_pages(sb, user, project_id, since, until)
    except evidence.EvidenceError as exc:
        raise _map_evidence_error(exc) from None
    project = result["project"]
    sheet = _event_sheet("Timeline", result["rows"])
    label = _project_text(project) or project_id[:8]
    title = f"Project Timeline: {label}"
    return _Built(title=title, filename=safe_filename(f"Project Timeline - {project.get('number') or project_id[:8]}"),
                  content=xlsx.build_workbook(title, _banner(_window_text(since, until), len(result["rows"]),
                                                              result["truncated"]), [sheet]),
                  row_count=len(result["rows"]), truncated=result["truncated"],
                  totals={"rows": len(result["rows"])},
                  preview_rows=preview_from_sheet(sheet[1], sheet[2], viewer=_viewer(user)),
                  params=_json_params({"project_id": project_id, "date_from": since, "date_to": until}))


def _build_project_file_access(sb: Any, user: Any, clean: dict) -> _Built:
    from app.services import file_access

    project_id = _project_id(clean, "project_file_access")
    since, until = _date_param(clean, "date_from"), _date_param(clean, "date_to")
    evidence = _evidence()
    try:
        opens = evidence.project_opens(sb, project_id)
        start, end = evidence.window(since, until)
        access = file_access.access_report(date_from=start, date_to=end, project_id=project_id, actor_kind="human",
                                           include_prefetch=False, limit=_max_rows())
    except evidence.EvidenceError as exc:
        raise _map_evidence_error(exc) from None
    sheets: list[xlsx.SheetSpec] = []
    visits = _rows_in(opens, ("rows", "opens", "users"))
    sheets.append(_generic_sheet("Project visits", visits))
    summary = [r for r in access.get("summary") or [] if isinstance(r, dict)]
    sheets.append(_generic_sheet("Files by person", summary))
    events = [r for r in access.get("events") or [] if isinstance(r, dict)]
    sheets.append(_generic_sheet("File events", events))
    caveats = [[str(c)] for c in opens.get("caveats") or []]
    if caveats:
        sheets.append(("Caveats", [("Caveat", 100, "wrap")], caveats))
    truncated = bool(access.get("truncated"))
    row_count = len(visits) + len(summary)
    title = f"Project File Access: {project_id[:8]}"
    return _Built(title=title, filename=safe_filename(f"Project File Access - {project_id[:8]}"),
                  content=xlsx.build_workbook(title, _banner(_window_text(since, until), row_count, truncated), sheets),
                  row_count=row_count, truncated=truncated, totals={"visit_rows": len(visits), "files": len(summary)},
                  preview_rows=preview_from_sheet(sheets[1][1], sheets[1][2], viewer=_viewer(user)),
                  params=_json_params({"project_id": project_id, "date_from": since, "date_to": until}))


_CHANGE_COLUMNS = [("When (Pacific)", 19, "datetime"), ("Field", 18, "text"), ("Before", 30, "wrap"),
                   ("After", 30, "wrap"), ("Before known", 12, "text"), ("Who", 24, "text"), ("Action", 22, "text")]


def _show(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)[:500]
    return str(value)


def _build_change_history(sb: Any, user: Any, clean: dict) -> _Built:
    project_id = _project_id(clean, "change_history")
    since, until = _date_param(clean, "date_from"), _date_param(clean, "date_to")
    field = clean.get("field")
    evidence = _evidence()
    cap = _max_rows()
    try:
        result = evidence.change_history(sb, project_id, field=field, since=since, until=until, limit=cap,
                                         max_limit=cap)
    except evidence.EvidenceError as exc:
        raise _map_evidence_error(exc) from None
    rows = [r for r in result.get("rows") or [] if isinstance(r, dict)][:cap]
    truncated = bool(result.get("truncated")) or len(result.get("rows") or []) > cap
    lines = []
    for r in rows:
        actor = r.get("actor") if isinstance(r.get("actor"), dict) else {}
        lines.append([r.get("at"), r.get("field"), _show(r.get("before")) if r.get("before_known", True) else "",
                      _show(r.get("after")), "Yes" if r.get("before_known", True) else "No (predates the trail)",
                      actor.get("label") or actor.get("name") or "", r.get("action")])
    project = result.get("project") if isinstance(result.get("project"), dict) else {}
    label = _project_text(project) or project_id[:8]
    title = f"Change History: {label}"
    return _Built(title=title, filename=safe_filename(f"Change History - {project.get('number') or project_id[:8]}"),
                  content=xlsx.build_workbook(title, _banner(_window_text(since, until), len(rows), truncated),
                                              [("Changes", list(_CHANGE_COLUMNS), lines)]),
                  row_count=len(rows), truncated=truncated, totals={"changes": len(rows)},
                  preview_rows=preview_from_sheet(_CHANGE_COLUMNS, lines, viewer=_viewer(user)),
                  params=_json_params({"project_id": project_id, "date_from": since, "date_to": until,
                                       "field": field}))


def _build_ai_usage(sb: Any, user: Any, clean: dict) -> _Built:
    since, until = _date_param(clean, "date_from"), _date_param(clean, "date_to")
    project_id = _project_id(clean, "ai_usage") if clean.get("project_id") else None
    group_by = str(clean.get("group_by") or "feature")
    evidence = _evidence()
    if group_by not in evidence.AI_USAGE_GROUPS:
        raise ReportError("report_params_invalid", "group_by must be feature, model or day.")
    try:
        result = evidence.ai_usage(sb, project_id=project_id, since=since, until=until, group_by=group_by)
    except evidence.EvidenceError as exc:
        raise _map_evidence_error(exc) from None
    sheets = _result_sheets(result, skip=("caps",))
    if not sheets:
        sheets = [("Summary", [("Measure", 30, "text"), ("Value", 30, "text")], [])]
    main = sheets[0]
    row_count = len(main[2])
    title = "AI Usage" + (f": project {project_id[:8]}" if project_id else ": company")
    truncated = bool(result.get("truncated"))
    return _Built(title=title, filename=safe_filename(f"AI Usage - {group_by}"),
                  content=xlsx.build_workbook(title, _banner(_window_text(since, until), row_count, truncated,
                                                              str(result.get("retention_note") or "")), sheets),
                  row_count=row_count, truncated=truncated, totals={"groups": row_count},
                  preview_rows=preview_from_sheet(main[1], main[2], viewer=_viewer(user)),
                  params=_json_params({"project_id": project_id, "date_from": since, "date_to": until,
                                       "group_by": group_by}))


_BUILDERS = {
    "bid_invitations": _build_bid_invitations,
    "quote_readiness": _build_quote_readiness,
    "estimator_vs_bid": _build_estimator_vs_bid,
    "project_timeline": _build_project_timeline,
    "project_file_access": _build_project_file_access,
    "change_history": _build_change_history,
    "ai_usage": _build_ai_usage,
}


# ── upload and record ────────────────────────────────────────────────────────


def _finish(sb: Any, user: Any, spec: str, built: _Built, *, conversation_id: str | None, run_id: str | None,
            approval_id: str | None) -> dict:
    """Upload the workbook, insert the row, audit, and shape the answer. The
    object is removed again when the row cannot be written, so a failed build
    leaves no orphan file."""
    from app.assistant import repo
    from app.services import notifications, storage

    if len(built.content) > MAX_FILE_BYTES:
        raise ReportError("report_too_large", "That report is larger than 10 MB. Narrow the dates or the range.")
    report_id = str(uuid.uuid4())
    user_id = str(user.id)
    path = f"{STORAGE_PREFIX}/{user_id}/{report_id}.xlsx"
    expires = datetime.now(timezone.utc) + timedelta(days=int(get_settings().assistant_report_ttl_days))
    storage.upload_file(path, built.content, XLSX_TYPE)
    row = {
        "id": report_id,
        "user_id": user_id,
        "spec": spec,
        "params": built.params,
        "title": built.title[:200],
        "filename": built.filename,
        "storage_path": path,
        "row_count": built.row_count,
        "truncated": built.truncated,
        "bytes": len(built.content),
        "conversation_id": conversation_id,
        "run_id": run_id,
        "approval_id": approval_id,
        "expires_at": expires.isoformat(),
    }
    try:
        repo.insert_report(user_id, row)
    except Exception:
        try:
            storage.delete_file(path)
        except Exception:  # noqa: BLE001 - the purge rider's orphan pass catches what is left
            logger.exception("assistant report: could not remove %s after a failed insert", path)
        raise
    try:
        notifications.audit(user_id, "assistant.report_build", "assistant_report", report_id,
                            {"spec": spec, "params": built.params, "rows": built.row_count})
    except Exception:  # noqa: BLE001 - the report exists; a lost audit row must not hide it
        logger.exception("assistant report: audit row for %s was not written", report_id)
    return {
        "report_id": report_id,
        "title": built.title,
        "filename": built.filename,
        "spec": spec,
        "row_count": built.row_count,
        "truncated": built.truncated,
        "totals": built.totals,
        "preview_rows": built.preview_rows,
        "expires_at_pt": xlsx.pacific(expires),
    }


def build(sb: Any, user: Any, spec: str, params: dict, *, conversation_id: str | None, run_id: str | None,
          approval_id: str | None = None) -> dict:
    """Build, upload and record one report for `user` (a CurrentUser) from
    in-process service calls with the caller's role. Refuses specs outside
    SPEC_ROLES for the role and always `person_evidence`.

    Returns `{report_id, title, filename, spec, row_count, truncated, totals,
    preview_rows (first 10, amounts stripped per role), expires_at_pt}`.
    Raises ReportError (see the module docstring)."""
    if spec not in SPEC_ROLES:
        raise ReportError("report_spec_invalid", f"There is no report named '{spec}'.")
    if spec == "person_evidence" or user.role not in SPEC_ROLES[spec]:
        raise ReportError("report_spec_forbidden", "Your role cannot build that report.")
    clean = _clean_params(spec, params)
    built = _BUILDERS[spec](sb, user, clean)
    return _finish(sb, user, spec, built, conversation_id=conversation_id, run_id=run_id, approval_id=approval_id)


def build_person_report(sb: Any, user: Any, subject: dict, view: str, result: dict, *,
                        conversation_id: str | None, run_id: str | None, approval_id: str) -> dict:
    """The `person_evidence` workbook, called only by the approval-bound
    `POST /analytics/evidence/users/{user_id}` with the rows that route
    already read (`result` is `evidence.person_evidence`'s output, `subject`
    the profile preview). Same row, path and audit rules as `build`; the
    route calls this BEFORE `repo.insert_evidence_access` and passes the
    returned `report_id` as `xlsx_report_id` in that single insert. The
    ledger is append-only (0180 B4) and is never updated. WI-1 fixed this
    signature (the plan names the function only).

    `subject` is `{id, full_name, role, is_active}`; `view` is one of
    evidence.VIEWS. The caller's role must be in EVIDENCE_ROLES (ReportError
    report_spec_forbidden otherwise). The stored params carry the view, the
    subject id and the window, never the reason. The first sheet holds the
    person's own rows; the assistant's rows appear only as counts on a
    second sheet."""
    if user.role not in SPEC_ROLES["person_evidence"]:
        raise ReportError("report_spec_forbidden", "Your role cannot build that report.")
    rows = [r for r in (result.get("rows") or []) if isinstance(r, dict)]
    cap = _max_rows()
    raw_total = result.get("total")
    total = raw_total if isinstance(raw_total, int) and not isinstance(raw_total, bool) else len(rows)
    # `result` is one person_evidence page: a next page (next_cursor, or a
    # total past the rows) makes the workbook partial, as a source cap does.
    truncated = (bool(result.get("truncated")) or bool(result.get("next_cursor")) or total > len(rows)
                 or len(rows) > cap)
    rows = rows[:cap]
    sheets: list[xlsx.SheetSpec] = [_event_sheet("Evidence", rows)]
    counts = result.get("assistant_counts")
    if isinstance(counts, dict) and counts:
        sheets.append(("Assistant rows", [("Source", 30, "text"), ("Rows written by the assistant", 20, "int")],
                       [[k, v] for k, v in sorted(counts.items())]))
    name = xlsx.clean_text(subject.get("full_name") or "") or "this person"
    title = f"Person Evidence: {name}, {view}"
    stamp = datetime.now(timezone.utc).astimezone(xlsx.PT).date().isoformat()
    params = {"view": view, "subject_user_id": str(subject.get("id") or "")}
    for key in ("since", "until", "project_id"):
        if result.get(key):
            params[key] = str(result[key])
    built = _Built(
        title=title, filename=safe_filename(f"Person Evidence - {view} - {stamp}"),
        content=xlsx.build_workbook(title, _banner(f"view: {view}", len(rows), truncated,
                                                   "Only the person's own actions are counted."), sheets),
        row_count=len(rows), truncated=truncated,
        totals={"rows": len(rows), "total": max(total, len(rows)), "assistant_counts": counts or {}},
        preview_rows=preview_from_sheet(sheets[0][1], sheets[0][2], viewer=_viewer(user)), params=params)
    return _finish(sb, user, "person_evidence", built, conversation_id=conversation_id, run_id=run_id,
                   approval_id=approval_id)

"""Daily "bids due today" digest email.

Every weekday morning (6:03 AM America/Los_Angeles by default) the poller
emails every active internal user except accountants a high-importance list of
the projects whose INTERNAL bid date falls on the current office-calendar day,
with three readiness checks per project:

  * Vendor quotes - at least one quote received for every category (RFQ) on
    the project. Post-0103 the General Material estimate is materialized as a
    real quotes row, so it is checked exactly like every other category.
  * Labor        - labor_reviews.labor_amount is entered.
  * Markups      - markups.labor_markup_amount plus the *_markup_amount of
    every pricing section PRESENT on the project (materials always; gear /
    underground / low_voltage only when an RFQ category maps to them). This
    mirrors the frontend advisory in lib/advanceReadiness.ts.

A row missing vendor quotes renders red; one missing only labor or markup
renders yellow. Checks are computed from the underlying tables, never from
lane completion flags - the Receive Quotes / labor / markup lanes are ungated
server-side and may be "complete" with the data still absent.

Membership mirrors /projects/today but strictly for TODAY (not overdue
carry-over): internal_bid_at inside the office day, excluding pm_only /
cp_only / declined / abandoned and bids already past send_out (submitted,
bid_outcome) - those have nothing left to chase. Days with no qualifying
projects send nothing.

Idempotency and multi-worker safety: due_digest_log rows are upserted with
ignore-duplicates against the (digest_date, user_id) unique index; a recipient
is emailed only when their row was genuinely inserted, so two uvicorn workers
cannot double-send. A failed Graph send deletes its claim row and the next
tick retries that recipient. If the app is down at send time, the digest still
goes out on the first tick inside the catch-up window (default 4 hours), after
which the day is skipped rather than emailing a stale morning report at night.
"""

import asyncio
import html
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.core.roles import INTERNAL_ROLES, Role
from app.core.supabase_client import get_supabase
from app.services import graph_email
from app.services.datetime_format import _parse_ts
from app.services.due_reminders import _page_all, _pg_ts
from app.services.email_branding import (
    LOGO_CONTENT_ID,
    LOGO_FILENAME,
    _BORDER,
    _FONT,
    _MUTED,
    _NAVY,
    _RED,
    _button,
    logo_bytes,
    render_branded_html,
)

logger = logging.getLogger(__name__)

DIGEST_TZ = ZoneInfo("America/Los_Angeles")

# Bids with nothing left to chase this morning: never-bid work, declined bids,
# and bids already sent out (submitted / bid_outcome).
EXCLUDED_STAGES = ("pm_only", "cp_only", "declined", "submitted", "bid_outcome")

# markups columns per pricing section; labor_markup_amount is checked on top.
_SECTION_MARKUP_COLUMNS = {
    "materials": "materials_markup_amount",
    "gear": "gear_markup_amount",
    "underground": "underground_markup_amount",
    "low_voltage": "low_voltage_markup_amount",
}

_OK_GREEN = "#1e7d3c"
_AMBER = "#8a5d00"
_ROW_BG = {"red": "#fbecec", "yellow": "#fff6dd", "ok": "#eef6ee"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def in_send_window(local: datetime, settings) -> bool:
    """Whether a tick at `local` (office time) should attempt today's digest.

    Weekdays only, from the configured send time until the catch-up horizon.
    The ledger makes repeat ticks inside the window no-ops once sent.
    """
    if local.weekday() >= 5:
        return False
    start = local.replace(
        hour=settings.due_digest_send_hour,
        minute=settings.due_digest_send_minute,
        second=0,
        microsecond=0,
    )
    return start <= local < start + timedelta(hours=settings.due_digest_catchup_hours)


def compute_checks(
    rfq_rows: list[dict],
    labor_row: dict | None,
    markup_row: dict | None,
) -> dict[str, bool]:
    """The three readiness booleans for one project.

    `rfq_rows` are the project's rfqs with embedded material_categories
    (pricing_section) and quotes (id-only). A project with no RFQs at all has
    no quotes by definition; its markup check still requires the materials
    section plus labor (matching the frontend, which always shows both).
    """
    has_quotes = bool(rfq_rows) and all(r.get("quotes") for r in rfq_rows)
    has_labor = bool(labor_row) and labor_row.get("labor_amount") is not None

    sections = {"materials"}
    for r in rfq_rows:
        cat = r.get("material_categories") or {}
        sections.add(cat.get("pricing_section") or "materials")
    if markup_row is None:
        has_markups = False
    else:
        has_markups = markup_row.get("labor_markup_amount") is not None and all(
            markup_row.get(_SECTION_MARKUP_COLUMNS[s]) is not None
            for s in sections
            if s in _SECTION_MARKUP_COLUMNS
        )
    return {"has_quotes": has_quotes, "has_labor": has_labor, "has_markups": has_markups}


def severity(checks: dict[str, bool]) -> str:
    """red > yellow > ok. Missing vendor quotes always wins the row color."""
    if not checks["has_quotes"]:
        return "red"
    if not (checks["has_labor"] and checks["has_markups"]):
        return "yellow"
    return "ok"


def _mark(ok: bool, missing_color: str) -> str:
    if ok:
        return (
            f'<span style="color:{_OK_GREEN};font-size:16px;font-weight:bold;">&#10003;</span>'
        )
    return (
        f'<span style="color:{missing_color};font-size:16px;font-weight:bold;">&#10007;</span>'
    )


def _project_label(project: dict) -> str:
    label = project.get("name") or "Untitled project"
    if project.get("number"):
        label = f"{label} (#{project['number']})"
    return label


def _due_time_str(internal_bid_at) -> str:
    dt = _parse_ts(internal_bid_at)
    if dt is None:
        return ""
    return dt.astimezone(DIGEST_TZ).strftime("%I:%M %p").lstrip("0")


def render_digest_email(
    *,
    recipient_name: str | None,
    date_label: str,
    rows: list[dict],
    base_url: str,
) -> str:
    """Branded HTML: greeting, the due-today table (project link + the three
    check cells, row tinted by severity), a color legend, and a Bids Today CTA.

    Each `row` is {project, checks, severity} with `project` holding at least
    id / name / number / internal_bid_at.
    """
    first = (recipient_name or "").strip().split(" ")[0]
    greeting = f"Hi {html.escape(first)}," if first else "Hi there,"
    count = len(rows)
    noun = "bid" if count == 1 else "bids"

    th = (
        f"padding:9px 10px;{_FONT}font-size:11px;letter-spacing:1px;"
        f"color:{_MUTED};text-transform:uppercase;border-bottom:2px solid {_NAVY};"
    )
    body_rows = []
    for row in rows:
        project = row["project"]
        checks = row["checks"]
        bg = _ROW_BG[row["severity"]]
        url = f"{base_url}/projects/{project['id']}"
        due_str = _due_time_str(project.get("internal_bid_at"))
        due_html = (
            f'<div style="font-size:12px;color:{_MUTED};padding-top:2px;">Due {due_str} PT</div>'
            if due_str
            else ""
        )
        cell = f"padding:10px;border-bottom:1px solid {_BORDER};background-color:{bg};"
        body_rows.append(
            f'<tr>'
            f'<td style="{cell}{_FONT}font-size:14px;">'
            f'<a href="{html.escape(url, quote=True)}" target="_blank" '
            f'style="color:{_NAVY};font-weight:bold;text-decoration:underline;">'
            f"{html.escape(_project_label(project))}</a>{due_html}</td>"
            f'<td align="center" style="{cell}">{_mark(checks["has_quotes"], _RED)}</td>'
            f'<td align="center" style="{cell}">{_mark(checks["has_labor"], _AMBER)}</td>'
            f'<td align="center" style="{cell}">{_mark(checks["has_markups"], _AMBER)}</td>'
            f"</tr>"
        )

    table = f"""\
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
          <tr>
            <th align="left" style="{th}">Project</th>
            <th align="center" width="92" style="{th}">Vendor quotes</th>
            <th align="center" width="70" style="{th}">Labor</th>
            <th align="center" width="80" style="{th}">Markups</th>
          </tr>
          {''.join(body_rows)}
        </table>"""

    legend = (
        f'<p style="margin:14px 0 16px;font-size:12px;color:{_MUTED};">'
        f"Vendor quotes needs at least one quote in every category on the project. "
        f'<span style="color:{_RED};font-weight:bold;">Red</span> rows are missing '
        f'vendor quotes; <span style="color:{_AMBER};font-weight:bold;">yellow</span> '
        f"rows are missing the labor number or a markup. Click a project to open it in BDR.</p>"
    )
    body = (
        f'<p style="margin:0 0 14px;color:{_MUTED};">{greeting}</p>'
        f'<div style="font-size:19px;font-weight:bold;color:{_NAVY};margin:0 0 4px;">'
        f"{count} {noun} due today</div>"
        f'<p style="margin:0 0 16px;color:{_MUTED};">{html.escape(date_label)} '
        f"(internal bid date)</p>"
        + table
        + legend
        + _button("Open Bids Today", f"{base_url}/bids-today")
    )
    return render_branded_html(body, subtitle="DAILY BID DIGEST")


def poll_once() -> None:
    settings = get_settings()
    now = _now()
    local = now.astimezone(DIGEST_TZ)
    if not in_send_window(local, settings):
        return
    sb = get_supabase()

    day_start = datetime.combine(local.date(), time.min, tzinfo=DIGEST_TZ).astimezone(
        timezone.utc
    )
    day_end = day_start + timedelta(days=1)
    projects = _page_all(
        lambda lo, hi: sb.table("projects")
        .select("id, name, number, internal_bid_at, current_stage")
        .not_.in_("current_stage", list(EXCLUDED_STAGES))
        .is_("abandoned_at", "null")
        .gte("internal_bid_at", _pg_ts(day_start))
        .lt("internal_bid_at", _pg_ts(day_end))
        .order("internal_bid_at")
        .range(lo, hi)
    )
    if not projects:
        return
    pids = [p["id"] for p in projects]

    # Check data, batched. Paging matters: a truncated rfqs response could drop
    # a quote-less category and flip a red row green.
    rfq_rows = _page_all(
        lambda lo, hi: sb.table("rfqs")
        .select("id, project_id, material_categories(pricing_section), quotes(id)")
        .in_("project_id", pids)
        .order("id")
        .range(lo, hi)
    )
    rfqs_by_pid: dict[str, list[dict]] = {}
    for r in rfq_rows:
        rfqs_by_pid.setdefault(r["project_id"], []).append(r)
    labor_rows = _page_all(
        lambda lo, hi: sb.table("labor_reviews")
        .select("project_id, labor_amount")
        .in_("project_id", pids)
        .order("project_id")
        .range(lo, hi)
    )
    labor_by_pid = {r["project_id"]: r for r in labor_rows}
    markup_rows = _page_all(
        lambda lo, hi: sb.table("markups")
        .select(
            "project_id, labor_markup_amount, materials_markup_amount, "
            "gear_markup_amount, underground_markup_amount, low_voltage_markup_amount"
        )
        .in_("project_id", pids)
        .order("project_id")
        .range(lo, hi)
    )
    markup_by_pid = {r["project_id"]: r for r in markup_rows}

    digest_rows = []
    for project in projects:
        checks = compute_checks(
            rfqs_by_pid.get(project["id"], []),
            labor_by_pid.get(project["id"]),
            markup_by_pid.get(project["id"]),
        )
        digest_rows.append(
            {"project": project, "checks": checks, "severity": severity(checks)}
        )

    # Audience: every active internal user except accountants (and except the
    # external estimator role, which INTERNAL_ROLES already excludes).
    profiles = _page_all(
        lambda lo, hi: sb.table("profiles")
        .select("id, full_name, email, role")
        .eq("is_active", True)
        .order("id")
        .range(lo, hi)
    )
    digest_role_values = {r.value for r in INTERNAL_ROLES if r is not Role.ACCOUNTANT}
    recipients = [
        p for p in profiles if p["role"] in digest_role_values and p.get("email")
    ]
    if not recipients:
        return

    # Claim before sending: only genuinely inserted ledger rows are emailed.
    digest_date = local.date().isoformat()
    ledger_rows = [
        {"digest_date": digest_date, "user_id": p["id"]} for p in recipients
    ]
    inserted = (
        sb.table("due_digest_log")
        .upsert(ledger_rows, on_conflict="digest_date,user_id", ignore_duplicates=True)
        .execute()
    ).data or []
    if not inserted:
        return  # every recipient already got today's digest

    by_id = {p["id"]: p for p in recipients}
    date_label = f"{local.strftime('%A, %B')} {local.day}"
    subject = f"G3 BDR · [HIGH IMPORTANCE] Bids due today: {date_label}"
    base_url = settings.frontend_url.rstrip("/")
    for row in inserted:
        profile = by_id.get(row["user_id"])
        if not profile:
            continue
        try:
            graph_email.send_mail(
                to=[profile["email"]],
                subject=subject,
                body_html=render_digest_email(
                    recipient_name=profile.get("full_name"),
                    date_label=date_label,
                    rows=digest_rows,
                    base_url=base_url,
                ),
                inline_images=[
                    (LOGO_CONTENT_ID, LOGO_FILENAME, logo_bytes(), "image/jpeg")
                ],
                importance="high",
            )
        except Exception:  # noqa: BLE001 - one bad recipient never stops the rest
            logger.exception(
                "Due digest send failed (user=%s date=%s)", profile["id"], digest_date
            )
            try:
                # Release the claim so the next tick retries this recipient.
                sb.table("due_digest_log").delete().eq("id", row["id"]).execute()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Due digest claim rollback failed (user=%s date=%s) - "
                    "this recipient will not be retried today",
                    profile["id"],
                    digest_date,
                )


async def polling_loop() -> None:
    interval = get_settings().due_digest_poll_interval_seconds
    while True:
        try:
            await asyncio.to_thread(poll_once)
        except Exception:  # noqa: BLE001 - the loop must survive any tick failure
            logger.exception("Due digest poll failed")
        await asyncio.sleep(interval)

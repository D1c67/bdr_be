"""Auto send poller (docs/AUTO_SEND.md section 7).

Every tick (AUTO_SEND_POLL_INTERVAL_SECONDS, default 30 s) reads the
auto_send_settings row and, while the IT Admin switch is ON, works four
wall-clock jobs on the Pacific calendar day (any day of the week):

  1. Preflight [preflight_time, send_time): one high-importance email per
     recipient listing what will auto send at the send time, what will not and
     why. Only when at least one non-silent project is due today.
  2. Send [send_time, send_time + catch-up): auto_send.run_send for every due,
     non-silent project with no run row for today. Never on a later day, never
     once the window has closed (re-checked before every claim), and only when
     the switch was already ON at today's send instant: turning it on after
     5:05 PM arms the next day, it never sends today's projects at once. The
     switch is re-read before every project, so turning it off stops the tick.
  3. Missed: once a day's send window has closed (looking back
     AUTO_SEND_MISSED_LOOKBACK_DAYS), every due, non-silent project with no run
     row that existed before that day's send instant, and whose send instant is
     after the latest switch-on, is recorded 'missed' and the Estimating Admin
     is notified (bell + mirror email) to push Send now.
  4. Digest [digest_time, ...): today's results, one high-importance email per
     recipient, once at least one run exists for today. It waits while any of
     today's runs is still 'sending', up to a 30 minute hard deadline.

Multi-worker safety: per-project sends are claimed by the unique
(project_id, bid_date) auto_send_runs row inside auto_send.run_send /
record_missed (a lost race returns None), and the internal emails by the
unique (send_date, kind, user_id) auto_send_email_log row: a recipient is
emailed only when their claim was genuinely inserted. A failed Graph send
deletes its claim so the next tick retries that recipient. GC proposal sends
are never retried by this poller.
"""

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone

from app.core.config import _parse_hhmm, get_settings
from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.services import auto_send, auto_send_emails, graph_email
from app.services.datetime_format import _parse_ts
from app.services.due_reminders import _page_all
from app.services.email_branding import LOGO_CONTENT_ID, LOGO_FILENAME, logo_bytes
from app.services.notifications import notify_role

logger = logging.getLogger(__name__)

# Every wall-clock time here is Pacific.
TZ = auto_send.TZ

# The digest waits for runs still at 'sending' until digest_time plus this.
DIGEST_HARD_DEADLINE = timedelta(minutes=30)

_EMAIL_LOG = "auto_send_email_log"

# Past days whose missed check finished cleanly, keyed to the
# enabled_changed_at it ran under. A past day's set of due projects without a
# run row only changes on an unusual edit, so it is not re-queried every tick.
_settled_days: dict[date, str | None] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _instant(day: date, hm: tuple[int, int]) -> datetime:
    """The UTC instant of Pacific wall-clock `hm` on `day` (DST safe)."""
    return datetime.combine(day, time(hm[0], hm[1]), tzinfo=TZ).astimezone(timezone.utc)


def _date_label(day: date) -> str:
    return f"{day.strftime('%A, %B')} {day.day}"


def _slots(settings) -> dict[str, tuple[int, int]]:
    """The three HH:MM settings, parsed once per tick (validated at boot)."""
    return {
        "preflight": _parse_hhmm(settings.auto_send_preflight_time, "AUTO_SEND_PREFLIGHT_TIME"),
        "send": _parse_hhmm(settings.auto_send_send_time, "AUTO_SEND_SEND_TIME"),
        "digest": _parse_hhmm(settings.auto_send_digest_time, "AUTO_SEND_DIGEST_TIME"),
    }


# ── Reads ─────────────────────────────────────────────────────────────────


def _runs_between(sb, first: date, last: date, columns: str) -> list[dict]:
    return _page_all(
        lambda lo, hi: sb.table("auto_send_runs")
        .select(columns)
        .gte("bid_date", first.isoformat())
        .lte("bid_date", last.isoformat())
        .order("id")
        .range(lo, hi)
    )


def _with_created_at(sb, projects: list[dict]) -> list[dict]:
    """Fill created_at (the missed check needs it) for rows that lack it."""
    missing = [p["id"] for p in projects if "created_at" not in p]
    if not missing:
        return projects
    rows = _page_all(
        lambda lo, hi: sb.table("projects")
        .select("id, created_at")
        .in_("id", missing)
        .order("id")
        .range(lo, hi)
    )
    created = {r["id"]: r.get("created_at") for r in rows}
    return [
        p if "created_at" in p else {**p, "created_at": created.get(p["id"])}
        for p in projects
    ]


def _unclaimed(sb, day: date, kind: str, recipients: list[dict]) -> list[dict]:
    """Recipients without a claim yet: a cheap read that spares the project
    evaluation on every tick after the email went out. The upsert claim in
    _claim_and_send stays the authority."""
    rows = _page_all(
        lambda lo, hi: sb.table(_EMAIL_LOG)
        .select("id, user_id")
        .eq("send_date", day.isoformat())
        .eq("kind", kind)
        .order("id")
        .range(lo, hi)
    )
    claimed = {r["user_id"] for r in rows}
    return [p for p in recipients if p["id"] not in claimed]


# ── Internal email claim + send ───────────────────────────────────────────


def _claim_and_send(sb, *, day: date, kind: str, recipients: list[dict], subject: str,
                    render) -> int:
    """Claim (day, kind, user) per recipient and email only the rows that were
    genuinely inserted. A failed send deletes its claim so the next tick
    retries. Returns how many emails went out."""
    ledger_rows = [
        {"send_date": day.isoformat(), "kind": kind, "user_id": p["id"]} for p in recipients
    ]
    if not ledger_rows:
        return 0
    inserted = (
        sb.table(_EMAIL_LOG)
        .upsert(ledger_rows, on_conflict="send_date,kind,user_id", ignore_duplicates=True)
        .execute()
    ).data or []
    by_id = {p["id"]: p for p in recipients}
    sent = 0
    for row in inserted:
        profile = by_id.get(row.get("user_id"))
        if not profile:
            continue
        try:
            graph_email.send_mail(
                to=[profile["email"]],
                subject=subject,
                body_html=render(profile),
                inline_images=[(LOGO_CONTENT_ID, LOGO_FILENAME, logo_bytes(), "image/jpeg")],
                importance="high",
            )
            sent += 1
        except Exception:  # noqa: BLE001 - one bad recipient never stops the rest
            logger.exception(
                "Auto send %s email failed (user=%s date=%s)", kind, profile["id"], day
            )
            try:
                # Release the claim so the next tick retries this recipient.
                sb.table(_EMAIL_LOG).delete().eq("id", row["id"]).execute()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Auto send %s claim rollback failed (user=%s date=%s): "
                    "this recipient will not be retried today",
                    kind,
                    profile["id"],
                    day,
                )
    return sent


# ── The four jobs ─────────────────────────────────────────────────────────


def _preflight(sb, settings, today: date, send_label: str) -> None:
    recipients = auto_send_emails.notify_recipients(sb)
    if not recipients:
        return
    pending = _unclaimed(sb, today, "preflight", recipients)
    if not pending:
        return  # everyone already has today's preflight
    items = []
    for project in auto_send.due_projects(today, sb=sb):
        try:
            ev = auto_send.evaluate(project["id"], sb=sb)
        except Exception:  # noqa: BLE001 - one project never stops the preflight
            logger.exception(
                "Auto send preflight check failed (project=%s date=%s)", project["id"], today
            )
            ev = _check_failed(project["id"])
        if ev.get("outcome") == "silent":
            continue
        items.append({"project": project, "evaluation": ev})
    if not items:
        return
    date_label = _date_label(today)
    base_url = settings.frontend_url.rstrip("/")
    _claim_and_send(
        sb,
        day=today,
        kind="preflight",
        recipients=pending,
        subject=auto_send_emails.preflight_subject(date_label, send_label),
        render=lambda p: auto_send_emails.render_preflight_email(
            recipient_name=p.get("full_name"),
            date_label=date_label,
            send_label=send_label,
            items=items,
            base_url=base_url,
        ),
    )


CHECK_FAILED_MESSAGE = (
    "This project could not be checked. Open the Auto send panel to review it."
)


def _check_failed(project_id: str) -> dict:
    """The preflight entry for a project whose evaluation raised: listed as
    will not auto send, never dropped (recipients must not read it as fine)."""
    return {
        "project_id": project_id,
        "outcome": "blocked",
        "reasons": [{"code": "check_failed", "message": CHECK_FAILED_MESSAGE}],
        "silent_reason": None,
        "amounts": None,
        "estimate": None,
        "draft": None,
        "gcs": [],
    }


def _other_run_sending(sb, project_id: str, today_iso: str) -> bool:
    """A run of this project for another bid date still at 'sending' (a Send
    now in flight, or a crashed run not yet reclaimed): it may be emailing the
    same GCs, so this tick leaves the project alone and looks again next tick."""
    rows = (
        sb.table("auto_send_runs").select("id, bid_date, status")
        .eq("project_id", project_id).eq("status", "sending").execute()
    ).data or []
    return any(str(r.get("bid_date"))[:10] != today_iso for r in rows)


def _send(sb, today: date, run_keys: set[tuple[str, str]], send_end: datetime) -> None:
    today_iso = today.isoformat()
    for project in auto_send.due_projects(today, sb=sb):
        pid = project["id"]
        if (pid, today_iso) in run_keys:
            continue
        # The switch is re-read before every project: turning it off stops
        # the rest of this tick (no claim, so no run row and no missed row).
        if auto_send._switched_off(sb):
            logger.info("Auto send switched off mid-tick; stopping (date=%s)", today_iso)
            return
        try:
            if auto_send.evaluate(pid, sb=sb).get("outcome") == "silent":
                continue
            if _now() >= send_end:
                # The catch-up window closed while this loop ran: never send
                # after it (the missed check records what is left).
                logger.warning("Auto send window closed mid-loop (date=%s); stopping", today_iso)
                return
            if _other_run_sending(sb, pid, today_iso):
                continue
            auto_send.run_send(pid, today, trigger="scheduled", sb=sb)
        except Exception:  # noqa: BLE001 - one project never stops the rest
            logger.exception("Auto send failed (project=%s date=%s)", pid, today_iso)


def _missed_message(project: dict, day: date, send_label: str) -> str:
    name = project.get("name") or "Untitled project"
    label = f"{name} ({project['number']})" if project.get("number") else name
    return (
        f"Auto send did not run for {label} on {_date_label(day)}. The server was not "
        f"running at {send_label}, so the proposal was not sent. Open the Auto send "
        "panel to send it now."
    )


def _missed(sb, settings, now: datetime, today: date, row: dict, slots: dict,
            send_label: str, run_keys: set[tuple[str, str]]) -> None:
    catchup = timedelta(minutes=settings.auto_send_catchup_minutes)
    changed_raw = row.get("enabled_changed_at")
    changed_at = _parse_ts(changed_raw)
    marker = str(changed_raw) if changed_raw is not None else None
    lookback = settings.auto_send_missed_lookback_days
    for stale in [d for d in _settled_days if d < today - timedelta(days=lookback)]:
        _settled_days.pop(stale, None)
    reasons = [
        {
            "code": "missed",
            "message": (
                f"The server was not running at {send_label}, so this was not sent. "
                "Open the project to send it now."
            ),
        }
    ]
    for offset in range(lookback, -1, -1):
        day = today - timedelta(days=offset)
        send_at = _instant(day, slots["send"])
        if now < send_at + catchup:
            continue  # the send window is still open (or not reached yet)
        if changed_at is not None and send_at <= changed_at:
            continue  # the switch was off (or flipped on after) at that send time
        if day < today and day in _settled_days and _settled_days[day] == marker:
            continue
        day_iso = day.isoformat()
        clean = True
        projects = _with_created_at(sb, auto_send.due_projects(day, sb=sb))
        for project in projects:
            pid = project["id"]
            if (pid, day_iso) in run_keys:
                continue
            created = _parse_ts(project.get("created_at"))
            if created is None or created >= send_at:
                continue  # did not exist at that send time
            try:
                if auto_send.evaluate(pid, sb=sb).get("outcome") == "silent":
                    continue
                inserted = auto_send.record_missed(pid, day, reasons, sb=sb)
            except Exception:  # noqa: BLE001
                clean = False
                logger.exception("Auto send missed check failed (project=%s date=%s)",
                                 pid, day_iso)
                continue
            run_keys.add((pid, day_iso))
            if not inserted:
                continue  # the other worker recorded it (and notified)
            try:
                notify_role(
                    Role.ESTIMATING_ADMIN,
                    pid,
                    "auto_send.missed",
                    _missed_message(project, day, send_label),
                )
            except Exception:  # noqa: BLE001 - the missed row is the record
                logger.exception("Auto send missed notice failed (project=%s)", pid)
        if clean and day < today:
            _settled_days[day] = marker


def _digest(sb, settings, now: datetime, today: date, slots: dict) -> None:
    digest_at = _instant(today, slots["digest"])
    if now < digest_at:
        return
    runs = _runs_between(
        sb,
        today,
        today,
        "id, project_id, bid_date, status, trigger, reasons, markup_pct, estimate, "
        "material_amount, labor_amount, total_amount, redirected_to, sent_at",
    )
    if not runs:
        return
    if any(r.get("status") == "sending" for r in runs) and now < digest_at + DIGEST_HARD_DEADLINE:
        return  # let in-flight sends settle before reporting
    recipients = auto_send_emails.notify_recipients(sb)
    if not recipients:
        return
    pending = _unclaimed(sb, today, "digest", recipients)
    if not pending:
        return

    run_ids = [r["id"] for r in runs]
    pids = sorted({r["project_id"] for r in runs})
    gc_rows = _page_all(
        lambda lo, hi: sb.table("auto_send_gcs")
        .select(
            "id, run_id, gc_id, gc_name, status, error, recipients, "
            "material_amount, labor_amount, total_amount, sent_at"
        )
        .in_("run_id", run_ids)
        .order("id")
        .range(lo, hi)
    )
    gcs_by_run: dict[str, list[dict]] = {}
    for g in gc_rows:
        gcs_by_run.setdefault(g["run_id"], []).append(g)
    for group in gcs_by_run.values():
        group.sort(key=lambda g: (g.get("gc_name") or "").lower())
    project_rows = _page_all(
        lambda lo, hi: sb.table("projects")
        .select("id, name, number")
        .in_("id", pids)
        .order("id")
        .range(lo, hi)
    )
    projects = {p["id"]: p for p in project_rows}
    items = [
        {
            "project": projects.get(r["project_id"]) or {"id": r["project_id"]},
            "run": r,
            "gcs": gcs_by_run.get(r["id"], []),
        }
        for r in runs
    ]
    items.sort(key=lambda i: (str(i["project"].get("number") or ""), i["project"]["id"]))

    date_label = _date_label(today)
    base_url = settings.frontend_url.rstrip("/")
    _claim_and_send(
        sb,
        day=today,
        kind="digest",
        recipients=pending,
        subject=auto_send_emails.digest_subject(date_label),
        render=lambda p: auto_send_emails.render_digest_email(
            recipient_name=p.get("full_name"),
            date_label=date_label,
            items=items,
            base_url=base_url,
        ),
    )


# ── Tick + loop ───────────────────────────────────────────────────────────


def poll_once() -> None:
    settings = get_settings()
    sb = get_supabase()
    try:
        row = auto_send.get_settings_row(sb=sb)
    except Exception:  # noqa: BLE001 - fail closed: no settings, no sends
        logger.exception("Auto send settings read failed; this tick does nothing")
        return
    try:
        auto_send.reclaim_stuck(sb=sb)
    except Exception:  # noqa: BLE001
        logger.exception("Auto send stuck-run reclaim failed")
    if not row.get("enabled"):
        return

    now = _now().astimezone(timezone.utc)
    today = now.astimezone(TZ).date()
    slots = _slots(settings)
    send_label = auto_send_emails.time_label(*slots["send"])
    preflight_at = _instant(today, slots["preflight"])
    send_at = _instant(today, slots["send"])
    send_end = send_at + timedelta(minutes=settings.auto_send_catchup_minutes)

    if preflight_at <= now < send_at:
        try:
            _preflight(sb, settings, today, send_label)
        except Exception:  # noqa: BLE001
            logger.exception("Auto send preflight failed (date=%s)", today)

    run_keys: set[tuple[str, str]] = set()
    try:
        first = today - timedelta(days=settings.auto_send_missed_lookback_days)
        run_keys = {
            (r["project_id"], str(r["bid_date"]))
            for r in _runs_between(sb, first, today, "id, project_id, bid_date")
        }
    except Exception:  # noqa: BLE001 - without the run index nothing can be decided
        logger.exception("Auto send run lookup failed (date=%s)", today)
        return

    # Catch-up covers a server that was down at the send time, never a switch
    # that was off then: a switch turned on after today's send instant arms
    # the next day (the missed check skips today for the same reason).
    changed_at = _parse_ts(row.get("enabled_changed_at"))
    switched_on_after_send = changed_at is not None and send_at <= changed_at
    if send_at <= now < send_end:
        if switched_on_after_send:
            logger.info(
                "Auto send switched on after today's send time; nothing sent today (date=%s)",
                today,
            )
        else:
            try:
                _send(sb, today, run_keys, send_end)
            except Exception:  # noqa: BLE001
                logger.exception("Auto send step failed (date=%s)", today)

    try:
        _missed(sb, settings, now, today, row, slots, send_label, run_keys)
    except Exception:  # noqa: BLE001
        logger.exception("Auto send missed check failed (date=%s)", today)

    try:
        _digest(sb, settings, now, today, slots)
    except Exception:  # noqa: BLE001
        logger.exception("Auto send digest failed (date=%s)", today)


async def polling_loop() -> None:
    interval = get_settings().auto_send_poll_interval_seconds
    while True:
        try:
            await asyncio.to_thread(poll_once)
        except Exception:  # noqa: BLE001 - the loop must survive any tick failure
            logger.exception("Auto send poll failed")
        await asyncio.sleep(interval)

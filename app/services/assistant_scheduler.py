"""The assistant routine loop (docs/ASSISTANT_PHASE3_PLAN.md 3.4).

Both uvicorn workers run `polling_loop` from the main.py lifespan while
ASSISTANT_ENABLED and ASSISTANT_SCHEDULER_ENABLED are on. Each poll refills
the second occurrence slot, claims due occurrences through the claim RPC
(row locks with skip locked plus the occurrence key make a double claim
harmless), checks the owner, reads the recipe through the shim as the
owner's principal, and delivers to chat, bell and email-to-self through the
per-channel claim ledger. Recipes run no model (deviation D4).

This module lives outside app/assistant/ (main.py never names that
package) and imports app.assistant.* lazily inside functions. Every
assistant_* row is read and written through app/assistant/repo.py; the one
direct read is email_log (the per-day email cap), which is not an assistant
table.

Fixed text only: every chat line, bell and email subject is built from the
schedule's server-built title (enums and a project number, deviation D3)
and counts. Project names reach the person only through the recipe
message's table and project_link blocks and the escaped email table.

Plain `def` throughout except `polling_loop`: each poll runs in a thread
(asyncio.to_thread), and the executor opens its own event loop per request.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.core import provenance, supabase_client
from app.core.config import get_settings

logger = logging.getLogger(__name__)

PT = ZoneInfo("America/Los_Angeles")
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

# uuid5 namespace of the routine message ids: one message per run, ever.
RUN_MESSAGE_NAMESPACE = uuid.UUID("8c2d6f1e-4b7a-4e95-9d31-6a0f5c2b7e43")

REFILL_LIMIT = 100
REAP_LIMIT = 50
RESUME_LIMIT = 100
RESUME_EVERY_SECONDS = 10 * 60     # the resume pass, at most this often per process
FENCE_RETRIES = 3                  # read-modify-write counters on a lost fence

BELL_DELIVERED = "assistant.schedule_delivered"
BELL_PAUSED = "assistant.schedule_paused"
BELL_RESUMED = "assistant.schedule_resumed"
BELL_ENDED = "assistant.schedule_ended"

DELIVERY_BELL = ("chat_bell", "chat_bell_email")
DELIVERY_EMAIL = ("chat_bell_email",)

# Skip kinds (never a failure, 3.4.6).
SKIP_NOT_ACTIVE = "not_active"
SKIP_FEATURE_OFF = "feature_off"
SKIP_OWNER_CHANGED = "owner_changed"
SKIP_HOLIDAY = "holiday"
SKIP_WATCH_FINISHED = "watch_finished"
SKIP_DIGEST_SENT = "digest_sent"

# Email outcomes on an otherwise delivered run (never a failure).
EMAIL_CAP = "email_cap"
EMAIL_FAILED = "email_failed"
EMAIL_UNAVAILABLE = "email_unavailable"

TEXT_WATCH_ENDED = "This watch ended: the bid was submitted, closed or removed."
TEXT_FAILED = {
    "firewall": "The routine's read was refused.",
    "not_picked_up": "The Run now request was not picked up in time.",
    "abandoned": "The run did not finish.",
    "recipe_failed": "The routine's bids could not be read.",
    "chat_failed": "The routine's message could not be posted.",
    "crashed": "The routine could not run.",
}
BELL_TEXT_FAILURES = "Routine paused after {n} failed runs: {title}."
BELL_TEXT_UNREAD = "Routine paused: {title}. {n} deliveries are unread. Resume it on the Routines tab."
BELL_TEXT_FEATURE_OFF = "Routine paused because bidding is switched off: {title}."
BELL_TEXT_NO_FUTURE_RUN = "Routine paused because it has no future run: {title}."
BELL_TEXT_RESUMED = "Routine resumed: {title}."
BELL_TEXT_WATCH_ENDED = "Routine ended: {title}. The watched bid was submitted, closed or removed."

_last_resume: float | None = None


# ── Small helpers ────────────────────────────────────────────────────────────


def _now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def message_id_for(run_id: str) -> str:
    """The routine message's deterministic id: a retry never posts twice."""
    return str(uuid.uuid5(RUN_MESSAGE_NAMESPACE, f"schedule-run:{run_id}"))


def _holiday_dates(holidays: Any) -> frozenset[date]:
    out: set[date] = set()
    for h in holidays or ():
        if isinstance(h, datetime):
            out.add(h.date())
        elif isinstance(h, date):
            out.add(h)
        else:
            try:
                out.add(date.fromisoformat(str(h)[:10]))
            except ValueError:
                continue
    return frozenset(out)


def _holidays(user_id: str) -> frozenset[date]:
    """The company holiday list (assistant_settings.holidays, id = 1)."""
    from app.assistant import repo

    return _holiday_dates((repo.settings_row(user_id) or {}).get("holidays"))


def _dtstart(schedule: dict) -> datetime:
    return _ts(schedule.get("dtstart")) or _ts(schedule.get("created_at")) or _now()


def _title(schedule: dict) -> str:
    return str(schedule.get("title") or "your routine")


def _bell(schedule: dict, type_: str, text: str, *, run_id: str | None = None,
          project_gone: bool = False) -> None:
    """notify_user without an email mirror (and so no Teams copy); best
    effort. The routine's own status bells (paused, resumed, ended) are
    system notices, outside any assistant scope. `project_gone` (a watch
    whose project was deleted or merged) sends no project_id, since
    notifications.project_id references projects(id); the watched id stays
    in the metadata."""
    try:
        from app.services import notifications

        metadata = {"schedule_id": str(schedule.get("id")),
                    "assistant_conversation_id": schedule.get("conversation_id")}
        if run_id:
            metadata["schedule_run_id"] = run_id
        project_id = schedule.get("project_id")
        if project_gone:
            if project_id:
                metadata["project_id"] = str(project_id)
            project_id = None
        notifications.notify_user(
            str(schedule["user_id"]), project_id, type_, text,
            mirror_email=False, metadata=metadata,
        )
    except Exception:  # noqa: BLE001 - a bell never fails the run
        logger.exception("assistant scheduler: %s bell failed for %s", type_, schedule.get("id"))


def _pause(schedule: dict, reason: str, *, bell_text: str | None = None,
           expect_status: str = "active") -> bool:
    """One fenced move into paused (paused_at set). True when this call moved
    it; the bell rings only then."""
    from app.assistant import repo

    moved = repo.update_schedule(
        str(schedule["user_id"]), str(schedule["id"]),
        {"status": "paused", "paused_reason": reason, "paused_at": _now().isoformat()},
        expect_status=expect_status,
    )
    if moved is not None and bell_text:
        _bell(moved, BELL_PAUSED, bell_text.format(title=_title(moved), n=""))
    return moved is not None


# ── Run bookkeeping ──────────────────────────────────────────────────────────


def _skip(run: dict, kind: str, *, expect: str | list[str] = "claimed", extra: dict | None = None) -> bool:
    from app.assistant import repo

    return repo.update_run(str(run["user_id"]), str(run["id"]), {
        "status": "skipped", "error_kind": kind, "finished_at": _now().isoformat(), **(extra or {}),
    }, expect_status=expect)


def _count_failure(user_id: str, schedule_id: str) -> None:
    """consecutive_failures + 1, fenced on the value read; at the ceiling an
    active routine pauses as failures with a bell (3.4.6)."""
    from app.assistant import repo

    ceiling = int(get_settings().assistant_schedule_max_failures)
    for _ in range(FENCE_RETRIES):
        schedule = repo.get_schedule(user_id, schedule_id)
        if schedule is None or schedule.get("status") == "ended":
            return
        current = int(schedule.get("consecutive_failures") or 0)
        n = current + 1
        patch: dict[str, Any] = {"consecutive_failures": n}
        pausing = n >= ceiling and schedule.get("status") == "active"
        if pausing:
            patch.update({"status": "paused", "paused_reason": "failures", "paused_at": _now().isoformat()})
        moved = repo.update_schedule(user_id, schedule_id, patch, expect_status=schedule.get("status"),
                                     expect={"consecutive_failures": current})
        if moved is None:
            continue
        if pausing:
            _bell(moved, BELL_PAUSED, BELL_TEXT_FAILURES.format(n=n, title=_title(moved)))
        return
    logger.warning("assistant scheduler: failure count of %s lost its fence", schedule_id)


def _fail_run(run: dict, kind: str, *, expect: str | list[str] | None = None) -> bool:
    """Fail one run (fenced on its open status) and count the failure."""
    from app.assistant import repo

    if expect is None:
        expect = list(repo.OPEN_RUN_STATUSES)
    moved = repo.update_run(str(run["user_id"]), str(run["id"]), {
        "status": "failed", "error_kind": kind, "error": TEXT_FAILED.get(kind, TEXT_FAILED["recipe_failed"]),
        "finished_at": _now().isoformat(),
    }, expect_status=expect)
    if moved:
        _count_failure(str(run["user_id"]), str(run["schedule_id"]))
    return moved


def _reset_failures(schedule: dict, patch: dict | None = None, *, notified: bool = False) -> dict | None:
    """After a delivered or no_change run: consecutive_failures = 0 plus
    `patch`, and unread_deliveries + 1 when a bell or an email went out
    (fenced on the value read). At the unread ceiling an active routine
    pauses as unread with a bell. Returns the row as written."""
    from app.assistant import repo

    user_id, schedule_id = str(schedule["user_id"]), str(schedule["id"])
    ceiling = int(get_settings().assistant_schedule_max_unread)
    row = schedule
    for _ in range(FENCE_RETRIES):
        unread = int(row.get("unread_deliveries") or 0)
        body: dict[str, Any] = {"consecutive_failures": 0, **(patch or {})}
        pausing = False
        if notified:
            body["unread_deliveries"] = unread + 1
            pausing = unread + 1 >= ceiling and row.get("status") == "active"
            if pausing:
                body.update({"status": "paused", "paused_reason": "unread", "paused_at": _now().isoformat()})
        moved = repo.update_schedule(user_id, schedule_id, body, expect_status=row.get("status"),
                                     expect={"unread_deliveries": unread})
        if moved is not None:
            if pausing:
                _bell(moved, BELL_PAUSED, BELL_TEXT_UNREAD.format(title=_title(moved), n=unread + 1))
            return moved
        fresh = repo.get_schedule(user_id, schedule_id)
        if fresh is None:
            return None
        row = fresh
    logger.warning("assistant scheduler: delivery bookkeeping of %s lost its fence", schedule_id)
    return None


# ── Step 1: refill ───────────────────────────────────────────────────────────


def refill(now: datetime | None = None) -> int:
    """Fill after_next_run_at on active rows whose second slot is empty
    (3.4.2 step 1). Never writes next_run_at. base = max(next_run_at, now
    minus the catch-up window), so an outage's stale next_run_at gets an
    `after` past the stale stretch: the claim records the stale slot as
    `missed` and advances. A rule with no future occurrence pauses as
    no_future_run with a bell. Returns how many slots were filled."""
    from app.assistant import repo, rrules

    now = _now(now)
    catchup = timedelta(hours=int(get_settings().assistant_schedule_catchup_hours))
    holidays: frozenset[date] | None = None
    filled = 0
    for row in repo.schedules_to_refill(None, limit=REFILL_LIMIT):
        try:
            if holidays is None:
                holidays = _holidays(str(row["user_id"]))
            next_at = _ts(row.get("next_run_at")) or now
            base = max(next_at, now - catchup)
            try:
                found = rrules.occurrences(str(row["rrule"]), _dtstart(row), base, 1, holidays)
            except rrules.CadenceError:
                found = []
            if not found:
                _pause(row, "no_future_run", bell_text=BELL_TEXT_NO_FUTURE_RUN)
                continue
            if repo.fill_after_slot(str(row["user_id"]), str(row["id"]), next_run_at=row["next_run_at"],
                                    after_next_run_at=found[0].isoformat()):
                filled += 1
        except Exception:  # noqa: BLE001 - one bad row never stops the pass
            logger.exception("assistant scheduler: refill of %s failed", row.get("id"))
    return filled


# ── Steps 3.4.3 to 3.4.5: one claimed run ────────────────────────────────────


def _runnable(schedule: dict, manual: bool) -> bool:
    if schedule.get("status") == "active":
        return True
    # A Run now click on a routine the person paused still runs (3.3.5).
    return manual and schedule.get("status") == "paused" and schedule.get("paused_reason") == "user"


def read_recipe(run: dict, schedule: dict, role: Any) -> Any:
    """The recipe rows through the shim as the owner's principal (3.4.4):
    an Executor over the never-registered RECIPE_READ_SPEC, a bdra_ token
    for (owner, run id). The firewall, require_feature, redact_for_role and
    the live profile and delegation checks all apply; no ledger row (the
    run row is the record). Raises executor.RequestFailed."""
    from app.assistant import recipes
    from app.assistant.executor import Executor

    spec = recipes.RECIPE_READ_SPEC
    params = _dict(schedule.get("params"))
    args: dict[str, Any] = {"recipe": schedule["recipe"], "at": _now(_ts(run.get("scheduled_for"))).isoformat()}
    for key in ("horizon_days", "project_id", "period", "week"):
        if params.get(key) not in (None, ""):
            args[key] = params[key]
    ex = Executor(user_id=str(run["user_id"]), user_label=str(run.get("user_label") or run["user_id"]),
                  run_id=str(run["id"]), specs={spec.name: spec}, lease_owner=WORKER_ID, role=role)
    return ex.read(spec.requests(args), spec)[0]


def _conversation_for(schedule: dict) -> str:
    """The schedule's conversation, created on the first delivery (surface
    'schedule', tainted at once: recipe output names projects), linked by a
    fenced update. On a lost fence the conversation that won is used; the
    extra one stays empty and is never listed."""
    from app.assistant import repo

    user_id, schedule_id = str(schedule["user_id"]), str(schedule["id"])
    current = schedule.get("conversation_id")
    if current:
        return str(current)
    conversation = repo.create_conversation(user_id, surface="schedule", title=_title(schedule))
    conversation_id = str(conversation["id"])
    repo.mark_conversation_tainted(user_id, conversation_id)
    if repo.set_schedule_conversation(user_id, schedule_id, conversation_id):
        schedule["conversation_id"] = conversation_id
        return conversation_id
    won = (repo.get_schedule(user_id, schedule_id) or {}).get("conversation_id")
    if won:
        schedule["conversation_id"] = str(won)
        return str(won)
    return conversation_id


def _post_chat(run: dict, schedule: dict, content: str, blocks: list[dict]) -> str | None:
    """The chat channel: claim, insert the message once (deterministic id),
    move the conversation up, done. Raises when the post fails (the channel
    records failed first). Returns the message id, or None when the claim
    was refused and no message exists: the run left 'running' (the reaper
    failed it), so nothing was delivered."""
    from app.assistant import repo

    user_id, run_id = str(run["user_id"]), str(run["id"])
    message_id = message_id_for(run_id)
    if not repo.run_channel(user_id, run_id, "chat", "claim"):
        return message_id if repo.get_message(user_id, message_id) is not None else None
    try:
        conversation_id = _conversation_for(schedule)
        existing = repo.get_message(user_id, message_id)
        if existing is None:
            row = {
                "id": message_id, "conversation_id": conversation_id, "role": "assistant", "status": "done",
                "content": content, "blocks": list(blocks) or [{"type": "text", "text": content}],
                "completed_at": _now().isoformat(),
            }
            try:
                existing = repo.insert_message(user_id, row)
            except Exception:
                # A 23505 on the primary key: it is already there.
                existing = repo.get_message(user_id, message_id)
                if existing is None:
                    raise
        try:
            repo.update_conversation(user_id, conversation_id, {
                "last_message_at": str((existing or {}).get("created_at") or _now().isoformat()),
            })
        except Exception:  # noqa: BLE001 - the sort order is a convenience
            logger.warning("assistant scheduler: conversation touch failed for %s", conversation_id)
    except Exception:
        repo.run_channel(user_id, run_id, "chat", "failed")
        raise
    repo.run_channel(user_id, run_id, "chat", "done", True)
    return message_id


def _emails_today(user_id: str, local_date: date) -> int:
    """email_log rows the person's assistant sent on this Pacific day (index
    email_log_sent_by_idx). The scheduler never passes through the outbound
    bucket, so this is its per-user cap (3.4.5)."""
    start = datetime.combine(local_date, datetime.min.time(), tzinfo=PT).astimezone(timezone.utc)
    end = datetime.combine(local_date + timedelta(days=1), datetime.min.time(), tzinfo=PT).astimezone(timezone.utc)
    cap = int(get_settings().assistant_schedule_max_emails_per_day)
    rows = (
        supabase_client.get_supabase().table("email_log").select("id")
        .eq("sent_by", user_id).eq("actor_kind", provenance.ACTOR_ASSISTANT)
        .gte("created_at", start.isoformat()).lt("created_at", end.isoformat())
        .limit(cap + 1).execute()
    ).data or []
    return len(rows)


def _ring(run: dict, schedule: dict, payload: dict) -> bool:
    """The bell channel, inside the run's provenance scope. True when it rang."""
    from app.assistant import recipes, repo

    user_id, run_id = str(run["user_id"]), str(run["id"])
    if not repo.run_channel(user_id, run_id, "bell", "claim"):
        return False
    try:
        from app.services import notifications

        with provenance.acting_as_assistant(user_id, run_id, None):
            notifications.notify_user(
                user_id, schedule.get("project_id"), BELL_DELIVERED, recipes.bell_text(_title(schedule), payload),
                mirror_email=False, metadata={
                    "schedule_id": str(schedule["id"]), "schedule_run_id": run_id,
                    "assistant_conversation_id": schedule.get("conversation_id"),
                },
            )
    except Exception:  # noqa: BLE001 - a lost bell never fails the run
        logger.exception("assistant scheduler: bell failed for run %s", run_id)
        repo.run_channel(user_id, run_id, "bell", "failed")
        return False
    repo.run_channel(user_id, run_id, "bell", "done", True)
    return True


def _email(run: dict, schedule: dict, payload: dict, human: dict | None, now: datetime) -> tuple[bool, str | None]:
    """The email channel: (sent, error_kind). Claimed before anything else;
    the cap, a missing Graph setup or address, and a send failure each
    record `failed` with their error_kind (never a failure count). Sent
    through graph_email.send_mail inside the run's provenance scope, so the
    email_log row carries actor_kind 'assistant' and the run id; never
    notification_email.queue (fire and forget, no claim)."""
    from app.assistant import repo
    from app.services import assistant_recipes

    user_id, run_id = str(run["user_id"]), str(run["id"])
    settings = get_settings()
    over_cap = _emails_today(user_id, now.astimezone(PT).date()) >= int(settings.assistant_schedule_max_emails_per_day)
    if not repo.run_channel(user_id, run_id, "email", "claim"):
        return False, None
    if over_cap:
        repo.run_channel(user_id, run_id, "email", "failed")
        return False, EMAIL_CAP
    address = str((human or {}).get("email") or "").strip()
    if not settings.ms_client_id or not address:
        repo.run_channel(user_id, run_id, "email", "failed")
        return False, EMAIL_UNAVAILABLE
    try:
        from app.services import graph_email
        from app.services.email_branding import LOGO_CONTENT_ID, LOGO_FILENAME, logo_bytes

        n = len(payload.get("rows") or [])
        with provenance.acting_as_assistant(user_id, run_id, None):
            logged = graph_email.send_mail(
                to=[address],
                subject=f"G3 BDR · Your routine: {_title(schedule)} ({n})",
                body_html=assistant_recipes.render_email(
                    _title(schedule), payload, recipient_name=str((human or {}).get("full_name") or ""),
                    base_url=settings.frontend_url,
                ),
                inline_images=[(LOGO_CONTENT_ID, LOGO_FILENAME, logo_bytes(), "image/jpeg")],
                sent_by=user_id,
            )
    except Exception:  # noqa: BLE001 - the chat message already landed
        logger.exception("assistant scheduler: email failed for run %s", run_id)
        repo.run_channel(user_id, run_id, "email", "failed")
        return False, EMAIL_FAILED
    log_id = _dict(logged).get("id")
    repo.run_channel(user_id, run_id, "email", "done", str(log_id) if log_id else True)
    return True, None


def _end_watch(run: dict, schedule: dict, state: str | None = None) -> None:
    """Step 6: the watched bid was submitted, closed or removed. One fixed
    chat line, the run skipped watch_finished, the routine ended, a bell."""
    from app.assistant import repo

    user_id = str(run["user_id"])
    message_id = None
    try:
        message_id = _post_chat(run, schedule, TEXT_WATCH_ENDED, [])
    except Exception:  # noqa: BLE001 - the end still happens
        logger.exception("assistant scheduler: watch end line failed for %s", schedule.get("id"))
    _skip(run, SKIP_WATCH_FINISHED, expect="running", extra={"message_id": message_id} if message_id else None)
    now = _now().isoformat()
    ended = repo.update_schedule(user_id, str(schedule["id"]), {
        "status": "ended", "ended_at": now, "ended_reason": "watch_finished",
        "paused_reason": None, "paused_at": None,
    }, expect_status=schedule.get("status"))
    if ended is not None:
        _bell(ended, BELL_ENDED, BELL_TEXT_WATCH_ENDED.format(title=_title(ended)), run_id=str(run["id"]),
              project_gone=state == "missing")


def run_claimed(run: dict, now: datetime | None = None) -> str:
    """One claimed run (3.4.3). Returns what happened: a skip kind,
    'lost' (another worker moved the run), 'failed', 'no_change' or
    'delivered'. Owner problems and skips never count as failures."""
    from app.assistant import access, recipes, repo, rrules
    from app.assistant.executor import RequestFailed
    from app.core.roles import Role

    now = _now(now)
    user_id, run_id = str(run["user_id"]), str(run["id"])
    manual = run.get("kind") == "run_now"

    # 1. The schedule must still run.
    schedule = repo.get_schedule(user_id, str(run["schedule_id"]))
    if schedule is None or not _runnable(schedule, manual):
        _skip(run, SKIP_NOT_ACTIVE)
        return SKIP_NOT_ACTIVE

    # 2. The owner (no failure count; map gap 7).
    check = access.owner_check(user_id, sub_app=schedule.get("sub_app"), require_writer=False)
    if check.problem == access.FEATURE_OFF:
        _skip(run, SKIP_FEATURE_OFF)
        if schedule.get("status") == "active":
            moved = repo.update_schedule(user_id, str(schedule["id"]), {
                "status": "paused_feature_off", "paused_reason": None, "paused_at": now.isoformat(),
            }, expect_status="active")
            if moved is not None:
                _bell(moved, BELL_PAUSED, BELL_TEXT_FEATURE_OFF.format(title=_title(moved)))
        return SKIP_FEATURE_OFF
    if check.problem is not None:
        _skip(run, SKIP_OWNER_CHANGED)
        if schedule.get("status") == "active":
            reason = "no_delegation" if check.detail == access.DETAIL_NO_DELEGATION else "owner_changed"
            _pause(schedule, reason)
        return SKIP_OWNER_CHANGED

    # 2b. A recipe only some roles may run (estimator_vs_bid: the roles that
    #     see actual bids) pauses for an owner who lost the role, as an owner
    #     change, instead of failing its reads three times.
    spec = recipes.RECIPES.get(str(schedule.get("recipe") or ""))
    if spec is not None and check.role is not None and check.role not in spec.roles:
        _skip(run, SKIP_OWNER_CHANGED)
        if schedule.get("status") == "active":
            _pause(schedule, "owner_changed")
        return SKIP_OWNER_CHANGED

    # 3. A holiday added after the slots were filled.
    if not manual:
        when = _ts(run.get("scheduled_for")) or now
        try:
            weekday_rule = rrules.weekday_only(rrules.from_rrule(str(schedule["rrule"])))
        except rrules.CadenceError:
            weekday_rule = False
        if weekday_rule and when.astimezone(PT).date() in _holidays(user_id):
            _skip(run, SKIP_HOLIDAY)
            return SKIP_HOLIDAY

    # 4. claimed -> running; a lost fence ends here.
    if not repo.update_run(user_id, run_id, {"status": "running"}, expect_status="claimed"):
        return "lost"

    # 5. The recipe read through the shim.
    try:
        payload = read_recipe(run, schedule, check.role)
    except RequestFailed as exc:
        _fail_run(run, exc.kind if exc.kind == "firewall" or exc.kind.startswith("http_") else "recipe_failed",
                  expect="running")
        return "failed"
    except Exception:  # noqa: BLE001 - one bad read fails this run only
        logger.exception("assistant scheduler: recipe read failed for run %s", run_id)
        _fail_run(run, "recipe_failed", expect="running")
        return "failed"
    if not isinstance(payload, dict):
        _fail_run(run, "recipe_failed", expect="running")
        return "failed"
    rows = [r for r in payload.get("rows") or [] if isinstance(r, dict)]
    recipe = str(schedule["recipe"])

    # 6. A project watch whose bid is no longer open ends the routine.
    watch = payload.get("watch")
    if isinstance(watch, dict) and watch.get("state") not in (None, "open"):
        _end_watch(run, schedule, str(watch.get("state")))
        return SKIP_WATCH_FINISHED

    # 7. The precedence stub against the 6:03 AM digest (scheduled due_today
    #    runs only; an Accountant never gets the digest).
    if (not manual and recipe == "due_today" and payload.get("digest_sent_today")
            and check.role is not Role.ACCOUNTANT):
        _skip(run, SKIP_DIGEST_SENT, expect="running")
        return SKIP_DIGEST_SENT

    # 8. only_if_changed (scheduled runs only; Run now always delivers).
    result_hash = recipes.result_hash(recipe, rows)
    if not manual and schedule.get("only_if_changed") and result_hash == schedule.get("last_result_hash"):
        if not repo.update_run(user_id, run_id, {
            "status": "no_change", "result_hash": result_hash, "stats": {"rows": len(rows)},
            "finished_at": _now().isoformat(),
        }, expect_status="running"):
            return "lost"           # the reaper failed it meanwhile: keep its failure count
        if int(schedule.get("consecutive_failures") or 0):
            _reset_failures(schedule)
        return "no_change"

    # 9. Deliver: chat (always), then bell and email when there are rows.
    content, blocks = recipes.message_for(recipe, payload)
    try:
        message_id = _post_chat(run, schedule, content, blocks)
    except Exception:  # noqa: BLE001 - nothing reached the person
        logger.exception("assistant scheduler: chat delivery failed for run %s", run_id)
        _fail_run(run, "chat_failed", expect="running")
        return "failed"
    if message_id is None:
        # The chat claim was refused: the reaper failed this run meanwhile.
        # Nothing reached the person, so no bell, email, hash or reset.
        return "lost"
    delivery = str(schedule.get("delivery") or "chat")
    rang = sent = False
    email_error: str | None = None
    if rows and delivery in DELIVERY_BELL:
        rang = _ring(run, schedule, payload)
    if rows and delivery in DELIVERY_EMAIL:
        sent, email_error = _email(run, schedule, payload, check.human, now)
    finished: dict[str, Any] = {
        "status": "delivered", "result_hash": result_hash, "message_id": message_id,
        "stats": {"rows": len(rows)}, "finished_at": _now().isoformat(),
    }
    if email_error:
        finished["error_kind"] = email_error
    if not repo.update_run(user_id, run_id, finished, expect_status="running"):
        # Reaped mid-delivery: the failure count and last_result_hash stay.
        return "lost"
    patch: dict[str, Any] = {"last_delivered_at": _now().isoformat()}
    if not manual:
        patch["last_result_hash"] = result_hash
    _reset_failures(schedule, patch, notified=rang or sent)
    return "delivered"


# ── Step 4: the reaper ───────────────────────────────────────────────────────


def reap(now: datetime | None = None) -> int:
    """Fail runs a worker took and never finished (abandoned), and Run now
    rows nobody took (not_picked_up), each fenced on its status, then
    count the failure (3.4.2 step 4). Returns how many were failed."""
    from app.assistant import repo

    minutes = int(get_settings().assistant_schedule_stale_run_minutes)
    failed = 0
    for run in repo.stale_runs(None, older_than_seconds=minutes * 60, limit=REAP_LIMIT):
        try:
            kind = "not_picked_up" if run.get("claimed_by") is None else "abandoned"
            if _fail_run(run, kind, expect=str(run.get("status") or "claimed")):
                failed += 1
        except Exception:  # noqa: BLE001 - one bad row never stops the pass
            logger.exception("assistant scheduler: reap of %s failed", run.get("id"))
    return failed


# ── Step 5: the resume pass ──────────────────────────────────────────────────


def _resume(row: dict, now: datetime) -> bool:
    """paused -> active with both slots recomputed from now, fenced on the
    status and reason read. A cap refusal leaves the row paused; a rule
    with no future run moves to paused no_future_run."""
    from app.assistant import repo, rrules

    user_id, schedule_id = str(row["user_id"]), str(row["id"])
    try:
        runs = rrules.next_runs(str(row["rrule"]), _dtstart(row), now, _holidays(user_id), n=2)
    except rrules.CadenceError:
        _pause(row, "no_future_run", bell_text=BELL_TEXT_NO_FUTURE_RUN, expect_status=str(row.get("status")))
        return False
    try:
        moved = repo.update_schedule(user_id, schedule_id, {
            "status": "active", "paused_reason": None, "paused_at": None,
            "next_run_at": runs[0].isoformat(),
            "after_next_run_at": runs[1].isoformat() if len(runs) > 1 else None,
        }, expect_status=str(row.get("status")), expect={"paused_reason": row.get("paused_reason")})
    except repo.ScheduleRefused as exc:
        logger.info("assistant scheduler: resume of %s refused (%s)", schedule_id, exc.code)
        return False
    if moved is None:
        return False
    _bell(moved, BELL_RESUMED, BELL_TEXT_RESUMED.format(title=_title(moved)))
    return True


def resume_pass(now: datetime | None = None) -> int:
    """Restart the rows that paused for a reason that has since cleared
    (3.4.2 step 5): paused_feature_off once the sub-app is on again for the
    owner's role; paused mfa_reset or no_delegation once the owner check
    passes and the live delegation was granted after the pause. Returns how
    many resumed."""
    from app.assistant import access, repo
    from app.core import features
    from app.core.roles import INTERNAL_ROLES, Role

    now = _now(now)
    resumed = 0
    for row in repo.paused_to_check(None, limit=RESUME_LIMIT):
        user_id = str(row["user_id"])
        try:
            if row.get("status") == "paused_feature_off":
                human = repo.human_profile(user_id) or {}
                try:
                    role = Role(human.get("role"))
                except ValueError:
                    continue
                if not human.get("is_active") or role not in INTERNAL_ROLES:
                    continue
                if not features.enabled_map(role).get(str(row.get("sub_app") or "bidding"), False):
                    continue
            else:
                if access.owner_check(user_id, require_writer=False).problem is not None:
                    continue
                grant = repo.live_delegation(user_id)
                granted, paused = _ts((grant or {}).get("created_at")), _ts(row.get("paused_at"))
                if granted is None or (paused is not None and granted <= paused):
                    continue
            if _resume(row, now):
                resumed += 1
        except Exception:  # noqa: BLE001 - one bad row never stops the pass
            logger.exception("assistant scheduler: resume check of %s failed", row.get("id"))
    return resumed


# ── The poll ─────────────────────────────────────────────────────────────────


def poll_once(now: datetime | None = None) -> dict:
    """One bounded pass, every step best effort and logged: refill, claim,
    run_claimed per claimed row, reap, the resume pass (at most every 10
    minutes per process), then assistant_maintenance.tick when
    settings.assistant_maintenance_enabled. Returns the step counts."""
    global _last_resume
    from app.assistant import repo

    now = _now(now)
    settings = get_settings()
    counts = {"refilled": 0, "claimed": 0, "skipped": 0, "delivered": 0, "failed": 0, "reaped": 0, "resumed": 0}
    try:
        counts["refilled"] = refill(now)
    except Exception:  # noqa: BLE001 - the claim still runs
        logger.exception("assistant scheduler: refill failed")

    try:
        runs = repo.claim_due_schedules(
            None, worker=WORKER_ID, limit=int(settings.assistant_schedule_claim_batch),
            catchup_seconds=int(settings.assistant_schedule_catchup_hours) * 3600,
        )
    except Exception:  # noqa: BLE001 - nothing claimed this poll
        logger.exception("assistant scheduler: claim failed")
        runs = []
    for run in runs:
        if run.get("status") != "claimed":
            counts["skipped"] += 1
            logger.info("assistant scheduler: run %s of %s recorded %s", run.get("id"), run.get("schedule_id"),
                        run.get("error_kind"))
            continue
        counts["claimed"] += 1
        try:
            outcome = run_claimed(run, now)
        except Exception:  # noqa: BLE001 - one exception fails that run only
            logger.exception("assistant scheduler: run %s crashed", run.get("id"))
            try:
                _fail_run(run, "crashed")
            except Exception:  # noqa: BLE001 - the reaper fails it later
                logger.exception("assistant scheduler: run %s could not be failed", run.get("id"))
            outcome = "failed"
        if outcome in ("delivered", "failed"):
            counts[outcome] += 1

    try:
        counts["reaped"] = reap(now)
    except Exception:  # noqa: BLE001
        logger.exception("assistant scheduler: reap failed")

    tick = time.monotonic()
    if _last_resume is None or tick - _last_resume >= RESUME_EVERY_SECONDS:
        _last_resume = tick
        try:
            counts["resumed"] = resume_pass(now)
        except Exception:  # noqa: BLE001
            logger.exception("assistant scheduler: resume pass failed")

    if settings.assistant_maintenance_enabled:
        try:
            from app.services import assistant_maintenance

            assistant_maintenance.tick(now, worker=WORKER_ID)
        except Exception:  # noqa: BLE001
            logger.exception("assistant scheduler: maintenance tick failed")
    return counts


async def polling_loop() -> None:
    """while True: await asyncio.to_thread(poll_once), log any exception,
    then sleep settings.assistant_scheduler_poll_seconds (the due_digest
    shape). Cancellation stops the sleep; a poll already in its thread
    finishes."""
    interval = get_settings().assistant_scheduler_poll_seconds
    while True:
        try:
            await asyncio.to_thread(poll_once)
        except Exception:  # noqa: BLE001 - the loop must survive any poll failure
            logger.exception("Assistant scheduler poll failed")
        await asyncio.sleep(interval)

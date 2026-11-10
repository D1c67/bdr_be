"""Browser task results and cards back into the chat (docs/ASSISTANT_BROWSER_V2_PLAN.md 4.4, 4.5).

The server, never the model, writes every browser message in a chat:

- Cards (`post_card`): site access, credential, link open, start, task. The
  admission service (WI-5) calls it with the request row already saved.
- Outbox events (`drain_outbox`): SQL writes one assistant_browser_chat_outbox
  row in the same transaction as each event (job terminal, help open, hold
  open, hold expiry), including the transitions with no Python caller (a
  worker hello after a crash, the v2 sweep, a credential replace or a site
  revoke cancelling a job). The drain runs from run_browser_tasks every 60 s
  and from `kick()` right after a Python transition; it posts one message per
  event and acks the rows.

Every message has a deterministic id, uuid5(job or admission id, event key),
so a retried drain or card never posts twice. `content` holds fixed
sentences only; untrusted page text (the result, facts, action labels) lives
only in the typed block. A result message is server_data and tainted when it
carries page text, so later turns read it as a user-role data block
(history_text) and the conversation is tainted.

After a terminal event the drain may start one automatic follow-up turn
(owner answer 1, plan 4.5): a pending assistant message with origin
browser_result and one assistant_turn job, at most one per user message
chain, skipped while a turn is in flight or when the user wrote after the
task started. That turn runs in the turn loop with a text-only toolset; this
module only enqueues it and imports no model code (it is on the scan list of
tests/test_assistant_no_model_unattended.py).

Hold, code, credential, start and site events also send the owner a Teams
alert through teams_alerts (called, never edited), respecting the person's
Teams switch. Plain `def` throughout (sync Supabase SDK).
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from app.assistant import repo

logger = logging.getLogger(__name__)

FOLLOW_UP_ORIGIN = "browser_result"
CARRIED_ORIGIN = "user_carried"
# app/assistant/jobs.JOB_TYPE, not imported: that module imports the turn loop.
TURN_JOB_TYPE = "assistant_turn"
TURN_PRIORITY = 50
FOLLOW_UP_EVENT = "follow_up"
TASK_EVENT = "task"
TERMINAL_EVENT = "terminal"

DRAIN_LIMIT = 50
# A row that keeps failing is acked (and logged) after this many claims, so
# one bad event never blocks the drain for good.
MAX_ATTEMPTS = 8
RECENT_MESSAGES = 50

RESULT_TEXT_MAX = 20_000
FACTS_MAX = 50
FACT_TEXT_MAX = 2_000
URL_MAX = 2_048
FILES_MAX = 20
ACTIONS_MAX = 20
LABEL_MAX = 200
CARRY_MAX_CHARS = 8_000

TERMINAL_STATUSES = ("succeeded", "failed", "canceled", "expired")
OPEN_CARD_STATUSES = ("requested", "open", "waiting_help")
CODE_REASONS = ("mfa", "mfa_step_up", "push")
CREDENTIAL_REASONS = ("needs_credential", "login_failed")

CARD_TEXT = {
    "browser_task": "I started a browser task on the office PC. Its result will appear in this chat.",
    "browser_task_start": ("This browser task needs your go-ahead before it starts. Review the card below; "
                           "nothing runs until you click Start."),
    "browser_site_access": "The browser task needs your permission to use a website. Review the card below.",
    "browser_credential_request": ("The browser task needs a sign-in for a website. Use the card below. "
                                   "Never type a password in the chat."),
    "browser_link_open": "The browser task wants to open a link that came from an email. Review the card below.",
    "browser_final_action": ("The browser task is about to send or submit something on a website. "
                             "Approve or decline it in the card below."),
    "browser_code_request": "The browser task needs a sign-in code. Enter it in the card below, not in the chat.",
    "browser_live_signin": "This website needs you to sign in yourself once. Use the card below.",
}
HELP_TEXT = "The browser task needs your help. Open it from the card below to answer."
CARD_TYPES = frozenset(CARD_TEXT)
ALERT_TYPES = frozenset(CARD_TYPES - {"browser_task"})

RESULT_TEXT = {
    "succeeded": "The browser task finished. Its result is below.",
    "failed": "The browser task stopped before it finished.",
    "canceled": "The browser task was cancelled.",
    "expired": "The browser task expired before the office PC could run it.",
}
REASON_TEXT = {
    "worker_restarted": "The office PC restarted while it was running.",
    "execution_interrupted": "The office PC lost the task while it was running.",
    "interrupted_repeatedly": "The office PC lost the task too many times.",
    "turn_uncertain": "The office PC lost the task while it was running.",
    "interrupted_after_action": ("The office PC stopped after the task had already sent something on the site, "
                                 "so it was not started again. Check the site before you ask again."),
    "waiting_timeout": "Nobody answered its question in time.",
    "offline_expired": "No office PC picked it up in time.",
    "credential_changed": "A saved sign-in it was using changed.",
    "credential_revoked": "A saved sign-in it was using was removed.",
    "scope_revoked": "The approval for a website it was using was revoked.",
    "lease_lost": "Its permissions changed.",
    "cost_cap": "It reached its spending limit.",
}
ACTIONS_TEXT_ONE = "It had already sent 1 approved action on the site; it is listed below."
ACTIONS_TEXT_MANY = "It had already sent {n} approved actions on the site; they are listed below."

ALERT_TEXT = {
    "browser_task_start": "Your browser task is waiting for your go-ahead.",
    "browser_site_access": "Your browser task needs permission to use a website.",
    "browser_credential_request": "Your browser task needs a sign-in for a website.",
    "browser_link_open": "Your browser task wants to open a link from an email.",
    "browser_final_action": "Your browser task is waiting for your approval before it sends something.",
    "browser_code_request": "Your browser task needs a sign-in code.",
    "browser_live_signin": "Your browser task needs you to sign in yourself.",
    "help": "Your browser task needs your help.",
}
ALERT_HEADING = "BDR Assistant"
ALERT_CTA = "Open the chat"
# What the turn loop reads before the result data of a history row.
HISTORY_RESULT_LABEL = "Browser task result:"
HISTORY_FACTS_LABEL = "Facts (each with its source page):"
CARRY_HEADER = "Carried over from a browser task result:"


class CarryRefused(ValueError):
    """POST /assistant/browser-results/{id}/carry refusals: not_found,
    no_facts."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ── Ids ──────────────────────────────────────────────────────────────────────


def message_id(scope_id: str, event_key: str) -> uuid.UUID:
    """Deterministic message id so retries never duplicate: uuid5 of the job
    or admission id (the namespace) and the event key."""
    return uuid.uuid5(uuid.UUID(str(scope_id)), str(event_key))


def _mid(scope_id: str, event_key: str) -> str:
    return str(message_id(scope_id, event_key))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, str) and value:
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _uuid_text(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value))) if value else None
    except (ValueError, TypeError, AttributeError):
        return None


def _money(value: Any) -> float:
    try:
        return float(Decimal(str(value or 0)))
    except (InvalidOperation, ValueError):
        return 0.0


# ── Message writes ───────────────────────────────────────────────────────────


def _insert_once(user_id: str, row: dict) -> tuple[dict | None, bool]:
    """Insert a message with its explicit id; (row, True) when this call
    inserted it, (existing row, False) when an earlier attempt had (23505 on
    the primary key). Any other failure raises."""
    existing = repo.get_message(user_id, row["id"])
    if existing is not None:
        return existing, False
    try:
        return repo.insert_message(user_id, row), True
    except Exception:
        existing = repo.get_message(user_id, row["id"])
        if existing is None:
            raise
        return existing, False


def _touch(user_id: str, conversation_id: str, row: dict | None) -> None:
    try:
        repo.update_conversation(user_id, conversation_id, {
            "last_message_at": str((row or {}).get("created_at") or _now().isoformat()),
        })
    except Exception:  # noqa: BLE001 - the sort order is a convenience
        logger.warning("browser chat: conversation touch failed for %s", conversation_id)


def _post(user_id: str, conversation_id: str, mid: str, content: str, block: dict, *,
          tainted: bool = False, server_data: bool = False) -> tuple[str, bool]:
    row = {
        "id": mid, "conversation_id": conversation_id, "role": "assistant", "status": "done",
        "content": content, "blocks": [{"type": "text", "text": content}, block],
        "completed_at": _now().isoformat(),
    }
    if tainted:
        row["tainted"] = True
    if server_data:
        row["server_data"] = True
    inserted, created = _insert_once(user_id, row)
    if created:
        _touch(user_id, conversation_id, inserted)
    return mid, created


def _patch_card(user_id: str, mid: str, patch: dict, *, only_open: bool = True) -> bool:
    """Merge `patch` into the browser block of one card message (its live
    status after the task moved on). Best effort."""
    try:
        row = repo.get_message(user_id, mid)
        if row is None:
            return False
        blocks = row.get("blocks") if isinstance(row.get("blocks"), list) else []
        changed = False
        out: list[Any] = []
        for block in blocks:
            if (isinstance(block, dict) and str(block.get("type") or "").startswith("browser_")
                    and (not only_open or block.get("status") in OPEN_CARD_STATUSES or block.get("status") is None)):
                block = {**block, **patch}
                changed = True
            out.append(block)
        if changed:
            repo.update_message(user_id, mid, {"blocks": out})
        return changed
    except Exception:  # noqa: BLE001 - the card's own route shows the live status anyway
        logger.warning("browser chat: card %s could not be updated", mid)
        return False


# ── Cards (WI-5 admission, and the outbox below) ─────────────────────────────


def post_card(user_id: str, conversation_id: str, block: dict[str, Any], *, event_key: str,
              scope_id: str, alert: bool = True) -> str:
    """Server-built card message (site access, credential, link, start, task,
    and the outbox's final action, code and live sign-in cards) with a fixed
    sentence in content and the typed block. Returns the message id; a retry
    returns the same id without posting again. Raises ValueError for an
    unknown block type or a conversation the user does not own."""
    kind = str((block or {}).get("type") or "")
    if kind not in CARD_TYPES:
        raise ValueError(f"not a browser card block: {kind}")
    if repo.get_conversation(user_id, str(conversation_id)) is None:
        raise ValueError("conversation not found")
    mid, created = _post(user_id, str(conversation_id), _mid(scope_id, event_key), CARD_TEXT[kind], dict(block))
    if created and alert and kind in ALERT_TYPES:
        _alert(user_id, str(conversation_id), kind)
    return mid


# ── Outbox ───────────────────────────────────────────────────────────────────


def drain_outbox(*, limit: int = DRAIN_LIMIT) -> int:
    """Claim, post and ack outbox rows; returns the number posted. A row
    whose post fails stays unacked (the SQL claim hands it out again after
    5 minutes) until MAX_ATTEMPTS, then it is acked and logged."""
    data = repo.browser_outbox_drain(None, limit=limit)
    rows = data.get("rows") if isinstance(data, dict) else None
    done: list[int] = []
    posted = 0
    for row in rows or ():
        if not isinstance(row, dict) or row.get("id") is None:
            continue
        try:
            if post_event(row):
                posted += 1
        except Exception as exc:  # noqa: BLE001 - one bad event never stops the others
            attempts = int(row.get("attempts") or 0)
            logger.warning("browser chat: outbox row %s (%s) failed on attempt %s: %s", row.get("id"),
                           str(row.get("event_key") or "")[:40], attempts, type(exc).__name__)
            if attempts < MAX_ATTEMPTS:
                continue
            logger.error("browser chat: outbox row %s dropped after %s attempts", row.get("id"), attempts)
        done.append(int(row["id"]))
    if done:
        repo.browser_outbox_ack(None, done)
    return posted


def kick() -> None:
    """Drain soon, off the caller's thread (after a Python transition such
    as a hold or help opened by a worker route). Never raises."""
    def _run() -> None:
        try:
            drain_outbox()
        except Exception:  # noqa: BLE001 - the 60 s maintenance drain is the backstop
            logger.warning("browser chat: kicked drain failed", exc_info=True)

    try:
        threading.Thread(target=_run, name="browser-chat-drain", daemon=True).start()
    except Exception:  # noqa: BLE001
        logger.warning("browser chat: kicked drain could not start")


def post_event(row: dict[str, Any]) -> str | None:
    """Post one event's message (and the Teams alert for hold, code, start and
    site events); returns the message id or None when skipped (no
    conversation, the job or row is gone, an event this build does not
    know)."""
    user_id = _uuid_text(row.get("user_id"))
    conversation_id = _uuid_text(row.get("conversation_id"))
    job_id = _uuid_text(row.get("job_id"))
    event_key = str(row.get("event_key") or "")
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    if not user_id or not conversation_id or not job_id:
        return None
    if repo.get_conversation(user_id, conversation_id) is None:
        return None
    job = repo.browser_job(user_id, job_id)
    if job is None or job.get("agent_type") != "user_browser":
        return None
    if event_key == TERMINAL_EVENT:
        # Read failures propagate (the row is retried) until the last
        # attempt, which posts what it can rather than nothing.
        strict = int(row.get("attempts") or 0) < MAX_ATTEMPTS
        return _post_terminal(user_id, conversation_id, job, payload, strict=strict)
    prefix, _, ident = event_key.partition(":")
    ident = _uuid_text(ident) or ""
    if prefix == "hold" and ident:
        return _post_hold(user_id, conversation_id, job, ident, event_key)
    if prefix == "hold_expired" and ident:
        mid = _mid(job_id, f"hold:{ident}")
        _patch_card(user_id, mid, {"status": "expired"})
        return None
    if prefix == "help" and ident:
        return _post_help(user_id, conversation_id, job, ident, event_key, payload)
    logger.info("browser chat: outbox event %s is not handled", event_key[:40])
    return None


# ── Holds and helps ──────────────────────────────────────────────────────────


def _site_label(user_id: str, site_key: Any) -> str | None:
    if not isinstance(site_key, str) or not site_key:
        return None
    try:
        row = repo.browser_site(user_id, site_key)
    except Exception:  # noqa: BLE001 - a label is decoration
        return None
    return str(row.get("label"))[:100] if row and row.get("label") else None


def _start_site(job: dict) -> str | None:
    seed = job.get("input") if isinstance(job.get("input"), dict) else {}
    key = seed.get("start_site_key") or job.get("platform")
    return str(key) if isinstance(key, str) and key else None


def _job_credential(user_id: str, job: dict, credential_id: Any) -> dict | None:
    try:
        rows = repo.browser_job_credentials(user_id, job_id=str(job["id"]))
    except Exception:  # noqa: BLE001
        return None
    wanted = _uuid_text(credential_id)
    for row in rows:
        if wanted and _uuid_text(row.get("credential_id")) == wanted:
            return row
    start = _start_site(job)
    return next((r for r in rows if r.get("site_key") == start), rows[0] if rows else None)


def _post_hold(user_id: str, conversation_id: str, job: dict, hold_id: str, event_key: str) -> str | None:
    hold = repo.browser_hold(user_id, hold_id)
    if hold is None or str(hold.get("job_id")) != str(job["id"]):
        return None
    kind = hold.get("kind")
    status = str(hold.get("status") or "open")
    job_id = str(job["id"])
    if kind == "site":
        request = None
        try:
            request = next((r for r in repo.browser_requests_open(user_id, job_id=job_id, kind="site_access")
                            if r.get("site_key") == hold.get("target_site_key")), None)
        except Exception:  # noqa: BLE001 - the hold card below still works
            request = None
        if request is not None:
            block = {"type": "browser_site_access", "request_id": str(request["id"]),
                     "status": "requested" if status == "open" else status, "hold_id": hold_id}
        else:
            # An approved multi-tenant site has no request row: the hold
            # itself is the decision (approve or decline on the hold route).
            block = {"type": "browser_final_action", "hold_id": hold_id, "job_id": job_id, "status": status,
                     "hold_kind": "site"}
    elif kind == "code":
        block = {"type": "browser_code_request", "job_id": job_id, "help_id": _uuid_text(hold.get("help_id")),
                 "site_label": _site_label(user_id, hold.get("target_site_key") or _start_site(job)),
                 "account_label": None, "status": status, "code_from_email": False}
    elif kind == "live_signin":
        block = {"type": "browser_live_signin", "job_id": job_id, "request_id": None,
                 "site_label": _site_label(user_id, hold.get("target_site_key") or _start_site(job)),
                 "status": status}
    else:
        block = {"type": "browser_final_action", "hold_id": hold_id, "job_id": job_id, "status": status}
    # Alert only for a live hold: one the terminal trigger already aborted
    # (or that expired) can no longer be approved.
    return post_card(user_id, conversation_id, block, event_key=event_key, scope_id=job_id,
                     alert=status == "open")


def _credential_request(user_id: str, conversation_id: str, job: dict, help_row: dict) -> dict | None:
    """The credential card's request row for a job that needs a sign-in
    (reuses the open one for the same site and account)."""
    cred = _job_credential(user_id, job, help_row.get("credential_id"))
    site_key = (cred or {}).get("site_key") or _start_site(job)
    label = (cred or {}).get("account_label") or None
    if not site_key:
        return None
    job_id = str(job["id"])
    for row in repo.browser_requests_open(user_id, job_id=job_id, kind="credential"):
        if row.get("site_key") == site_key and (row.get("account_label") or None) == label:
            return row
    record = {"kind": "credential", "job_id": job_id, "conversation_id": conversation_id, "site_key": site_key,
              "reason_code": "login_rejected" if help_row.get("reason_code") == "login_failed" else "no_saved_login"}
    if label:
        record["account_label"] = label
    result = repo.browser_request_create(user_id, record)
    request = result.get("request") if isinstance(result, dict) and result.get("ok") is True else None
    return request if isinstance(request, dict) and request.get("id") else None


def _post_help(user_id: str, conversation_id: str, job: dict, help_id: str, event_key: str,
               payload: dict) -> str | None:
    help_row = repo.browser_help_state_by_id(user_id, help_id)
    if help_row is None or str(help_row.get("job_id")) != str(job["id"]):
        return None
    if help_row.get("status") != "open":
        return None  # answered or ended before the drain: nothing to ask
    reason = str(help_row.get("reason_code") or payload.get("reason_code") or "")
    job_id = str(job["id"])
    if reason in CODE_REASONS:
        cred = _job_credential(user_id, job, help_row.get("credential_id"))
        block = {"type": "browser_code_request", "job_id": job_id, "help_id": help_id,
                 "site_label": _site_label(user_id, (cred or {}).get("site_key") or _start_site(job)),
                 "account_label": (cred or {}).get("account_label"), "status": "open", "code_from_email": False}
        return post_card(user_id, conversation_id, block, event_key=event_key, scope_id=job_id)
    if reason in CREDENTIAL_REASONS:
        try:
            request = _credential_request(user_id, conversation_id, job, help_row)
        except Exception:  # noqa: BLE001 - fall back to the plain help card
            logger.warning("browser chat: credential request for job %s not created", job_id)
            request = None
        if request is not None:
            block = {"type": "browser_credential_request", "request_id": str(request["id"]),
                     "site_key": request.get("site_key"), "account_label": request.get("account_label"),
                     "status": "requested", "version": None}
            return post_card(user_id, conversation_id, block, event_key=event_key, scope_id=job_id)
    block = _task_block(user_id, job, status="waiting_help")
    mid, created = _post(user_id, conversation_id, _mid(job_id, event_key), HELP_TEXT, block)
    if created:
        _alert(user_id, conversation_id, "help")
    return mid


# ── Terminal: the result message and the follow-up ──────────────────────────


def _task_block(user_id: str, job: dict, *, status: str | None = None, holds_pending: int = 0) -> dict:
    seed = job.get("input") if isinstance(job.get("input"), dict) else {}
    goal = seed.get("goal")
    return {"type": "browser_task", "job_id": str(job["id"]), "status": status or str(job.get("status") or ""),
            "steps": int(job.get("step_count") or 0), "cost_usd": _money(job.get("cost_usd")),
            "holds_pending": holds_pending, "site_label": _site_label(user_id, _start_site(job)),
            "goal": goal[:2000] if isinstance(goal, str) else None}


def _read_result(user_id: str, job_id: str, *, strict: bool = False) -> dict:
    """The job's newest result record (encrypted at rest, owner bound), or
    {} when there is none. A read or decrypt failure raises when `strict`
    (the outbox row is retried, so a transient error never loses the
    result for good); otherwise it answers {}."""
    from app.services import assistant_browser_records as records

    try:
        rows = repo.browser_records(user_id, job_id, kinds=("result",), before=None, limit=1)
        if not rows:
            return {}
        value = records.unpack_record(user_id, job_id, rows[0])
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001 - the last attempt still posts with its status
        logger.warning("browser chat: result record of job %s unavailable", job_id)
        if strict:
            raise
        return {}


def result_parts(content: dict) -> tuple[str | None, list[dict]]:
    """(text, facts) of a result record. The v2 record (runtime.finish)
    keeps the model's `result` and `facts[{text, source_url}]`; older
    shapes use `text` or `summary` and `url`."""
    text = content.get("result") or content.get("text") or content.get("summary")
    facts: list[dict] = []
    for raw in content.get("facts") or ():
        if not isinstance(raw, dict) or not isinstance(raw.get("text"), str) or not raw["text"].strip():
            continue
        url = raw.get("source_url") or raw.get("url")
        url = url if isinstance(url, str) and len(url) <= URL_MAX else None
        facts.append({"id": f"f{len(facts) + 1}", "text": raw["text"][:FACT_TEXT_MAX], "url": url})
        if len(facts) >= FACTS_MAX:
            break
    return (text[:RESULT_TEXT_MAX] if isinstance(text, str) and text.strip() else None), facts


def _files(user_id: str, job_id: str, *, strict: bool = False) -> list[dict]:
    try:
        rows = repo.browser_downloads(user_id, job_id, limit=FILES_MAX)
    except Exception:  # noqa: BLE001
        if strict:
            raise
        return []
    out = []
    for row in rows:
        if not _uuid_text(row.get("id")):
            continue
        status = row.get("status") if row.get("status") in ("stored", "staged", "rejected") else "rejected"
        out.append({"id": str(row["id"]), "filename": (row.get("filename") or None),
                    "size_bytes": int(row.get("size_bytes") or 0), "sniffed_type": row.get("sniffed_type") or "unknown",
                    "status": status, "project_id": _uuid_text(row.get("project_id")),
                    "file_id": _uuid_text(row.get("file_id"))})
    return out[:FILES_MAX]


def _released_actions(user_id: str, job_id: str, *, strict: bool = False) -> list[dict]:
    """Final actions the owner approved that reached the site (consumed
    holds), for a task that stopped after sending something."""
    try:
        rows = repo.browser_holds(user_id, job_id, status=["consumed"], limit=ACTIONS_MAX)
    except Exception:  # noqa: BLE001
        if strict:
            raise
        return []
    return [{"hold_id": str(r["id"]), "method": r.get("method"), "origin": r.get("origin"),
             "label": (r.get("label") or "")[:LABEL_MAX] or None, "decided_at": r.get("decided_at")}
            for r in rows if r.get("id") and r.get("kind") == "final_action"]


def _close_cards(user_id: str, job: dict) -> None:
    """After the job ended: the task card shows the final status, and every
    hold or help card still showing open shows the hold's own status (the
    terminal trigger aborted it)."""
    job_id = str(job["id"])
    _patch_card(user_id, _mid(job_id, TASK_EVENT), {k: v for k, v in _task_block(user_id, job).items()
                                                     if k in ("status", "steps", "cost_usd", "holds_pending")},
                only_open=False)
    try:
        holds = repo.browser_holds(user_id, job_id, limit=50)
    except Exception:  # noqa: BLE001
        holds = []
    for hold in holds:
        status = str(hold.get("status") or "aborted")
        _patch_card(user_id, _mid(job_id, f"hold:{hold['id']}"),
                    {"status": "aborted" if status == "open" else status})
    try:
        help_row = repo.browser_help_state(user_id, job_id)
    except Exception:  # noqa: BLE001
        help_row = None
    if help_row and help_row.get("id"):
        _patch_card(user_id, _mid(job_id, f"help:{help_row['id']}"), {"status": "aborted"})


def _terminal_text(status: str, error_kind: Any, actions: int) -> str:
    parts = [RESULT_TEXT.get(status, RESULT_TEXT["failed"])]
    if status != "succeeded":
        reason = REASON_TEXT.get(str(error_kind or ""))
        if reason:
            parts.append(reason)
    if actions and status != "succeeded":
        parts.append(ACTIONS_TEXT_ONE if actions == 1 else ACTIONS_TEXT_MANY.format(n=actions))
    return " ".join(parts)


def _post_terminal(user_id: str, conversation_id: str, job: dict, payload: dict, *,
                   strict: bool = False) -> str | None:
    job_id = str(job["id"])
    status = str(job.get("status") or payload.get("status") or "failed")
    if status not in TERMINAL_STATUSES:
        status = str(payload.get("status") or "failed")
    error_kind = job.get("error_kind") or payload.get("error_kind")
    # Every read first: with `strict` a failure raises before anything is
    # posted, and the deterministic message id is never written half empty.
    text, facts = result_parts(_read_result(user_id, job_id, strict=strict))
    side_effects = job.get("side_effects_at") or payload.get("side_effects")
    actions = _released_actions(user_id, job_id, strict=strict) if side_effects else []
    files = _files(user_id, job_id, strict=strict)
    _close_cards(user_id, job)
    block = {"type": "browser_result", "job_id": job_id, "status": status, "error_kind": error_kind,
             "text": text, "facts": facts, "files": files, "released_actions": actions}
    content = _terminal_text(status, error_kind, len(actions))
    untrusted = bool(text or facts or actions)
    mid, _created = _post(user_id, conversation_id, _mid(job_id, TERMINAL_EVENT), content, block,
                          tainted=untrusted, server_data=True)
    try:
        maybe_enqueue_follow_up(user_id, conversation_id, job_id)
    except Exception:  # noqa: BLE001 - the result is posted; the follow-up is optional
        logger.warning("browser chat: follow-up for job %s not started", job_id, exc_info=True)
    return mid


def history_text(row: dict) -> str:
    """What a later turn reads for a browser result message (a user-role
    data block, plan 4.5 Replay): the fixed sentence, then the result text
    and the facts with their sources from the block. Other rows: content."""
    content = str(row.get("content") or "").strip()
    blocks = row.get("blocks") if isinstance(row.get("blocks"), list) else []
    block = next((b for b in blocks if isinstance(b, dict) and b.get("type") == "browser_result"), None)
    if block is None:
        return content
    lines = [content]
    if isinstance(block.get("text"), str) and block["text"].strip():
        lines += [HISTORY_RESULT_LABEL, block["text"].strip()]
    facts = [f for f in block.get("facts") or () if isinstance(f, dict) and isinstance(f.get("text"), str)]
    if facts:
        lines.append(HISTORY_FACTS_LABEL)
        for fact in facts[:FACTS_MAX]:
            source = _plain_source(fact.get("url"))
            lines.append(f"- {fact['text']}" + (f" (source: {source})" if source else ""))
    return "\n".join(lines)


def _plain_source(url: Any) -> str | None:
    """A source URL as plain text: host and path, no scheme or query."""
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.hostname:
        return None
    return (parts.hostname + (parts.path or ""))[:300]


def _settings() -> Any:
    from app.core.config import get_settings

    return get_settings()


def _follow_up_allowed(user_id: str) -> bool:
    """The owner can still have a turn run for them (every kill switch, the
    delegation, a writer role) and the v2 browser is on."""
    from app.assistant import access

    settings = _settings()
    if not (getattr(settings, "assistant_browser_enabled", False)
            and getattr(settings, "assistant_browser_v2_enabled", False)):
        return False
    if not getattr(settings, "llm_queue_enabled", True):
        return False
    try:
        return access.owner_check(user_id, require_writer=True).problem is None
    except Exception:  # noqa: BLE001 - an unreadable owner gets no unattended turn
        return False


def maybe_enqueue_follow_up(user_id: str, conversation_id: str, job_id: str) -> str | None:
    """One text-only follow-up turn per user message chain (owner answer 1);
    returns the pending message id or None when skipped: already started,
    a turn in flight, the user wrote after the task started, a follow-up
    already ran in this chain, a mailbox-review or archived conversation, or
    an owner who may no longer have turns. Counts toward the owner's spend
    like any turn (the loop reserves before each model call)."""
    fid = _mid(job_id, FOLLOW_UP_EVENT)
    existing = repo.get_message(user_id, fid)
    if existing is not None:
        return _repair_follow_up(user_id, conversation_id, fid, existing)
    job = repo.browser_job(user_id, job_id)
    conversation = repo.get_conversation(user_id, conversation_id)
    if job is None or conversation is None or str(job.get("assistant_conversation_id") or "") != conversation_id:
        return None
    if (conversation.get("archived_at") or conversation.get("mailbox_tainted_at")
            or (conversation.get("mode") or "normal") != "normal"):
        return None
    recent = repo.browser_chat_recent_messages(user_id, conversation_id, limit=RECENT_MESSAGES)
    if any(m.get("status") in ("pending", "running") for m in recent):
        return None
    last_user = next((m for m in recent if m.get("role") == "user"), None)
    started, last_user_at = _ts(job.get("created_at")), _ts((last_user or {}).get("created_at"))
    if last_user is None or started is None or last_user_at is None or last_user_at > started:
        return None
    if any(m.get("origin") == FOLLOW_UP_ORIGIN and (_ts(m.get("created_at")) or last_user_at) >= last_user_at
           for m in recent):
        return None
    if not _follow_up_allowed(user_id):
        return None
    try:
        repo.insert_message(user_id, {
            "id": fid, "conversation_id": conversation_id, "role": "assistant", "content": "",
            "status": "pending", "origin": FOLLOW_UP_ORIGIN,
        })
    except Exception:  # noqa: BLE001 - a turn started meanwhile (one active turn per conversation)
        return None
    job_row_id = _enqueue_turn(user_id, conversation_id, fid)
    if job_row_id is None:
        try:
            repo.delete_message(user_id, fid)
        except Exception:  # noqa: BLE001 - never leave a jobless pending row behind
            logger.warning("browser chat: pending follow-up %s could not be withdrawn", fid)
            _fail_pending(user_id, fid)
        return None
    _link_job(user_id, fid, job_row_id)
    return fid


def _link_job(user_id: str, fid: str, job_row_id: str) -> None:
    if _uuid_text(job_row_id):
        try:
            repo.update_message(user_id, fid, {"llm_job_id": job_row_id})
        except Exception:  # noqa: BLE001 - the link is informational
            logger.info("browser chat: follow-up %s job link not stored", fid)


def _fail_pending(user_id: str, fid: str) -> None:
    """Mark a jobless pending follow-up failed (fenced on pending), so the
    conversation is never stuck behind it (repo.active_turn)."""
    try:
        repo.update_message(user_id, fid, {"status": "failed", "content": "", "error_kind": "interrupted",
                                           "completed_at": _now().isoformat()}, expect_status="pending")
    except Exception:  # noqa: BLE001
        logger.error("browser chat: pending follow-up %s could not be marked failed", fid)


def _turn_job_for(conversation_id: str, fid: str) -> bool:
    """True when a turn job is active in the conversation or a job already
    carried this follow-up (the queue marks its message). A read error
    answers True: never risk a second follow-up."""
    from app.services import llm_queue

    try:
        if llm_queue.active_job(TURN_JOB_TYPE, conversation_id) is not None:
            return True
        latest = llm_queue.latest_job(TURN_JOB_TYPE, conversation_id)
    except Exception:  # noqa: BLE001
        logger.warning("browser chat: follow-up %s job read failed", fid)
        return True
    payload = (latest or {}).get("payload")
    return isinstance(payload, dict) and str(payload.get("message_id") or "") == fid


def _repair_follow_up(user_id: str, conversation_id: str, fid: str, existing: dict) -> str | None:
    """A redelivered terminal row whose follow-up row already exists. A
    pending row with no job (the process died between the insert and the
    enqueue, or the withdraw failed) is enqueued again, or marked failed
    when the queue refuses; anything else is left alone. The outbox claim
    (5 minutes) keeps two drains off the same row, so this never races the
    first attempt."""
    if existing.get("status") != "pending" or existing.get("llm_job_id"):
        return None
    if _turn_job_for(conversation_id, fid):
        return None
    job_row_id = _enqueue_turn(user_id, conversation_id, fid)
    if job_row_id is None:
        _fail_pending(user_id, fid)
        return None
    _link_job(user_id, fid, job_row_id)
    return fid


def _enqueue_turn(user_id: str, conversation_id: str, message_id_: str) -> str | None:
    """One assistant_turn job (max_attempts 1) for the follow-up: its id
    ("" when the queue returned none), or None when a job is already active
    for the conversation or the queue refused."""
    from app.services import llm_queue

    try:
        job = llm_queue.enqueue(
            TURN_JOB_TYPE, target_id=conversation_id,
            payload={"message_id": message_id_, "user_id": user_id, "conversation_id": conversation_id,
                     "origin": FOLLOW_UP_ORIGIN},
            created_by=user_id, priority=TURN_PRIORITY, raise_on_active=True, max_attempts_override=1,
        )
    except Exception as exc:  # noqa: BLE001 - JobAlreadyActive or the queue is down
        logger.info("browser chat: follow-up turn not enqueued (%s)", type(exc).__name__)
        return None
    return str(job.get("id") or "") if isinstance(job, dict) else ""


# ── "Continue in a new chat" (plan 4.6 item 3) ───────────────────────────────


def carry_result(user_id: str, source_message_id: str, fact_ids: list[str]) -> dict:
    """Start a new conversation whose first user message is the facts the
    user ticked on a browser_result block, origin user_carried. The user's
    click is the same act as pasting, so the new conversation starts
    untainted. No turn is started; the user asks next. Returns
    {conversation_id, message_id}. Raises CarryRefused."""
    mid = _uuid_text(source_message_id)
    row = repo.get_message(user_id, mid) if mid else None
    if row is None or row.get("role") != "assistant":
        raise CarryRefused("not_found")
    blocks = row.get("blocks") if isinstance(row.get("blocks"), list) else []
    block = next((b for b in blocks if isinstance(b, dict) and b.get("type") == "browser_result"), None)
    source = repo.get_conversation(user_id, str(row.get("conversation_id") or ""))
    if block is None or source is None:
        raise CarryRefused("not_found")
    wanted = {str(f) for f in fact_ids or ()}
    chosen = [f for f in block.get("facts") or ()
              if isinstance(f, dict) and str(f.get("id")) in wanted and isinstance(f.get("text"), str)]
    if not chosen:
        raise CarryRefused("no_facts")
    lines = [CARRY_HEADER]
    for fact in chosen:
        source_text = _plain_source(fact.get("url"))
        lines.append(f"- {fact['text'].strip()}" + (f" (source: {source_text})" if source_text else ""))
    text = "\n".join(lines)
    if len(text) > CARRY_MAX_CHARS:
        text = text[: CARRY_MAX_CHARS - 6].rstrip() + " [cut]"
    surface = source.get("surface") if source.get("surface") in ("panel", "page") else "panel"
    conversation = repo.create_conversation(user_id, surface=surface, project_id=source.get("project_id"))
    conversation_id = str(conversation["id"])
    message = repo.insert_message(user_id, {
        "conversation_id": conversation_id, "role": "user", "content": text, "status": "done",
        "origin": CARRIED_ORIGIN,
    })
    _touch(user_id, conversation_id, message)
    return {"conversation_id": conversation_id, "message_id": str(message.get("id"))}


# ── Teams alerts ─────────────────────────────────────────────────────────────


def _alert(user_id: str, conversation_id: str, kind: str) -> None:
    """A Teams alert to the owner for a new card (hold, code, credential,
    start, site, link, help). Fire-and-forget; never raises. Fixed text only:
    no page text, site address or goal reaches Teams."""
    try:
        from app.services import teams_alerts

        if not teams_alerts.is_configured():
            return
        text = ALERT_TEXT.get(kind, ALERT_TEXT["help"])
        threading.Thread(target=_deliver_alert, args=(user_id, conversation_id, text),
                         name="browser-chat-teams", daemon=True).start()
    except Exception:  # noqa: BLE001 - an alert never blocks the card
        logger.warning("browser chat: Teams alert not queued")


def _deliver_alert(user_id: str, conversation_id: str, text: str) -> None:
    try:
        from app.core.roles import Role
        from app.core.supabase_client import get_supabase
        from app.services import graph_email, teams_alerts

        if graph_email._active_test_session(get_supabase()):
            return
        profile = repo.human_profile(user_id)
        if (not profile or not profile.get("is_active") or not profile.get("email")
                or profile.get("role") == Role.ESTIMATOR.value):
            return
        state = teams_alerts.get_state(user_id) or {}
        if state.get("enabled") is False:  # the person's Teams switch
            return
        base = str(getattr(_settings(), "frontend_url", "") or "").rstrip("/")
        card = teams_alerts.build_card(heading=ALERT_HEADING, message=text, project_label=None,
                                       cta_label=ALERT_CTA,
                                       cta_url=f"{base}/assistant?tab=chat&conversation={conversation_id}")
        teams_alerts._deliver(user_id, profile["email"], card, text, state)
    except Exception as exc:  # noqa: BLE001 - best effort, like every Teams alert
        logger.warning("browser chat: Teams alert not delivered (%s)", type(exc).__name__)

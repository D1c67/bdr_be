"""Personal browser: phase 6 metadata reads and the v2 task admission.

Phase 6 (retired `user_browser_read`): vault status, credential metadata,
task metadata and the MFA-reset revoke stay here for their callers.

Browser v2 (docs/ASSISTANT_BROWSER_V2_PLAN.md 4.3, 5.3, 5.5, 12, WI-5):
`admit` turns a `browser_task_start` call into an admission record
(assistant_browser_admission_create), posts the next server-built card
when a grant, a login, an email link approval or a start click is missing,
and enqueues the job (assistant_browser_task_enqueue_v2: first 1 USD
reserve step, one expiry for job and reservation) when nothing is. The
model never words a card, never grants, never fills a login and never
names a cost. Credential save and revoke live in
assistant_browser_credentials (WI-4).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from app.assistant import repo
from app.core.config import get_settings
from app.services import assistant_browser_sites as sites
from app.services import assistant_browser_vault as vault

EXECUTION_READY = True
UNAVAILABLE_REASON = "Personal browser execution is unavailable. Check the browser configuration."
METADATA_FIELDS = ("id", "service", "label", "username", "origins", "status", "version", "fingerprint", "updated_at")

logger = logging.getLogger(__name__)


def configured() -> bool:
    try:
        vault._key()
        return True
    except vault.VaultUnavailable:
        return False


def site_status() -> dict:
    enabled = bool(getattr(get_settings(), "assistant_browser_enabled", False))
    return {"enabled": enabled, "execution_ready": EXECUTION_READY, "configured": configured(),
            "items": [{"service": spec.key, "label": spec.label, "origins": list(spec.origins),
                       "available": spec.available,
                       "reason": (None if enabled and EXECUTION_READY else UNAVAILABLE_REASON)
                                 if spec.available else "This site is not supported yet."}
                      for spec in sites.SITES.values()]}


def metadata(row: dict) -> dict:
    return {key: row.get(key) for key in METADATA_FIELDS}


def credentials(user_id: str) -> dict:
    return {"items": [metadata(row) for row in repo.browser_credentials(user_id)]}


def revoke_before_mfa_reset(user_id: str) -> None:
    """Fence stored authority before factors are removed, even with flags off.

    Deployments without the phase 6 table cannot have personal credentials.
    Every other read/revoke failure propagates and blocks the reset.
    """
    try:
        rows = repo.browser_credentials(user_id)
    except Exception as exc:
        if str(getattr(exc, "code", "")) in {"42P01", "PGRST205"}:
            return
        raise
    if any(row.get("status") == "active" for row in rows):
        repo.browser_revoke_owner(user_id)
        if any(row.get("status") == "active" for row in repo.browser_credentials(user_id)):
            raise ValueError("Personal browser revocation failed.")


def task_metadata(row: dict) -> dict:
    # No checkpoint, tool output, storage locator or credential identifiers.
    seed = row.get("input") if isinstance(row.get("input"), dict) else {}
    def text(value, limit=100):
        return value[:limit] if isinstance(value, str) else None
    return {"id": text(row.get("id")), "service": text(seed.get("service")), "url": text(seed.get("url"), 2048),
            "objective": text(seed.get("objective") or seed.get("task"), 2000), "status": text(row.get("status")),
            "outcome": text(row.get("outcome")), "error_kind": text(row.get("error_kind")),
            "created_at": text(row.get("created_at")), "expires_at": text(row.get("expires_at")),
            "cost_usd": row.get("cost_usd", 0), "result": None, "help_id": None}


def tasks(user_id: str) -> dict:
    return {"items": [task_metadata(row) for row in repo.browser_jobs(user_id)]}


def task(user_id: str, job_id: str) -> dict | None:
    row = repo.browser_job(user_id, job_id)
    if not row:
        return None
    from app.services import assistant_browser_records
    result = task_metadata(row)
    result["result"] = assistant_browser_records.latest_result(user_id, job_id)
    help_state = repo.browser_help_state(user_id, job_id)
    result["help_id"] = help_state.get("id") if help_state else None
    return result




# ── Browser v2 admission (docs/ASSISTANT_BROWSER_V2_PLAN.md 4.3, 5.3, 5.5, 12) ─
#
# `admit` is the only job-creation path for the model. `continue_admission`
# runs after a human click (grant, credential save, link approval, start
# card) through WI-4's routes. Every refusal is an AdmissionRefused with a
# stable code and a fixed sentence (never the caller's input).

TYPE_KEY = "user_browser"
TASK_FIELDS = frozenset({"site_key", "start_url", "goal", "project_id", "account_label"})
SITE_ACCESS_FIELDS = frozenset({"site_key", "url", "reason_code"})
CREDENTIAL_FIELDS = frozenset({"site_key", "account_label", "reason_code"})
SITE_ACCESS_REASONS = ("task_needs_site", "user_asked", "link_from_email", "link_from_page")
CREDENTIAL_REASONS = ("no_saved_login", "login_rejected", "user_asked")
ACCOUNT_LABEL_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,40}$")
SITE_KEY_RE = re.compile(r"^[a-z0-9_.:-]{2,80}$")
GOAL_MAX = 2000
WAITING_STATUSES = ("waiting_grant", "waiting_credential", "waiting_link", "waiting_start", "ready")
ACTIVE_JOB_STATUSES = ("queued", "running", "waiting_help", "applying")
TERMINAL_JOB_STATUSES = ("succeeded", "failed", "canceled", "expired")
# One card at a time, in this order (the status priority of
# assistant_browser_admission_status, 0192 A1.20).
CARD_ORDER = ("site_access", "credential", "live_signin", "link_open", "task_start")
CARD_BLOCK_TYPES = {
    "site_access": "browser_site_access",
    "credential": "browser_credential_request",
    "link_open": "browser_link_open",
    "task_start": "browser_task_start",
}
RESULT_TEXT_MAX = 10_000
FACTS_MAX = 50

# What the model is told (fixed sentences; the card itself is server-built).
STATUS_MESSAGES = {
    "waiting_grant": "A site approval card is in the chat. The task starts after the user approves the site.",
    "waiting_credential": ("A sign-in card is in the chat. The task starts after the user saves a login for this "
                           "site. Never ask for a password or a code in chat."),
    "waiting_link": ("This link came from an email, so a link card is in the chat. The task starts after the "
                     "user approves the link."),
    "waiting_start": "A start card is in the chat. The task starts after the user clicks Start.",
    "ready": "The task is ready to start. Check its status again to see whether it started.",
    "enqueued": "The task is queued on the office PC. The result will appear in this chat.",
    "declined": "The user declined this browser task.",
    "expired": "This browser request expired. Ask again if it is still needed.",
}
SSO_MESSAGE = ("This site uses Microsoft or Google sign-in. Signing in yourself through a live view is not "
               "available yet.")


class AdmissionRefused(ValueError):
    """A browser request refused. `code` is stable; the message is fixed."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or REFUSALS.get(code, REFUSALS["invalid"]))
        self.code = code if code in REFUSALS else "invalid"
        self.message = str(self)


REFUSALS = {
    "unavailable": "The browser is not available right now.",
    "ineligible": "The browser is not available for this account.",
    "invalid": "The browser request is not valid.",
    "conversation": "Start a normal conversation for browser work.",
    "unknown_site": "That site is not in the site list. Give the page address instead.",
    "site_disabled": "That site is turned off for the browser.",
    "site_mismatch": "The page address is not on that site.",
    "url_refused": "The browser cannot open that page address.",
    "identity_provider": ("Sign-in pages of Microsoft, Google and other sign-in providers are not opened "
                          "directly. Give the address of the site itself."),
    "project_invalid": "That project does not exist, or you cannot add files to it.",
    "project_not_bound": ("This chat holds outside content, so the project must be one the user opened or named. "
                          "Ask the user to open the project in BDR, or start the task without a project."),
    "sso_required": SSO_MESSAGE,
    "no_login": "This site does not use a sign-in.",
    "concurrency_cap": "Too many browser tasks are queued or running. Wait for one to finish.",
    "user_cap": "Your daily assistant budget is used up.",
    "global_cap": "The company's daily assistant budget is used up.",
    "admission_changed": "The browser request changed. Ask again.",
    "not_found": "No such browser task.",
    "link_in_goal": "Pass the link as start_url.",
}
# RPC codes that mean the platform, not the request, is the problem.
_UNAVAILABLE_CODES = frozenset({"runtime_unavailable", "type_unavailable"})
_CHANGED_CODES = frozenset({"admission_changed", "conversation_changed", "authority_changed", "invalid_cost",
                            "request_changed"})


def _refusal(result: dict | None) -> AdmissionRefused:
    code = str((result or {}).get("code") or "invalid")
    if code in _UNAVAILABLE_CODES:
        return AdmissionRefused("unavailable")
    if code == "conversation_changed":
        return AdmissionRefused("conversation")
    if code in _CHANGED_CODES:
        return AdmissionRefused("admission_changed")
    return AdmissionRefused(code if code in REFUSALS else "invalid")


# ── Gates ────────────────────────────────────────────────────────────────────


def v2_enabled() -> bool:
    """ASSISTANT_BROWSER_ENABLED and ASSISTANT_BROWSER_V2_ENABLED (config
    requires the first for the second)."""
    s = get_settings()
    return bool(getattr(s, "assistant_browser_enabled", False) and getattr(s, "assistant_browser_v2_enabled", False))


def _price_ok(value: Any) -> bool:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return False
    return number.is_finite() and number > 0


def _require_flags() -> None:
    if not v2_enabled():
        raise AdmissionRefused("unavailable")


def _require_owner(user_id: str) -> None:
    from app.assistant import access

    _require_flags()
    if access.owner_check(user_id, require_writer=True).problem:
        raise AdmissionRefused("ineligible")


def _require_ready(user_id: str) -> tuple[dict, dict]:
    """Flags, owner, and the `user_browser` type row and settings usable
    (the enqueue RPC rechecks all of it under the owner lock)."""
    _require_owner(user_id)
    kind, settings = repo.browser_platform_config_v2(user_id)
    if (kind.get("principal_kind") != "user" or kind.get("enabled") is not True
            or settings.get("enabled") is not True or kind.get("provider") != "openai"
            or not kind.get("model") or not kind.get("model_probe_ok_at")
            or not _price_ok(kind.get("price_in_per_mtok")) or not _price_ok(kind.get("price_out_per_mtok"))):
        raise AdmissionRefused("unavailable")
    return kind, settings


def _conversation(user_id: str, conversation_id: str | None) -> dict:
    conversation = repo.get_conversation(user_id, conversation_id) if conversation_id else None
    if not conversation or conversation.get("mode") != "normal" or conversation.get("mailbox_tainted_at"):
        raise AdmissionRefused("conversation")
    return conversation


def turn_flags(user_id: str, turn: dict) -> tuple[bool, bool]:
    """(tainted, fresh_user_message) for a turn context (repo.turn_context).

    Tainted (4.3): the turn's own taint, the conversation's, or any tainted
    tool result in the conversation. Fresh: the turn's answer message was
    started by the user's own message (origin null, a user message before
    it); the browser_result follow-up turn (origin browser_result) is not.
    Any failed read answers the cautious way (tainted, not fresh), which
    only adds the start card."""
    conversation_id = turn.get("conversation_id")
    tainted = bool(turn.get("tainted") or turn.get("mailbox_tainted"))
    if not tainted:
        try:
            tainted = bool(repo.tainted_results(user_id, conversation_id, limit=1))
        except Exception:  # noqa: BLE001 - fail closed: the start card applies
            logger.warning("browser admission: tainted result read failed")
            tainted = True
    fresh = False
    message_id = turn.get("message_id")
    if message_id and turn.get("user_texts"):
        try:
            message = repo.browser_message_turn(user_id, str(message_id))
        except Exception:  # noqa: BLE001 - fail closed: the start card applies
            logger.warning("browser admission: turn message read failed")
            message = None
        fresh = bool(message and message.get("role") == "assistant" and not message.get("origin"))
    return tainted, fresh


# ── Argument checks ──────────────────────────────────────────────────────────


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _uuid(value: Any) -> str | None:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        return None


def _site_key(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not SITE_KEY_RE.match(value):
        raise AdmissionRefused("invalid")
    return value


def _account_label(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not ACCOUNT_LABEL_RE.match(value):
        raise AdmissionRefused("invalid")
    return value


def _args(args: Any, allowed: frozenset[str]) -> dict:
    if not isinstance(args, dict) or set(args) - allowed:
        raise AdmissionRefused("invalid")
    return {k: v for k, v in args.items() if v is not None}


def _normalized_url(value: Any) -> str:
    if not isinstance(value, str):
        raise AdmissionRefused("invalid")
    try:
        parsed = sites.parse_url(value.strip(), top_level=True)
    except sites.UrlRefused:
        raise AdmissionRefused("url_refused") from None
    if sites.is_identity_provider(parsed.host):
        raise AdmissionRefused("identity_provider")
    return parsed.url


def _visible_site(user_id: str, site_key: str) -> dict | None:
    """The registry row by key, as sites_list shows it: a curated site, or
    an ad hoc row only while this owner has an active grant for it. Ad hoc
    rows are global (a colleague's grant creates them), so a key alone never
    reaches another user's approved site or reveals that it exists."""
    site = sites.get_site(user_id, site_key)
    if site is None:
        return None
    if site.get("kind") != "curated" and repo.browser_site_grant(user_id, str(site.get("site_key") or site_key)) is None:
        return None
    return site


def _resolve(user_id: str, site_key: str | None, url: str | None) -> tuple[dict | None, Any]:
    """(registry row or None, GrantTarget) for a start URL or a site key.
    A URL decides the site; a site key given with it must agree."""
    if url is not None:
        site, target = sites.resolve_site(user_id, url)
        if site_key is not None and site_key != target.site_key:
            raise AdmissionRefused("site_mismatch")
    elif site_key is not None:
        site = _visible_site(user_id, site_key)
        if site is None:
            raise AdmissionRefused("unknown_site")
        try:
            target = sites.grant_target_for_site(site)
        except ValueError:
            raise AdmissionRefused("invalid") from None
    else:
        raise AdmissionRefused("invalid")
    if site is not None and not site.get("enabled", True):
        raise AdmissionRefused("site_disabled")
    return site, target


def _project(user_id: str, conversation_id: str, project_id: Any, *, tainted: bool,
             user_texts: list[str]) -> str | None:
    """5.3 step 6: the project must exist and the owner must be allowed to
    upload to it (writer role, already required by the owner check, as
    routers/files.py upload_file). In a tainted conversation it must also be
    bound (4.3): opened by the user in this conversation (message context)
    or typed by the user."""
    if project_id is None:
        return None
    pid = _uuid(project_id)
    if pid is None or not repo.browser_project_exists(user_id, pid):
        raise AdmissionRefused("project_invalid")
    if tainted:
        typed = any(pid in (text or "").lower() for text in user_texts)
        if not typed and not repo.browser_project_in_context(user_id, conversation_id, pid):
            raise AdmissionRefused("project_not_bound")
    return pid


def _refuse_sso(user_id: str, site: dict | None, account_label: str | None) -> None:
    """Before wave E a site whose sign-in is SSO only (curated seed) or whose
    saved account is marked SSO (7.5) cannot run with a login."""
    if site is None:
        return
    if site.get("login_kind") == "sso_only":
        raise AdmissionRefused("sso_required")
    if site.get("login_kind") == "none":
        return
    row = repo.browser_credential_v2(user_id, str(site["site_key"]), account_label, active_only=True)
    if row is not None and row.get("sso_required"):
        raise AdmissionRefused("sso_required")


# ── Views (what the model and the task card see) ─────────────────────────────


def _scope_keys(admission: dict) -> list[str]:
    keys: list[str] = []
    start = admission.get("start_site_key")
    if isinstance(start, str) and start:
        keys.append(start)
    for entry in admission.get("site_scope") or ():
        key = entry.get("site_key") if isinstance(entry, dict) else None
        if isinstance(key, str) and key and key not in keys:
            keys.append(key)
    return keys


def _admission_view(admission: dict, needs: list[str], cards: list[dict] | None = None) -> dict:
    status = str(admission.get("status") or "waiting_grant")
    if status in WAITING_STATUSES and admission.get("expires_at") and _expired(admission.get("expires_at")):
        status = "expired"
    message = STATUS_MESSAGES.get(status, STATUS_MESSAGES["ready"])
    if "live_signin" in needs and status == "waiting_credential":
        message = SSO_MESSAGE
    return {"task_id": None, "admission_id": str(admission.get("id")), "status": status,
            "sites_in_scope": _scope_keys(admission), "needs": sorted(set(needs)), "cards": cards or [],
            "message": message}


def _job_view(job: dict, admission: dict | None = None) -> dict:
    seed = job.get("input") if isinstance(job.get("input"), dict) else {}
    admission_id = job.get("assistant_admission_id") or seed.get("admission_id")
    scope = _scope_keys(admission) if admission else [k for k in [seed.get("start_site_key")] if k]
    status = str(job.get("status") or "queued")
    message = STATUS_MESSAGES["enqueued"] if status in ACTIVE_JOB_STATUSES else "The task has ended."
    return {"task_id": str(job.get("id")), "admission_id": str(admission_id) if admission_id else None,
            "status": status, "sites_in_scope": scope, "needs": [], "cards": [], "message": message}


def _expired(value: Any) -> bool:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp <= datetime.now(timezone.utc)


# ── Cards (server-built; the model never words one) ──────────────────────────


def _post_card(user_id: str, conversation_id: str, block: dict, *, event_key: str, scope_id: str) -> bool:
    """Post one card through WI-13's writer. The request row is already
    saved, so a card that fails to post is reported (posted: false) and the
    admission waits; it never grants anything by itself."""
    from app.services import assistant_browser_chat as chat

    try:
        chat.post_card(user_id, conversation_id, block, event_key=event_key, scope_id=scope_id)
        return True
    except NotImplementedError:
        logger.warning("browser admission: chat cards are not built yet; %s card not posted", block.get("type"))
    except Exception:  # noqa: BLE001 - the request row stands; the model hears posted: false
        logger.exception("browser admission: %s card could not be posted", block.get("type"))
    return False


def _card_block(kind: str, request: dict, admission_id: str | None) -> dict:
    request_id = str(request["id"])
    if kind == "site_access":
        return {"type": "browser_site_access", "request_id": request_id, "status": "requested", "hold_id": None}
    if kind == "credential":
        return {"type": "browser_credential_request", "request_id": request_id, "site_key": request.get("site_key"),
                "account_label": request.get("account_label"), "status": "requested", "version": None}
    if kind == "link_open":
        return {"type": "browser_link_open", "request_id": request_id, "status": "requested"}
    return {"type": "browser_task_start", "admission_id": admission_id, "request_id": request_id,
            "status": "requested"}


def _open_request(user_id: str, *, kind: str, admission_id: str | None = None,
                  conversation_id: str | None = None, site_key: str | None = None,
                  account_label: str | None = None) -> dict | None:
    rows = repo.browser_requests_open(user_id, admission_id=admission_id, conversation_id=conversation_id,
                                      kind=kind)
    for row in rows:
        if admission_id is None and row.get("admission_id"):
            continue
        if site_key is not None and row.get("site_key") != site_key:
            continue
        if kind == "credential" and (row.get("account_label") or None) != (account_label or None):
            continue
        return row
    return None


def _request_card(user_id: str, conversation_id: str, record: dict, *, scope_id: str) -> dict:
    """Create the request row (or reuse the open one) and post its card.
    Returns {kind, request_id, posted}."""
    kind = record["kind"]
    existing = _open_request(user_id, kind=kind, admission_id=record.get("admission_id"),
                             conversation_id=None if record.get("admission_id") else conversation_id,
                             site_key=record.get("site_key"), account_label=record.get("account_label"))
    if existing is not None:
        return {"kind": kind, "request_id": str(existing["id"]), "posted": True, "reused": True}
    result = repo.browser_request_create(user_id, {**record, "conversation_id": conversation_id})
    request = result.get("request") if isinstance(result, dict) else None
    if not result.get("ok") or not isinstance(request, dict) or not request.get("id"):
        raise _refusal(result)
    block = _card_block(kind, request, record.get("admission_id"))
    posted = _post_card(user_id, conversation_id, block, event_key=f"{kind}:{request['id']}", scope_id=scope_id)
    return {"kind": kind, "request_id": str(request["id"]), "posted": posted}


def _task_card(user_id: str, job: dict, admission: dict | None) -> list[dict]:
    """The `browser_task` card (4.5: status line, progress, Cancel) for a job
    the enqueue just created. Message id uuid5(job id, "task"), so a retry
    never posts it twice; the terminal and waiting events come later from
    WI-13's outbox drain. Best effort: the job stands either way."""
    conversation_id = str(job.get("assistant_conversation_id") or (admission or {}).get("conversation_id") or "")
    if not conversation_id or not job.get("id"):
        return []
    seed = job.get("input") if isinstance(job.get("input"), dict) else {}
    goal = seed.get("goal") if isinstance(seed.get("goal"), str) else (admission or {}).get("goal")
    block = {"type": "browser_task", "job_id": str(job["id"]), "status": str(job.get("status") or "queued"),
             "steps": 0, "cost_usd": 0.0, "holds_pending": 0,
             "site_label": _site_label(user_id, seed.get("start_site_key") or job.get("platform")),
             "goal": goal[:GOAL_MAX] if isinstance(goal, str) else None}
    posted = _post_card(user_id, conversation_id, block, event_key="task", scope_id=str(job["id"]))
    return [{"kind": "task", "request_id": None, "posted": posted}]


def _key_missing_grant(user_id: str, admission: dict) -> str | None:
    for key in _scope_keys(admission):
        if repo.browser_site_grant(user_id, key) is None:
            return key
    return None


def _key_missing_login(user_id: str, admission: dict) -> tuple[str, str | None] | None:
    start = admission.get("start_site_key")
    registry = repo.browser_sites_by_keys(user_id, _scope_keys(admission))
    for key in _scope_keys(admission):
        site = registry.get(key)
        if site is None or site.get("login_kind") == "none":
            continue
        label = admission.get("account_label") if key == start else None
        row = repo.browser_credential_v2(user_id, key, label, active_only=True)
        if row is None or row.get("sso_required"):
            return key, label
    return None


def _next_card(user_id: str, admission: dict, needs: list[str]) -> list[dict]:
    """Post the card for the first missing piece (CARD_ORDER). Returns the
    card list for the view ([] when there is none to post, for example a
    live sign-in before wave E)."""
    kind = next((k for k in CARD_ORDER if k in needs), None)
    admission_id = str(admission["id"])
    conversation_id = str(admission.get("conversation_id") or "")
    if kind is None or kind == "live_signin" or not conversation_id:
        return []
    start = admission.get("start_site_key")
    provenance = admission.get("start_provenance")
    provenance = provenance if provenance in ("typed", "bdr_record", "search", "email", "page", "unknown") else None
    record: dict[str, Any] = {"kind": kind, "admission_id": admission_id}
    if kind == "site_access":
        key = _key_missing_grant(user_id, admission) or start
        record.update(site_key=key, reason_code="task_needs_site",
                      url=admission.get("start_url") if key == start else None,
                      provenance=provenance if key == start else None)
    elif kind == "credential":
        missing = _key_missing_login(user_id, admission)
        key, label = missing if missing else (start, admission.get("account_label"))
        record.update(site_key=key, account_label=label, reason_code="no_saved_login")
    elif kind == "link_open":
        record.update(site_key=start, url=admission.get("start_url"), reason_code="link_from_email",
                      provenance="email")
    else:
        record.update(site_key=start, reason_code="tainted_conversation" if admission.get("tainted")
                      else "unattended_turn")
    record = {k: v for k, v in record.items() if v is not None}
    return [_request_card(user_id, conversation_id, record, scope_id=admission_id)]


# ── Admission ────────────────────────────────────────────────────────────────


_GOAL_URL = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
GOAL_URLS_MAX = 10


def _refuse_email_link_in_goal(user_id: str, run_id: str, conversation_id: str, goal: str,
                               start_url: str | None) -> None:
    """D6, 5.5: an email link needs the link card every time, so it may not
    ride inside the free-text goal past the start_url provenance check. Any
    goal link that came from an email (and is not the start_url itself) is
    refused with link_in_goal; the model passes it as start_url instead."""
    tokens = [t.rstrip(".,;:!?)]}'\"") for t in _GOAL_URL.findall(goal)]
    if len(tokens) > GOAL_URLS_MAX:
        raise AdmissionRefused("link_in_goal")
    for token in tokens:
        try:
            normalized = sites.parse_url(token, top_level=True).url
        except sites.UrlRefused:
            continue
        if start_url is not None and normalized == start_url:
            continue
        found = sites.provenance_detail(user_id, run_id, normalized, conversation_id=conversation_id)
        if found.kind == "email":
            raise AdmissionRefused("link_in_goal")


def admit(user_id: str, run_id: str, conversation_id: str, args: dict, *, tainted: bool,
          fresh_user_message: bool) -> dict:
    """Admission for browser_task_start (5.3 steps 1 to 8): returns
    {task_id or admission_id, status, sites_in_scope, needs, cards,
    message}; cards are posted by the server. No job exists until nothing
    is missing; the enqueue RPC rechecks everything under the owner lock."""
    _require_ready(user_id)
    clean = _args(args, TASK_FIELDS)
    goal = _text(clean.get("goal"))
    if goal is None or len(goal) > GOAL_MAX:
        raise AdmissionRefused("invalid")
    site_key = _site_key(clean.get("site_key"))
    account_label = _account_label(clean.get("account_label"))
    conversation = _conversation(user_id, conversation_id)
    tainted = bool(tainted or conversation.get("tainted_at"))
    url = _normalized_url(clean["start_url"]) if clean.get("start_url") is not None else None
    site, target = _resolve(user_id, site_key, url)
    provenance = "none"
    if url is not None:
        provenance = sites.provenance_detail(user_id, run_id, url, conversation_id=conversation_id).kind
    _refuse_email_link_in_goal(user_id, run_id, conversation_id, goal, url)
    user_texts: list[str] = []
    if tainted and clean.get("project_id") is not None and run_id:
        try:
            user_texts = repo.mail_user_texts(user_id, str(run_id))
        except Exception:  # noqa: BLE001 - no typed text: the context check still runs
            user_texts = []
    project_id = _project(user_id, conversation_id, clean.get("project_id"), tainted=tainted, user_texts=user_texts)
    _refuse_sso(user_id, site, account_label)
    record: dict[str, Any] = {
        "conversation_id": conversation_id, "run_id": _uuid(run_id), "goal": goal, "start_url": url,
        "start_provenance": provenance, "start_site_key": target.site_key, "site_keys": [],
        "account_label": account_label, "project_id": project_id, "tainted": tainted,
        "fresh_user_message": bool(fresh_user_message),
    }
    result = repo.browser_admission_create(user_id, {k: v for k, v in record.items() if v is not None})
    admission = result.get("admission") if isinstance(result, dict) else None
    if not result.get("ok") or not isinstance(admission, dict) or not admission.get("id"):
        raise _refusal(result)
    needs = [str(n) for n in result.get("needs") or ()]
    if not needs:
        return enqueue_admission(user_id, str(admission["id"]))
    return _admission_view(admission, needs, _next_card(user_id, admission, needs))


def continue_admission(user_id: str, admission_id: str) -> dict:
    """After a grant, credential save, link or start decision: enqueue when
    nothing is missing, else post the next card. Returns admit's shape.
    An admission that already has its job answers with the job; a declined
    or expired one answers with its status (no card, no job)."""
    if not v2_enabled():
        raise AdmissionRefused("unavailable")
    aid = _uuid(admission_id)
    admission = repo.browser_admission(user_id, aid) if aid else None
    if admission is None:
        raise AdmissionRefused("not_found")
    status = admission.get("status")
    if status == "enqueued":
        job = repo.browser_job_by_admission(user_id, aid)
        return _job_view(job, admission) if job else _admission_view(admission, [])
    if status in ("declined", "expired") or _expired(admission.get("expires_at")):
        return _admission_view({**admission, "status": "expired" if status != "declined" else status}, [])
    return enqueue_admission(user_id, aid)


def enqueue_admission(user_id: str, admission_id: str) -> dict:
    """assistant_browser_task_enqueue_v2 with the configured day caps (the
    RPC reserves the first step of user_task_reserve_step_usd, never the
    whole task cap). When the RPC finds something missing, the next card is
    posted and the waiting view returned; any other refusal raises
    AdmissionRefused."""
    from app.assistant import spend

    user_cap, global_cap = spend.cap_defaults()
    result = repo.browser_task_enqueue_v2(user_id, admission_id, {}, user_cap_default=user_cap,
                                          global_cap_default=global_cap)
    if result.get("ok") and isinstance(result.get("job"), dict):
        admission = repo.browser_admission(user_id, admission_id)
        view = _job_view(result["job"], admission)
        if result.get("created") is True:
            view["cards"] = _task_card(user_id, result["job"], admission)
        return view
    if result.get("code") == "needs":
        admission = repo.browser_admission(user_id, admission_id)
        if admission is None:
            raise AdmissionRefused("not_found")
        needs = [str(n) for n in result.get("needs") or ()]
        return _admission_view(admission, needs, _next_card(user_id, admission, needs))
    raise _refusal(result)


# ── Model-facing reads, cancel and card requests (4.3) ───────────────────────


def _money(value: Any) -> float:
    try:
        return float(Decimal(str(value or 0)))
    except (InvalidOperation, ValueError):
        return 0.0


def _result_payload(user_id: str, job_id: str) -> tuple[str | None, list[dict]]:
    """The newest result record's text and facts (encrypted at rest, read
    owner-bound). Untrusted page text: the tool marks it tainted."""
    from app.services import assistant_browser_records as records

    try:
        rows = repo.browser_records(user_id, job_id, kinds=("result",), before=None, limit=1)
        if not rows:
            return None, []
        value = records.unpack_record(user_id, job_id, rows[0])
    except Exception:  # noqa: BLE001 - the status still answers without the result
        logger.warning("browser status: result record unavailable")
        return None, []
    # The v2 record (runtime.finish) keeps the model's `result` and
    # facts[{text, source_url}]; older shapes use `text` or `summary` and `url`.
    text = value.get("result") or value.get("text") or value.get("summary")
    facts: list[dict] = []
    for index, fact in enumerate(value.get("facts") or ()):
        if not isinstance(fact, dict) or not isinstance(fact.get("text"), str) or not fact["text"]:
            continue
        raw_url = fact.get("source_url") or fact.get("url")
        url = raw_url if isinstance(raw_url, str) and len(raw_url) <= 2048 else None
        facts.append({"id": str(fact.get("id") or f"f{index + 1}")[:80], "text": fact["text"][:2000], "url": url})
        if len(facts) >= FACTS_MAX:
            break
    return (text[:RESULT_TEXT_MAX] if isinstance(text, str) else None), facts


def _site_label(user_id: str, site_key: Any) -> str | None:
    if not isinstance(site_key, str) or not SITE_KEY_RE.match(site_key):
        return None
    try:
        row = repo.browser_site(user_id, site_key)
    except Exception:  # noqa: BLE001 - a label is decoration
        return None
    return str(row.get("label")) if row and row.get("label") else None


def task_status(user_id: str, task_id: str) -> dict:
    """browser_task_status: a job (status, steps, cost so far, open holds,
    the result when done), a waiting admission (status and needs), or a
    credential request (saved, site key, account label, version; never the
    username). Raises AdmissionRefused("not_found"). Reads of the user's own
    rows need only the flags (a task stays visible after the owner loses
    the assistant)."""
    _require_flags()
    tid = _uuid(task_id)
    if tid is None:
        raise AdmissionRefused("not_found")
    job = repo.browser_job(user_id, tid)
    if job is None:
        admission = repo.browser_admission(user_id, tid)
        if admission is not None:
            refused: AdmissionRefused | None = None
            if admission.get("status") == "enqueued":
                job = repo.browser_job_by_admission(user_id, tid)
            elif admission.get("status") == "ready" and not _expired(admission.get("expires_at")):
                # A ready admission whose enqueue was refused (a cap) has
                # nothing else retrying it: the status read retries it.
                admission, job, refused = _retry_ready(user_id, tid, admission)
            if job is None:
                view = _admission_view(admission, [str(n) for n in admission.get("needs") or ()])
                view.update(steps=0, cost_usd=0.0, holds_pending=0, result=None, facts=[],
                            site_label=_site_label(user_id, admission.get("start_site_key")),
                            goal=admission.get("goal"))
                if refused is not None:
                    view["refused"] = refused.code
                    view["message"] = f"The task is ready but has not started. {refused.message}"
                return view
            tid = str(job["id"])
    if job is None:
        request = repo.browser_request(user_id, tid)
        if request is None:
            raise AdmissionRefused("not_found")
        result = request.get("result") if isinstance(request.get("result"), dict) else {}
        version = result.get("version") if isinstance(result.get("version"), int) else None
        return {"request_id": str(request["id"]), "kind": request.get("kind"), "status": request.get("status"),
                "saved": request.get("status") == "saved", "site_key": request.get("site_key"),
                "account_label": request.get("account_label") or result.get("account_label"),
                "version": version}
    if job.get("agent_type") != TYPE_KEY or job.get("principal_user_id") != user_id:
        raise AdmissionRefused("not_found")
    seed = job.get("input") if isinstance(job.get("input"), dict) else {}
    status = str(job.get("status") or "queued")
    try:
        holds = len(repo.browser_holds(user_id, tid, status="open"))
    except Exception:  # noqa: BLE001
        holds = 0
    text, facts = _result_payload(user_id, tid) if status in TERMINAL_JOB_STATUSES else (None, [])
    return {"task_id": tid, "admission_id": str(job.get("assistant_admission_id") or "") or None,
            "status": status, "error_kind": job.get("error_kind"), "steps": int(job.get("step_count") or 0),
            "cost_usd": _money(job.get("cost_usd")), "holds_pending": holds, "result": text, "facts": facts,
            "site_label": _site_label(user_id, seed.get("start_site_key") or job.get("platform")),
            "goal": seed.get("goal") if isinstance(seed.get("goal"), str) else None}


def _retry_ready(user_id: str, admission_id: str,
                 admission: dict) -> tuple[dict, dict | None, AdmissionRefused | None]:
    """Enqueue a ready admission again (owner-scoped; the RPC rechecks the
    caps and everything else under the owner lock). Returns the fresh
    admission, the job when one now exists, and the refusal if any."""
    try:
        _require_owner(user_id)
        result = continue_admission(user_id, admission_id)
    except AdmissionRefused as exc:
        return admission, None, exc
    except Exception as exc:  # noqa: BLE001 - the status read still answers
        logger.warning("browser admission %s retry failed (%s)", admission_id, type(exc).__name__)
        return admission, None, None
    job = repo.browser_job_by_admission(user_id, admission_id) if (result or {}).get("task_id") else None
    return repo.browser_admission(user_id, admission_id) or admission, job, None


def cancel_task(user_id: str, task_id: str) -> dict:
    """browser_task_cancel, own tasks only: a job gets cancel_requested_at
    (the fence stops it, the sweep ends it `canceled`); a waiting admission
    has its open cards cancelled, which declines it. Returns {status}.
    Stopping work only removes authority, so the flags are the only gate."""
    _require_flags()
    tid = _uuid(task_id)
    if tid is None:
        raise AdmissionRefused("not_found")
    job = repo.browser_job(user_id, tid)
    if job is None:
        admission = repo.browser_admission(user_id, tid)
        if admission is None:
            raise AdmissionRefused("not_found")
        if admission.get("status") == "enqueued":
            job = repo.browser_job_by_admission(user_id, tid)
        if job is None:
            if admission.get("status") not in WAITING_STATUSES:
                return {"status": str(admission.get("status"))}
            cancelled = False
            for row in repo.browser_requests_open(user_id, admission_id=tid):
                result = repo.browser_request_decide(user_id, str(row["id"]), "cancel")
                cancelled = cancelled or bool(result.get("ok"))
            if not cancelled and str(admission.get("status") or "").startswith("waiting_"):
                # No open card left (it expired or never posted): decline the
                # admission itself, which only removes authority.
                cancelled = bool(repo.browser_start_decide(user_id, tid, "decline").get("ok"))
            return {"status": "declined" if cancelled else str(admission.get("status"))}
    if job.get("agent_type") != TYPE_KEY or job.get("principal_user_id") != user_id:
        raise AdmissionRefused("not_found")
    if job.get("status") in ("queued", "running", "waiting_help"):
        if job.get("cancel_requested_at") or repo.browser_job_cancel_request(user_id, str(job["id"])):
            return {"status": "cancel_requested"}
        fresh = repo.browser_job(user_id, tid) or job
        return {"status": str(fresh.get("status"))}
    return {"status": str(job.get("status"))}


def sites_list(user_id: str) -> dict:
    """browser_sites_list: the curated sites and the user's approved sites,
    each with its saved account labels and whether a live saved session
    exists. Never usernames, never origins of other users' rows."""
    _require_flags()
    grants = {str(g["site_key"]): g for g in repo.browser_site_grants(user_id, status="active") if g.get("site_key")}
    curated = {str(s["site_key"]): s for s in repo.browser_sites(user_id, kind="curated") if s.get("site_key")}
    registry = {**curated, **repo.browser_sites_by_keys(user_id, sorted(grants))}
    live = set()
    for row in repo.browser_sessions_v2(user_id):
        expires = row.get("expires_at")
        if row.get("site_key") and expires and not _expired(expires):
            live.add(str(row["site_key"]))
    items = []
    for key in sorted(registry, key=lambda k: (k not in curated, k)):
        site = registry[key]
        if not site.get("enabled", True):
            continue
        approved = key in grants
        labels: list[str] = []
        if approved:
            labels = [str(r["account_label"]) for r in repo.browser_credentials_for_site(user_id, key)
                      if r.get("status") == "active" and r.get("account_label") and not r.get("sso_required")]
        items.append({"site_key": key, "label": site.get("label"), "kind": site.get("kind"), "approved": approved,
                      "account_labels": labels, "has_session": key in live and approved,
                      "login_kind": site.get("login_kind"), "prefer_api": site.get("prefer_api")})
    return {"items": items}


def request_site_access(user_id: str, run_id: str | None, conversation_id: str, args: dict) -> dict:
    """request_site_access: posts the site access card (only a human click
    grants). A site already approved answers already_approved and posts
    nothing. Returns {request_id, status, site_key, posted}."""
    _require_ready(user_id)
    clean = _args(args, SITE_ACCESS_FIELDS)
    if clean.get("reason_code") not in SITE_ACCESS_REASONS:
        raise AdmissionRefused("invalid")
    _conversation(user_id, conversation_id)
    url = _normalized_url(clean["url"]) if clean.get("url") is not None else None
    site, target = _resolve(user_id, _site_key(clean.get("site_key")), url)
    if site is not None and repo.browser_site_grant(user_id, target.site_key) is not None:
        return {"request_id": None, "status": "already_approved", "site_key": target.site_key, "posted": False}
    provenance = None
    if url is not None:
        provenance = sites.provenance_detail(user_id, run_id, url, conversation_id=conversation_id).kind
    record = {"kind": "site_access", "site_key": target.site_key, "url": url, "reason_code": clean["reason_code"],
              "provenance": provenance}
    card = _request_card(user_id, conversation_id, {k: v for k, v in record.items() if v is not None},
                         scope_id=conversation_id)
    return {"request_id": card["request_id"], "status": "card_posted", "site_key": target.site_key,
            "posted": card["posted"]}


def request_site_credential(user_id: str, run_id: str | None, conversation_id: str, args: dict) -> dict:
    """request_site_credential: posts the secure credential card; the user
    fills it on the human-only route. Returns {request_id, status, posted};
    the save shows later in browser_task_status(request_id)."""
    _require_ready(user_id)
    clean = _args(args, CREDENTIAL_FIELDS)
    if clean.get("reason_code") not in CREDENTIAL_REASONS:
        raise AdmissionRefused("invalid")
    _conversation(user_id, conversation_id)
    site_key = _site_key(clean.get("site_key"))
    label = _account_label(clean.get("account_label"))
    site = _visible_site(user_id, site_key) if site_key else None
    if site is None:
        raise AdmissionRefused("unknown_site")
    if not site.get("enabled", True):
        raise AdmissionRefused("site_disabled")
    if site.get("login_kind") == "none":
        raise AdmissionRefused("no_login")
    if site.get("login_kind") == "sso_only":
        raise AdmissionRefused("sso_required")
    record = {"kind": "credential", "site_key": site_key, "account_label": label,
              "reason_code": clean["reason_code"]}
    card = _request_card(user_id, conversation_id, {k: v for k, v in record.items() if v is not None},
                         scope_id=conversation_id)
    return {"request_id": card["request_id"], "status": "card_posted", "posted": card["posted"]}

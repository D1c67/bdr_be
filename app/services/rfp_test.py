"""RFP Ingestion: test mode and monitor (docs/RFP_TESTING.md).

The dev-only bench that lets the IT Admin exercise the whole RFP Ingestion
feature end to end against real forwarded mail. This module owns:

- the session (section 3): `activate` with its preflight, `end_session`,
  `set_auto_create`, and the cheap `active_session` read the pollers and the
  senders make once per tick or per send;
- the event ledger (section 7): `record`, which NEVER raises into a
  pipeline, and the detail cap; every capture point in the intake, the
  filer, the harvest, the creation step and the senders is a one-liner
  guarded by `session_for_email(row)`, so the normal path costs one dict
  lookup;
- the forward unwrap (section 5): `unwrap_forward` reads the ORIGINAL
  sender, subject, date and body out of an inline forward (Outlook, the
  older "Original Message" block, Gmail, Apple Mail) or out of a forwarded
  `.eml` / item attachment, so the pipeline judges the GC's message, not
  the forwarder's;
- the outbound redirect (section 6): `redirect` rewrites a Graph message
  dict to go to RFP_TESTING_REDIRECT_TO as plain text with a header naming
  the intended recipients (resolved through profiles, GC contacts and
  vendor contacts), and fails CLOSED on an empty address;
- the cleanup (section 9): `cleanup` deletes the tagged rows of an ended
  session in dependency order and writes the report on the session row;
- the pure `step_chips`, which the router uses to draw an email's step
  strip from its status and the session's events.

Every function that takes `sb` is sync (the Supabase SDK is sync) and is
called from a thread or a plain `def` router, never from `async def`.

Nothing here reads the session table while RFP_TESTING_ENABLED is false:
`active_session` answers None without a query, so a deployment without the
bench (production) never pays for it. The rest of this module is inert
until a session row exists.
"""

from __future__ import annotations

import email as email_lib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy as email_policy
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.core.config import get_settings
from app.core.roles import Role
from app.services.graph_email import graph_request, graph_stream

logger = logging.getLogger(__name__)

TABLE_SESSIONS = "rfp_test_sessions"
TABLE_EVENTS = "rfp_test_events"

STATUS_ACTIVE = "active"
STATUS_ENDED = "ended"

# Event sources (docs section 7.1).
SOURCE_SESSION = "session"
SOURCE_INTAKE = "intake"
SOURCE_FILER = "filer"
SOURCE_HARVEST = "harvest"
SOURCE_CREATE = "create"
SOURCE_SPLIT = "split"        # the Bid File Splitter step (docs/RFP_SPLIT.md 7)
SOURCE_MAIL_OUT = "mail_out"
SOURCE_HUMAN = "human"

LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"

# Error codes (app.core.error_codes carries the same strings for the router).
CODE_ALREADY_ACTIVE = "rfp_testing_already_active"
CODE_SESSION_ACTIVE = "rfp_testing_session_active"
CODE_SESSION_ENDED = "rfp_testing_session_ended"
CODE_INGEST_OFF = "rfp_testing_ingest_off"
CODE_GRAPH_OFF = "rfp_testing_graph_off"
CODE_MAILBOX_UNREACHABLE = "rfp_testing_mailbox_unreachable"
CODE_REDIRECT_INVALID = "rfp_testing_redirect_invalid"

# Why a message in the test mailbox was not read (sections 4.1 and 4.2).
IGNORED_NOT_TEST_SENDER = "not_test_sender"
IGNORED_BEFORE_SESSION = "before_session"

# How a test row's forward was unwrapped (section 5.3).
UNWRAP_INLINE = "inline"
UNWRAP_EML = "eml"
UNWRAP_NONE = "none"
# The flag_reason of a test row the listing-time rules would have dropped
# (section 5.3).
FLAG_TEST_LISTING_SKIP = "test_listing_skip"
MSG_LISTING_SKIP = "In production this message would never get a row: {reason}"
AUTH_FORWARD_NOTE = "judged on the forward's headers (original headers are not available)"
AUTH_FORWARD_PASS_NOTE = (
    "judged on the forward's headers (original headers are not available); the policy read "
    "{policy} on the tenant's own user, so the row passes on the tenant's trust in the "
    "accepted sender"
)

# The graph_sync_state ids the activation resets (section 3.2): the RFP
# intake's delta key and the filer's, both `{prefix}:{mailbox}:inbox`. The
# prefixes are spelled here rather than imported (both pollers import this
# module); tests pin them to the pollers' constants.
INTAKE_SYNC_PREFIX = "rfp-mail"
FILER_SYNC_PREFIX = "pm-mail"

# The ten steps of the strip on the RFP emails tab (section 8), in order.
STEPS = (
    "received", "auth", "keywords", "classify", "authorize", "method", "extract", "match",
    "harvest", "split", "create",
)
STEP_DONE = "done"
STEP_CURRENT = "current"
STEP_WAITING = "waiting"
STEP_SKIPPED = "skipped"
STEP_FAILED = "failed"
STEP_PENDING = "pending"
# decided_at_step values that do not name a chip directly.
_STEP_ALIASES = {"fetch": "received", "review_match": "match"}
# The human lanes: the chip a person is being waited on.
_STATUS_WAITING_STEP = {
    "review_llm": "classify",
    "flagged_unauthorized": "authorize",
    "review_match": "match",
}
# Terminal statuses whose failing chip is fixed by the status itself; the
# rest (rejected_by_review, failed) fail at decided_at_step.
_STATUS_FAILED_STEP = {
    "flagged_auth": "auth",
    "flagged_no_keywords": "keywords",
    "flagged_llm_no": "classify",
}

_DETAIL_MAX_BYTES = 64 * 1024
_TRUNCATED_MARKER = " [truncated]"
_MIN_STRING_CAP = 200
PREVIEW_MAX_CHARS = 2000
_TITLE_MAX_CHARS = 300
_SESSIONS_LIST_MAX = 50
_IN_CHUNK = 200
_EML_PARTS_MAX = 100

_EMAIL_RE = re.compile(r"[^@\s,;<>\[\]\"']+@[^@\s,;<>\[\]\"']+\.[^@\s,;<>\[\]\"']+")
# Forwarded-subject prefixes, repeated: "FW: Fwd: TR: Invitation".
_FORWARD_PREFIX_RE = re.compile(r"^\s*(?:(?:fw|fwd|tr)\s*:\s*)+", re.IGNORECASE)
# The marker lines a forward starts with (section 5.1), matched on a
# stripped line. Outlook's default has no marker: its block starts with
# "From:" directly, which `_HEADER_LINE_RE` recognises below.
_MARKER_RES = (
    re.compile(r"^-{2,}\s*original message\s*-{2,}$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*forwarded message\s*-{2,}$", re.IGNORECASE),
    re.compile(r"^begin forwarded message:?$", re.IGNORECASE),
)
_HEADER_LINE_RE = re.compile(
    r"^\s*(from|sent|date|to|cc|bcc|subject|reply-to|importance)\s*:\s*(.*)$", re.IGNORECASE
)
_MAX_BLOCK_LINES = 40
# Wall-clock forms the three mail clients write when RFC 2822 parsing fails
# (Outlook "Tuesday, September 16, 2026 9:12 AM", Gmail "Tue, Sep 16, 2026
# at 9:12 AM", Apple "September 16, 2026 at 9:12:34 AM PDT"). Naive results
# are read as company (Pacific) time.
_COMPANY_TZ = ZoneInfo("America/Los_Angeles")
_DATE_FORMATS = (
    "%A, %B %d, %Y %I:%M %p",
    "%A, %B %d, %Y %I:%M:%S %p",
    "%a, %b %d, %Y at %I:%M %p",
    "%B %d, %Y at %I:%M:%S %p",
    "%B %d, %Y at %I:%M %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %I:%M:%S %p",
)
_TZ_SUFFIX_RE = re.compile(r"\s+(?:[A-Z]{2,5}|[+-]\d{4})$")

_ROLE_LABELS: dict[str, str] = {
    Role.ESTIMATING_ENGINEER_MATERIALS.value: "Estimating Engineer (Materials)",
    Role.ESTIMATING_ENGINEER_LABOR.value: "Estimating Engineer (Labor)",
    Role.ESTIMATING_ADMIN.value: "Estimating Admin",
    Role.EXECUTIVE.value: "Executive",
    Role.ACCOUNTANT.value: "Accountant",
    Role.IT_ADMIN.value: "IT Admin",
    Role.ESTIMATOR.value: "Estimator",
}
_HEADER_RULE = "-" * 62


class RfpTestError(Exception):
    """A refused session action: `code` is the detail the router sends
    (and the page shows verbatim), `status` the HTTP status."""

    def __init__(self, code: str, message: str | None = None, *, status: int = 409):
        super().__init__(message or code)
        self.code = code
        self.status = status


class RedirectRefused(RuntimeError):
    """A send refused while a session is active because the redirect
    address is empty (section 6, fail closed)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc).lower()
    return "23505" in text or "duplicate key" in text


def _chunks(items: list, size: int = _IN_CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ── Sessions (section 3) ─────────────────────────────────────────────────────


def active_session(sb) -> dict | None:
    """The single active session, or None. Free while the switch is off
    (no query), one small read otherwise: the pollers call it once per tick
    and the senders once per send, never per row (section 3.4)."""
    if not get_settings().rfp_testing_enabled:
        return None
    rows = (
        sb.table(TABLE_SESSIONS).select("*").eq("status", STATUS_ACTIVE).limit(1).execute()
    ).data or []
    return rows[0] if rows else None


def get_session(sb, session_id: str) -> dict | None:
    rows = sb.table(TABLE_SESSIONS).select("*").eq("id", session_id).limit(1).execute().data or []
    return rows[0] if rows else None


def list_sessions(sb, limit: int = _SESSIONS_LIST_MAX) -> list[dict]:
    return (
        sb.table(TABLE_SESSIONS)
        .select("*")
        .order("started_at", desc=True)
        .limit(max(1, min(int(limit), _SESSIONS_LIST_MAX)))
        .execute()
    ).data or []


def session_for_email(row: dict | None) -> str | None:
    """The tag on a pipeline row: the guard every capture point reads."""
    return (row or {}).get("test_session_id")


def preflight(settings=None) -> None:
    """The activation checks (section 3.2, step 2), each an RfpTestError
    with a 422 code the page shows verbatim. The cheap checks come first so
    a misconfiguration never spends a Graph round trip."""
    settings = settings or get_settings()
    if not settings.rfp_ingest_enabled:
        raise RfpTestError(CODE_INGEST_OFF, status=422)
    if not settings.ms_client_id:
        raise RfpTestError(CODE_GRAPH_OFF, status=422)
    if not _EMAIL_RE.fullmatch((settings.rfp_testing_redirect_to or "").strip()):
        raise RfpTestError(CODE_REDIRECT_INVALID, status=422)
    mailbox = (settings.rfp_testing_mailbox or "").strip().lower()
    try:
        resp = graph_request("GET", f"/users/{mailbox}/mailFolders/inbox")
    except httpx.HTTPStatusError as exc:
        raise RfpTestError(
            CODE_MAILBOX_UNREACHABLE, f"{CODE_MAILBOX_UNREACHABLE}: {exc.response.status_code}",
            status=422,
        ) from exc
    except Exception as exc:  # noqa: BLE001 - a transport error is unreachable too
        raise RfpTestError(
            CODE_MAILBOX_UNREACHABLE, f"{CODE_MAILBOX_UNREACHABLE}: {type(exc).__name__}",
            status=422,
        ) from exc
    if resp.status_code != 200:
        raise RfpTestError(
            CODE_MAILBOX_UNREACHABLE, f"{CODE_MAILBOX_UNREACHABLE}: {resp.status_code}",
            status=422,
        )


def reset_delta_links(sb, mailbox: str) -> None:
    """Drop the test mailbox's stored delta links (both pollers) so the
    first tick does an initial pull; messages older than started_at are
    dropped by the sync filter, so the lookback window is irrelevant."""
    keys = [f"{INTAKE_SYNC_PREFIX}:{mailbox}:inbox", f"{FILER_SYNC_PREFIX}:{mailbox}:inbox"]
    sb.table("graph_sync_state").delete().in_("id", keys).execute()


def activate(sb, *, actor_id: str | None, name: str | None, auto_create: bool) -> dict:
    """Section 3.2: one active session at a time (409 on a second), the
    preflight (422), the insert with the settings snapshot, the delta
    reset, the `session.activated` event."""
    settings = get_settings()
    if active_session(sb) is not None:
        raise RfpTestError(CODE_ALREADY_ACTIVE, status=409)
    preflight(settings)
    mailbox = settings.rfp_testing_mailbox.strip().lower()
    row = {
        "status": STATUS_ACTIVE,
        "name": (name or "").strip()[:200] or None,
        "started_by": actor_id,
        "started_at": _iso(_now()),
        "mailbox": mailbox,
        "sender": settings.rfp_testing_sender.strip().lower(),
        "redirect_to": settings.rfp_testing_redirect_to.strip(),
        "auto_create": bool(auto_create),
    }
    try:
        session = sb.table(TABLE_SESSIONS).insert(row).execute().data[0]
    except Exception as exc:  # noqa: BLE001 - the partial unique index turns a race into the 409
        if _is_unique_violation(exc):
            raise RfpTestError(CODE_ALREADY_ACTIVE, status=409) from exc
        raise
    reset_delta_links(sb, mailbox)
    record(
        sb, session_id=session["id"], source=SOURCE_SESSION, kind="activated",
        title=f"Session activated: watching {mailbox}, accepting {row['sender']}, "
              f"redirecting to {row['redirect_to']}",
        detail={
            "name": row["name"], "mailbox": mailbox, "sender": row["sender"],
            "redirect_to": row["redirect_to"], "auto_create": row["auto_create"],
            "started_by": actor_id,
        },
    )
    return session


def end_session(sb, session_id: str, *, actor_id: str | None) -> dict:
    """Section 3.3: idempotent; a second call on an ended session returns
    it unchanged. Rows tagged with an ended session are frozen (neither
    poller sweeps them again) and the paused pollers resume on their next
    tick."""
    session = get_session(sb, session_id)
    if session is None:
        raise LookupError("Test session not found")
    if session.get("status") == STATUS_ENDED:
        return session
    now_iso = _iso(_now())
    rows = (
        sb.table(TABLE_SESSIONS)
        .update({"status": STATUS_ENDED, "ended_at": now_iso, "ended_by": actor_id})
        .eq("id", session_id)
        .eq("status", STATUS_ACTIVE)
        .execute()
    ).data or []
    if not rows:
        return get_session(sb, session_id) or session
    record(
        sb, session_id=session_id, source=SOURCE_SESSION, kind="ended",
        title="Session ended; the paused pollers resume on their next tick",
        detail={"ended_by": actor_id},
    )
    return rows[0]


def set_auto_create(sb, session_id: str, auto_create: bool) -> dict:
    """PATCH on the active session only (409 rfp_testing_session_ended)."""
    session = get_session(sb, session_id)
    if session is None:
        raise LookupError("Test session not found")
    rows = (
        sb.table(TABLE_SESSIONS)
        .update({"auto_create": bool(auto_create)})
        .eq("id", session_id)
        .eq("status", STATUS_ACTIVE)
        .execute()
    ).data or []
    if not rows:
        raise RfpTestError(CODE_SESSION_ENDED, status=409)
    return rows[0]


def heartbeat(sb, session_id: str | None, column: str) -> None:
    """`intake_last_tick_at` / `filer_last_tick_at`: the page's "checked
    12 s ago" text. Never raises into a tick."""
    if not session_id:
        return
    try:
        sb.table(TABLE_SESSIONS).update({column: _iso(_now())}).eq("id", session_id).execute()
    except Exception:  # noqa: BLE001
        logger.warning("rfp test: heartbeat %s failed for session %s", column, session_id, exc_info=True)


def poll_seconds(settings, normal: int, test_mode: bool) -> int:
    """The sleep after a tick: RFP_TESTING_POLL_SECONDS while a session is
    active, the poller's own interval otherwise (sections 4.1 and 4.2)."""
    if test_mode and getattr(settings, "rfp_testing_enabled", False):
        return int(settings.rfp_testing_poll_seconds)
    return int(normal)


def message_filter(session: dict, from_address: str | None, received_at: Any) -> str | None:
    """The test-mode listing rule (sections 4.1 and 4.2): None when the
    message is read (From equals the accepted sender, case-insensitive,
    received at or after started_at), else the ignore reason."""
    sender = (session.get("sender") or "").strip().lower()
    if (from_address or "").strip().lower() != sender:
        return IGNORED_NOT_TEST_SENDER
    started = _parse_ts(session.get("started_at"))
    received = _parse_ts(received_at)
    if started is not None and (received is None or received < started):
        return IGNORED_BEFORE_SESSION
    return None


# ── Events (section 7) ───────────────────────────────────────────────────────


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _encoded_size(value: Any) -> int:
    return len(json.dumps(value, default=str).encode("utf-8"))


def _truncate_strings(value: Any, cap: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= cap else value[:cap] + _TRUNCATED_MARKER
    if isinstance(value, dict):
        return {k: _truncate_strings(v, cap) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate_strings(v, cap) for v in value]
    return value


def cap_detail(detail: Any) -> dict:
    """The event detail as stored: a JSON-safe dict at most 64 KB. Long
    strings (a prompt, a body) are truncated with a marker, the cap halving
    until the whole document fits; a document that is too big even with
    every string at 200 characters (thousands of keys) keeps its key list
    only. A non-dict detail is wrapped as {"value": ...}."""
    if detail is None:
        return {}
    if not isinstance(detail, dict):
        detail = {"value": detail}
    try:
        safe = _json_safe(detail)
    except Exception:  # noqa: BLE001 - a detail that cannot be encoded at all
        return {"value": str(detail)[:_MIN_STRING_CAP] + _TRUNCATED_MARKER}
    if _encoded_size(safe) <= _DETAIL_MAX_BYTES:
        return safe
    cap = 16_000
    while cap >= _MIN_STRING_CAP:
        trimmed = _truncate_strings(safe, cap)
        if _encoded_size(trimmed) <= _DETAIL_MAX_BYTES:
            trimmed["truncated"] = True
            return trimmed
        cap //= 2
    return {"truncated": True, "keys": sorted(str(k) for k in safe)[:500]}


def record(
    sb, *, session_id: str | None, source: str, kind: str, title: str, level: str = LEVEL_INFO,
    detail: Any = None, rfp_email_id: str | None = None, ingested_email_id: str | None = None,
    harvest_id: str | None = None, project_id: str | None = None,
) -> None:
    """One rfp_test_events insert. NEVER raises into the pipeline that
    called it: a failure is logged at warning and the step goes on. A None
    session_id is a no-op, so a capture point can pass the row's tag
    straight through."""
    if not session_id:
        return
    try:
        sb.table(TABLE_EVENTS).insert(
            {
                "session_id": session_id,
                "source": source,
                "kind": kind,
                "level": level if level in (LEVEL_INFO, LEVEL_WARN, LEVEL_ERROR) else LEVEL_INFO,
                "title": (str(title) or kind)[:_TITLE_MAX_CHARS],
                "rfp_email_id": rfp_email_id,
                "ingested_email_id": ingested_email_id,
                "harvest_id": harvest_id,
                "project_id": project_id,
                "detail": cap_detail(detail),
            }
        ).execute()
    except Exception:  # noqa: BLE001 - the ledger must never fail a pipeline step
        logger.warning(
            "rfp test: event %s.%s not recorded for session %s", source, kind, session_id,
            exc_info=True,
        )


def events_for(
    sb, session_id: str, *, after: int = 0, limit: int = 500, rfp_email_id: str | None = None,
    source: str | None = None, project_id: str | None = None, newest_first: bool = False,
) -> list[dict]:
    """The session's events, ascending by id from `after` (the page's
    cursor), optionally narrowed to one email, one source or one project."""
    query = sb.table(TABLE_EVENTS).select("*").eq("session_id", session_id)
    if after:
        query = query.gt("id", int(after))
    if rfp_email_id:
        query = query.eq("rfp_email_id", rfp_email_id)
    if source:
        query = query.eq("source", source)
    if project_id:
        query = query.eq("project_id", project_id)
    return (
        query.order("id", desc=newest_first).limit(max(1, min(int(limit), 500))).execute()
    ).data or []


# ── Forward unwrap (section 5) ───────────────────────────────────────────────


@dataclass
class Unwrapped:
    """What the fetch step writes for a test row (section 5.3): the
    effective sender, subject, date and body, plus the forward's own values
    and the `.eml` parts when the original came as an attachment."""

    method: str
    from_address: str | None
    from_name: str | None
    subject: str | None
    received_at: str | None          # ISO; the forward's when the block gave no date
    body_text: str
    forwarder_note: str | None = None
    eml_attachment_id: str | None = None
    eml_parts: list[dict] = field(default_factory=list)


def strip_forward_prefixes(subject: Any) -> str | None:
    """Leading FW: / Fwd: / FW : / TR: prefixes, repeated, removed."""
    if subject is None:
        return None
    text = _FORWARD_PREFIX_RE.sub("", str(subject)).strip()
    return text or None


def parse_address(value: Any) -> tuple[str | None, str | None]:
    """(name, address) from a forwarded From line: `Name <addr>`, a bare
    address, Outlook's `Name [mailto:addr]`, or a name alone (no address)."""
    text = " ".join(str(value or "").replace("mailto:", "").split())
    if not text:
        return None, None
    name, addr = parseaddr(text)
    if not addr or "@" not in addr:
        hit = _EMAIL_RE.search(text)
        addr = hit.group(0) if hit else ""
        name = text[: hit.start()] if hit else text
    name = name.strip().strip("\"'").rstrip("<[( ").strip() or None
    addr = addr.strip().strip("<>[]() ").lower() or None
    return name, addr


def parse_forward_date(value: Any) -> datetime | None:
    """`email.utils.parsedate_to_datetime` first; then the wall-clock forms
    the three clients write, read as company (Pacific) time. None when
    nothing parses (the caller keeps the forward's receivedDateTime)."""
    text = " ".join(str(value or "").split())
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is not None and parsed.tzinfo:
        return parsed  # a real RFC 2822 date with its own offset
    # The lenient parser also reads the Outlook wall-clock form but drops
    # the AM/PM marker ("2:10 PM" comes back as 02:10), so the explicit
    # forms below are tried first and its naive answer is the last resort.
    candidates = [text, _TZ_SUFFIX_RE.sub("", text)]
    for candidate in candidates:
        for fmt in _DATE_FORMATS:
            try:
                naive = datetime.strptime(candidate, fmt)
            except ValueError:
                continue
            return naive.replace(tzinfo=_COMPANY_TZ)
    if parsed is not None:
        return parsed.replace(tzinfo=_COMPANY_TZ)  # naive: company time
    return None


@dataclass
class ForwardBlock:
    """A recognised header block inside an inline forward."""

    headers: dict[str, str]
    note: str | None
    body: str
    marker: str


def parse_inline_forward(body_text: str | None, scan_chars: int | None = None) -> ForwardBlock | None:
    """Section 5.1: the first recognised header block within the scan
    window, read case-insensitively at the start of a line. A block must
    carry a From line and at least one of Subject / Sent / Date, so a
    "From:" in prose never counts. The text before the block is the
    forwarder's note, the text after it the effective body."""
    if not body_text:
        return None
    limit = int(scan_chars or get_settings().rfp_testing_unwrap_scan_chars)
    lines = body_text.splitlines()
    consumed = 0
    for index, raw in enumerate(lines):
        if consumed > limit:
            return None
        consumed += len(raw) + 1
        stripped = raw.strip()
        header = _HEADER_LINE_RE.match(raw)
        if any(r.match(stripped) for r in _MARKER_RES):
            marker, start = stripped.lower().strip("- :"), index + 1
        elif header is not None and header.group(1).lower() == "from":
            marker, start = "from", index
        else:
            continue
        block = _read_header_block(lines, start)
        if block is None:
            continue
        headers, end = block
        if "from" not in headers or not ({"subject", "sent", "date"} & set(headers)):
            continue
        note = "\n".join(lines[:index]).strip() or None
        body = "\n".join(lines[end:]).lstrip("\n").rstrip()
        return ForwardBlock(headers=headers, note=note, body=body, marker=marker)
    return None


def _read_header_block(lines: list[str], start: int) -> tuple[dict[str, str], int] | None:
    """Header lines from `start` (blank lines allowed before the first
    one, continuation lines folded), as ({name: value}, index after the
    block). None when no header line follows within a few lines."""
    headers: dict[str, str] = {}
    last: str | None = None
    index = start
    seen_header = False
    while index < len(lines) and index - start < _MAX_BLOCK_LINES:
        line = lines[index]
        match = _HEADER_LINE_RE.match(line)
        if match:
            name = match.group(1).lower()
            if name in headers:
                break  # a second From: is the next quoted message, not this block
            headers[name] = match.group(2).strip()
            last = name
            seen_header = True
        elif not line.strip():
            if seen_header:
                break
            if index - start > 3:
                return None
        elif seen_header and last and line[:1].isspace():
            headers[last] = (headers[last] + " " + line.strip()).strip()
        else:
            break
        index += 1
    if not seen_header:
        return None
    return headers, index


def _list_eml_candidates(attachments_meta: list[dict] | None) -> list[dict]:
    out = []
    for att in attachments_meta or []:
        if not isinstance(att, dict) or not att.get("id"):
            continue
        name = str(att.get("name") or "").strip().lower()
        ctype = str(att.get("contentType") or "").strip().lower()
        if att.get("kind") == "item" or name.endswith(".eml") or ctype == "message/rfc822":
            out.append(att)
    return out


def fetch_eml_bytes(mailbox: str, message_id: str, attachment_id: str, *, max_bytes: int) -> bytes:
    """The raw MIME of an attached message through `$value`, streamed under
    the cap (never more than one message in memory)."""
    path = f"/users/{mailbox}/messages/{message_id}/attachments/{attachment_id}/$value"
    chunks: list[bytes] = []
    size = 0
    with graph_stream("GET", path) as resp:
        for chunk in resp.iter_bytes(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                resp.close()
                raise ValueError("The attached message is larger than the per-file limit.")
            chunks.append(chunk)
    return b"".join(chunks)


def parse_eml(data: bytes) -> tuple[dict[str, str], str, list[dict]]:
    """Section 5.2: (headers, text body, parts) from a MIME message. The
    body prefers the plain part, else the HTML part converted to text;
    `parts` lists the file attachments in order (index, name,
    content_type, size) without holding their payloads."""
    from app.services.rfp_harvest import html_to_text

    msg = email_lib.message_from_bytes(data, policy=email_policy.default)
    headers = {
        "from": str(msg.get("From") or ""),
        "subject": str(msg.get("Subject") or ""),
        "date": str(msg.get("Date") or ""),
        "to": str(msg.get("To") or ""),
    }
    body = ""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:  # noqa: BLE001 - a malformed part: no body
        part = None
    if part is not None:
        try:
            content = part.get_content()
        except Exception:  # noqa: BLE001
            content = ""
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        body = str(content or "")
        if part.get_content_type() == "text/html":
            body = html_to_text(body, limit=200_000) or ""
    parts: list[dict] = []
    for index, att in enumerate(_iter_eml_attachments(msg)):
        if index >= _EML_PARTS_MAX:
            break
        try:
            payload = att.get_payload(decode=True) or b""
        except Exception:  # noqa: BLE001
            payload = b""
        parts.append(
            {
                "index": index,
                "name": str(att.get_filename() or f"part-{index}")[:200],
                "content_type": att.get_content_type(),
                "size": len(payload),
            }
        )
    return headers, body, parts


def _iter_eml_attachments(msg):
    try:
        yield from msg.iter_attachments()
    except Exception:  # noqa: BLE001 - a broken multipart yields what it can
        return


def eml_part_payload(data: bytes, index: int) -> tuple[str, bytes]:
    """(name, payload) of one attachment part of a MIME message, for the
    harvester's `eml:` locator."""
    msg = email_lib.message_from_bytes(data, policy=email_policy.default)
    for i, att in enumerate(_iter_eml_attachments(msg)):
        if i == index:
            return str(att.get_filename() or f"part-{index}"), att.get_payload(decode=True) or b""
    raise LookupError(f"The attached message has no part {index}.")


def unwrap_forward(
    row: dict, full_message: dict, attachments_meta: list[dict] | None, mailbox: str,
    graph_message_id: str,
) -> Unwrapped:
    """Section 5: the `.eml` / item attachment first (5.2), then the inline
    block (5.1), else `none` (the row proceeds with the forwarder as the
    sender and the page shows the warning chip)."""
    settings = get_settings()
    body = ((full_message.get("body") or {}).get("content")) or ""
    raw = Unwrapped(
        method=UNWRAP_NONE,
        from_address=row.get("from_address"),
        from_name=row.get("from_name"),
        subject=row.get("subject"),
        received_at=row.get("received_at"),
        body_text=body,
    )
    for att in _list_eml_candidates(attachments_meta):
        try:
            data = fetch_eml_bytes(
                mailbox, graph_message_id, str(att["id"]),
                max_bytes=int(settings.inbound_attachment_max_bytes),
            )
            headers, text, parts = parse_eml(data)
        except Exception:  # noqa: BLE001 - fall through to the inline block
            logger.warning("rfp test: attached message %s could not be read", att.get("id"), exc_info=True)
            continue
        name, address = parse_address(headers.get("from"))
        if not address:
            continue
        date = parse_forward_date(headers.get("date"))
        return Unwrapped(
            method=UNWRAP_EML,
            from_address=address,
            from_name=name,
            subject=strip_forward_prefixes(headers.get("subject")) or raw.subject,
            received_at=_iso(date) if date else raw.received_at,
            body_text=text,
            forwarder_note=(body.strip() or None),
            eml_attachment_id=str(att["id"]),
            eml_parts=parts,
        )
    block = parse_inline_forward(body, settings.rfp_testing_unwrap_scan_chars)
    if block is None:
        return raw
    name, address = parse_address(block.headers.get("from"))
    if not address:
        return raw
    date = parse_forward_date(block.headers.get("sent") or block.headers.get("date"))
    return Unwrapped(
        method=UNWRAP_INLINE,
        from_address=address,
        from_name=name,
        subject=strip_forward_prefixes(block.headers.get("subject")) or strip_forward_prefixes(raw.subject),
        received_at=_iso(date) if date else raw.received_at,
        body_text=block.body,
        forwarder_note=block.note,
    )


def unwrap_fields(row: dict, unwrapped: Unwrapped) -> dict:
    """The rfp_emails columns the fetch step's CAS writes for a test row
    (section 5.3): the effective values plus `forward_meta`."""
    meta = {
        "unwrap": unwrapped.method,
        "raw_from_address": row.get("from_address"),
        "raw_from_name": row.get("from_name"),
        "raw_subject": row.get("subject"),
        "raw_received_at": row.get("received_at"),
        "forwarder_note": (unwrapped.forwarder_note or "")[:PREVIEW_MAX_CHARS] or None,
    }
    if unwrapped.method == UNWRAP_EML:
        meta["eml_attachment_id"] = unwrapped.eml_attachment_id
        meta["eml_parts"] = list(unwrapped.eml_parts)
    fields = {
        "from_address": (unwrapped.from_address or row.get("from_address") or "").strip().lower(),
        "from_name": unwrapped.from_name,
        "subject": unwrapped.subject,
        "received_at": unwrapped.received_at or row.get("received_at"),
        "body_text": unwrapped.body_text,
        "body_preview": (unwrapped.body_text or "")[:255] or None,
        "forward_meta": meta,
    }
    if unwrapped.method == UNWRAP_EML:
        # The harvester lists the parts instead of the forward's listing.
        fields["attachments_meta"] = [
            {
                "name": p.get("name"),
                "contentType": p.get("content_type"),
                "size": p.get("size"),
                "kind": "file",
                "inline": False,
            }
            for p in unwrapped.eml_parts
        ]
        fields["has_attachments"] = bool(unwrapped.eml_parts)
    return fields


# ── Outbound redirect (section 6) ────────────────────────────────────────────


@dataclass
class Redirected:
    """A rewritten message and what the `mail_out.redirected` event records."""

    message: dict
    who: str
    detail: dict


def _address_of(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    address = ((entry.get("emailAddress") or {}).get("address")) or ""
    return address.strip() or None


def _addresses(entries: Any) -> list[str]:
    out = []
    for entry in entries or []:
        address = _address_of(entry)
        if address:
            out.append(address)
    return out


def _fetch_in(sb, table: str, select: str, column: str, values: list[str]) -> list[dict]:
    rows: list[dict] = []
    for chunk in _chunks(values):
        rows += (sb.table(table).select(select).in_(column, chunk).execute()).data or []
    return rows


def resolve_recipients(sb, addresses: list[str]) -> dict[str, str]:
    """address (lowercased) -> display: a profile ("Jane Doe, Executive"),
    then a GC contact ("Meridian Builders: Sam Lee, GC contact"), then a
    vendor contact ("Graybar: Pat Q, vendor contact"), else "(unknown)".
    One query per table, addresses batched."""
    wanted = sorted({a.strip().lower() for a in addresses if a and a.strip()})
    if not wanted:
        return {}
    # Stored addresses may carry capitals; ask for both spellings.
    lookup = sorted({*wanted, *(a for a in addresses if a)})
    out: dict[str, str] = {}
    for p in _fetch_in(sb, "profiles", "full_name, email, role", "email", lookup):
        key = (p.get("email") or "").strip().lower()
        if key and key not in out:
            role = _ROLE_LABELS.get(str(p.get("role") or ""), str(p.get("role") or "")).strip()
            out[key] = ", ".join(x for x in ((p.get("full_name") or "").strip(), role) if x)
    contacts = _fetch_in(sb, "gc_contacts", "name, email, gc_id", "email", lookup)
    gc_names: dict[str, str] = {}
    gc_ids = sorted({c["gc_id"] for c in contacts if c.get("gc_id")})
    if gc_ids:
        for g in _fetch_in(sb, "general_contractors", "id, name", "id", gc_ids):
            gc_names[g["id"]] = g.get("name") or ""
    for c in contacts:
        key = (c.get("email") or "").strip().lower()
        if key and key not in out:
            org = gc_names.get(c.get("gc_id"), "")
            out[key] = f"{org}: {c.get('name') or ''}, GC contact".lstrip(": ").strip()
    vendors_c = _fetch_in(sb, "vendor_contacts", "name, email, vendor_id", "email", lookup)
    vendor_names: dict[str, str] = {}
    vendor_ids = sorted({c["vendor_id"] for c in vendors_c if c.get("vendor_id")})
    if vendor_ids:
        for v in _fetch_in(sb, "vendors", "id, name", "id", vendor_ids):
            vendor_names[v["id"]] = v.get("name") or ""
    for c in vendors_c:
        key = (c.get("email") or "").strip().lower()
        if key and key not in out:
            org = vendor_names.get(c.get("vendor_id"), "")
            out[key] = f"{org}: {c.get('name') or ''}, vendor contact".lstrip(": ").strip()
    return out


def display_for(address: str, resolved: dict[str, str]) -> str:
    label = resolved.get(address.strip().lower())
    return f"{label} <{address}>" if label else f"{address} (unknown)"


def to_plain_text(content: Any, content_type: Any) -> str:
    """The body as text: HTML through `rfp_harvest.html_to_text` (block
    ends become line breaks, entities decoded, image alt text and tags
    dropped, three or more blank lines collapsed to two)."""
    text = str(content or "")
    if str(content_type or "").strip().lower() == "html":
        from app.services.rfp_harvest import html_to_text

        return html_to_text(text, limit=500_000) or ""
    return text


def _size_label(size: Any) -> str:
    try:
        n = int(size or 0)
    except (TypeError, ValueError):
        return "?"
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def _attachment_size(att: dict) -> int:
    if att.get("size") is not None:
        try:
            return int(att["size"])
        except (TypeError, ValueError):
            return 0
    encoded = att.get("contentBytes") or ""
    return (len(encoded) * 3) // 4  # base64 to bytes, close enough for a label


def _session_label(session: dict) -> str:
    started = _parse_ts(session.get("started_at"))
    when = started.astimezone(_COMPANY_TZ).strftime("%b %-d, %Y %-I:%M %p PT") if started else "?"
    name = (session.get("name") or "").strip()
    return f'session "{name}", started {when}' if name else f"session started {when}"


def build_header(
    *, session: dict, intended: list[tuple[str, str]], sender_mailbox: str | None,
    project_label: str | None, attachments: list[dict],
) -> str:
    """The plain-text block above the converted body (section 6)."""
    lines = [f"BDR TEST MODE  ({_session_label(session)})", "This email was going to:"]
    for kind, display in intended:
        lines.append(f"  {kind}:  {display}")
    if not intended:
        lines.append("  (no recipients)")
    lines.append(f"From mailbox: {sender_mailbox or '?'}")
    if project_label:
        lines.append(f"Project: {project_label}")
    if attachments:
        kept = ", ".join(f"{a.get('name') or 'attachment'} ({_size_label(a.get('size'))})" for a in attachments)
        lines.append(f"Attachments kept: {kept}")
    lines.append(_HEADER_RULE)
    return "\n".join(lines)


def _project_label(sb, project_id: str | None) -> str | None:
    if not project_id:
        return None
    try:
        rows = sb.table("projects").select("number, name").eq("id", project_id).limit(1).execute().data or []
    except Exception:  # noqa: BLE001 - the label is a courtesy
        return None
    if not rows:
        return None
    return f"{rows[0].get('number') or ''} {rows[0].get('name') or ''}".strip() or None


def redirect(
    message: dict, *, sb, session: dict, context: dict | None = None, sender_mailbox: str | None = None,
) -> Redirected:
    """Rewrite a Graph message dict IN PLACE for the redirect (section 6):
    every recipient replaced by the session's redirect address, the
    subject prefixed with the intended recipient, the body converted to
    text under the header, inline images dropped, file attachments kept.
    Raises RedirectRefused (and records `mail_out.refused`) when the
    redirect address is empty: fail closed, never the real recipients."""
    context = context or {}
    redirect_to = (session.get("redirect_to") or "").strip()
    if not redirect_to:
        record(
            sb, session_id=session.get("id"), source=SOURCE_MAIL_OUT, kind="refused",
            level=LEVEL_ERROR,
            title=f"Send refused: the session has no redirect address ({message.get('subject') or ''})",
            detail={"subject": message.get("subject"), "to": _addresses(message.get("toRecipients")),
                    "cc": _addresses(message.get("ccRecipients")),
                    "project_id": context.get("project_id"), "rfq_id": context.get("rfq_id")},
            project_id=context.get("project_id"),
        )
        raise RedirectRefused(
            "Test mode is active but the redirect address is empty; the email was not sent."
        )

    to_list = _addresses(message.get("toRecipients"))
    cc_list = _addresses(message.get("ccRecipients"))
    bcc_list = _addresses(message.get("bccRecipients"))
    resolved = resolve_recipients(sb, to_list + cc_list + bcc_list)
    intended = [("To", display_for(a, resolved)) for a in to_list]
    intended += [("CC", display_for(a, resolved)) for a in cc_list]
    intended += [("BCC", display_for(a, resolved)) for a in bcc_list]

    # "[TEST for Jane Doe +2]": the first intended To (else CC) by name when
    # known (the label without its role / kind suffix), else the address.
    first = to_list[0] if to_list else (cc_list[0] if cc_list else None)
    label = resolved.get(first.lower()) if first else None
    who = label.rsplit(", ", 1)[0] if label else (first or "nobody")
    extra = (len(to_list) if to_list else len(cc_list)) - 1
    if extra > 0:
        who = f"{who} +{extra}"

    attachments = [a for a in (message.get("attachments") or []) if isinstance(a, dict)]
    kept = [a for a in attachments if not a.get("isInline")]
    kept_meta = [{"name": a.get("name"), "size": _attachment_size(a)} for a in kept]
    dropped = [a.get("name") for a in attachments if a.get("isInline")]

    body = message.get("body") or {}
    plain = to_plain_text(body.get("content"), body.get("contentType"))
    project_label = context.get("project_label") or _project_label(sb, context.get("project_id"))
    header = build_header(
        session=session, intended=intended, sender_mailbox=sender_mailbox,
        project_label=project_label, attachments=kept_meta,
    )
    original_subject = message.get("subject") or ""
    message["subject"] = f"[TEST for {who}] {original_subject}".rstrip()
    message["toRecipients"] = [{"emailAddress": {"address": redirect_to}}]
    message["ccRecipients"] = []
    message["bccRecipients"] = []
    message["body"] = {"contentType": "Text", "content": header + "\n" + plain}
    if attachments:
        message["attachments"] = kept
    detail = {
        "to": [{"address": a, "display": display_for(a, resolved)} for a in to_list],
        "cc": [{"address": a, "display": display_for(a, resolved)} for a in cc_list],
        "bcc": [{"address": a, "display": display_for(a, resolved)} for a in bcc_list],
        "subject": original_subject,
        "redirected_to": redirect_to,
        "redirected_subject": message["subject"],
        "sender_mailbox": sender_mailbox,
        "project_id": context.get("project_id"),
        "project_label": project_label,
        "rfq_id": context.get("rfq_id"),
        "attachments": kept_meta,
        "inline_dropped": dropped,
        "preview": plain[:PREVIEW_MAX_CHARS],
    }
    return Redirected(message=message, who=who, detail=detail)


def record_redirected(
    sb, session: dict, redirected: Redirected, *, email_log_id: str | None, status: str,
    error: str | None = None,
) -> None:
    """The `mail_out.redirected` event after the send (section 6)."""
    detail = {**redirected.detail, "email_log_id": email_log_id, "status": status}
    if error:
        detail["error"] = str(error)[:500]
    record(
        sb, session_id=session.get("id"), source=SOURCE_MAIL_OUT, kind="redirected",
        level=LEVEL_INFO if status == "sent" else LEVEL_WARN,
        title=f"Redirected to {redirected.detail.get('redirected_to')}: "
              f"{redirected.detail.get('redirected_subject') or ''}",
        detail=detail,
        project_id=redirected.detail.get("project_id"),
    )


# ── Step strip (section 8) ───────────────────────────────────────────────────


def step_chips(row: dict, events: list[dict] | None = None) -> list[dict]:
    """The ten chips of an email's step strip from its status (the source of
    truth) with the id of the latest event about each step attached, so the
    page can open it. States: done | current | waiting | skipped | failed |
    pending. A pending row parked behind next_attempt_at (a retry, a model
    away, a harvest job running) is `waiting`; a human lane is `waiting` on
    the chip the person decides; the terminal refusals are `failed` at the
    step that decided; merged / duplicate skip harvest and create; a row
    that never harvested shows harvest `skipped`."""
    status = str(row.get("status") or "")
    decided = str(row.get("decided_at_step") or "")
    decided = _STEP_ALIASES.get(decided, decided)
    states = {step: STEP_PENDING for step in STEPS}

    def mark(step: str, state: str) -> None:
        for s in STEPS:
            if s == step:
                states[s] = state
                return
            states[s] = STEP_DONE

    if status in STEPS:
        mark(status, STEP_WAITING if row.get("next_attempt_at") else STEP_CURRENT)
    elif status in _STATUS_WAITING_STEP:
        mark(_STATUS_WAITING_STEP[status], STEP_WAITING)
    elif status in _STATUS_FAILED_STEP:
        mark(_STATUS_FAILED_STEP[status], STEP_FAILED)
    elif status in ("rejected_by_review", "failed"):
        mark(decided if decided in STEPS else "received", STEP_FAILED)
    elif status in ("merged", "duplicate"):
        mark("match", STEP_DONE)
        states["harvest"] = STEP_SKIPPED
        states["split"] = STEP_SKIPPED
        states["create"] = STEP_SKIPPED
    elif status in ("created", "done"):
        mark("create", STEP_DONE)
        if not row.get("harvest_id"):
            states["harvest"] = STEP_SKIPPED
            states["split"] = STEP_SKIPPED
        elif row.get("split_status") in ("skipped", None) and "split_status" in row:
            states["split"] = STEP_SKIPPED
    latest: dict[str, int] = {}
    for ev in events or []:
        step = _event_step(ev)
        if step:
            latest[step] = max(int(ev.get("id") or 0), latest.get(step, 0))
    out = []
    for step in STEPS:
        chip = {"step": step, "state": states[step]}
        if step in latest:
            chip["event_id"] = latest[step]
        out.append(chip)
    return out


_EVENT_KIND_STEP = {
    "listed": "received", "fetched": "received", "listing_skip": "received",
    "auth": "auth", "keywords": "keywords", "classify": "classify", "authorize": "authorize",
    "method": "method", "extract": "extract", "match": "match",
}


def _event_step(ev: dict) -> str | None:
    source, kind = ev.get("source"), str(ev.get("kind") or "")
    if source == SOURCE_HARVEST:
        return "harvest"
    if source == SOURCE_SPLIT:
        return "split"
    if source == SOURCE_CREATE:
        return "create"
    if source != SOURCE_INTAKE:
        return None
    if kind in _EVENT_KIND_STEP:
        return _EVENT_KIND_STEP[kind]
    step = str((ev.get("detail") or {}).get("step") or "")
    step = _STEP_ALIASES.get(step, step)
    return step if step in STEPS else None


# ── Cleanup (section 9) ──────────────────────────────────────────────────────


def _count_delete(sb, table: str, session_id: str) -> int:
    """Delete the table's rows tagged with the session; how many went."""
    return len((sb.table(table).delete().eq("test_session_id", session_id).execute()).data or [])


def cleanup(sb, session_id: str, *, actor_id: str | None) -> dict:
    """Delete everything an ENDED session produced, in dependency order,
    and write the report on the session row. Never touches an untagged
    row; never runs on the active session (409). Sync: the router runs it
    in its threadpool."""
    from app.services import rfp_ingest, storage

    session = get_session(sb, session_id)
    if session is None:
        raise LookupError("Test session not found")
    if session.get("status") == STATUS_ACTIVE:
        raise RfpTestError(CODE_SESSION_ACTIVE, status=409)
    sb.table(TABLE_SESSIONS).update({"cleanup_started_at": _iso(_now())}).eq("id", session_id).execute()
    record(sb, session_id=session_id, source=SOURCE_SESSION, kind="cleanup_started",
           title="Cleanup started", detail={"by": actor_id})
    deleted = {
        "projects": 0, "gcs": 0, "gc_contacts": 0, "split_jobs": 0, "harvests": 0, "ingest_runs": 0,
        "rfp_emails": 0, "ingested_emails": 0, "email_log": 0,
    }
    kept: list[dict] = []
    errors: list[str] = []

    def fail(step: str, exc: Exception) -> None:
        logger.exception("rfp test: cleanup %s failed for session %s", step, session_id)
        errors.append(f"{step}: {str(exc)[:300]}")

    # 1. Projects (the FK cascades carry the children, as discard relies on);
    #    their email_log rows first, since that FK only sets null.
    try:
        projects = (
            sb.table("projects").select("id, number").eq("test_session_id", session_id).execute()
        ).data or []
        project_ids = [p["id"] for p in projects]
        for chunk in _chunks(project_ids):
            rows = sb.table("email_log").delete().in_("project_id", chunk).execute().data or []
            deleted["email_log"] += len(rows)
        for project in projects:
            rows = (
                sb.table("projects").delete().eq("id", project["id"]).eq("test_session_id", session_id).execute()
            ).data or []
            if not rows:
                continue
            deleted["projects"] += 1
            try:
                storage.delete_project_prefix(project["id"])
            except Exception as exc:  # noqa: BLE001
                fail(f"storage {project.get('number')}", exc)
        _count_delete(sb, "rfp_created_projects", session_id)
    except Exception as exc:  # noqa: BLE001
        fail("projects", exc)

    # 2. GCs and contacts the session inserted, unless another project still
    #    references them.
    try:
        contacts = (
            sb.table("gc_contacts").select("id, gc_id, name").eq("test_session_id", session_id).execute()
        ).data or []
        for contact in contacts:
            links = (
                sb.table("project_gc_contacts").select("id").eq("gc_contact_id", contact["id"]).limit(1).execute()
            ).data or []
            if links:
                kept.append({"table": "gc_contacts", "id": contact["id"], "why": "selected on another project"})
                continue
            rows = (
                sb.table("gc_contacts").delete().eq("id", contact["id"]).eq("test_session_id", session_id).execute()
            ).data or []
            deleted["gc_contacts"] += len(rows)
        gcs = (
            sb.table("general_contractors").select("id, name").eq("test_session_id", session_id).execute()
        ).data or []
        for gc in gcs:
            links = sb.table("project_gcs").select("id").eq("gc_id", gc["id"]).limit(1).execute().data or []
            if links:
                kept.append({"table": "general_contractors", "id": gc["id"], "why": "linked to another project"})
                continue
            rows = (
                sb.table("general_contractors").delete().eq("id", gc["id"]).eq("test_session_id", session_id).execute()
            ).data or []
            deleted["gcs"] += len(rows)
    except Exception as exc:  # noqa: BLE001
        fail("gcs", exc)

    # 3a. The Bid File Splitter jobs the split step staged from the
    #     session's harvests (source rfp): their storage prefix, then the
    #     rows (files and segments cascade). docs/RFP_SPLIT.md 7.
    try:
        tagged = (
            sb.table("rfp_harvests").select("id").eq("test_session_id", session_id).execute()
        ).data or []
        for chunk in _chunks([h["id"] for h in tagged]):
            jobs = (
                sb.table("bid_split_jobs").select("id").eq("source", "rfp").in_("rfp_harvest_id", chunk).execute()
            ).data or []
            for job in jobs:
                try:
                    storage.delete_bid_split_prefix(job["id"])
                except Exception as exc:  # noqa: BLE001
                    fail(f"split storage {job['id']}", exc)
                rows = sb.table("bid_split_jobs").delete().eq("id", job["id"]).eq("source", "rfp").execute().data or []
                deleted["split_jobs"] += len(rows)
    except Exception as exc:  # noqa: BLE001
        fail("split_jobs", exc)

    # 3. Harvests and their sandbox runs (the run delete removes both
    #    storage prefixes; a run the sandbox refuses to delete is kept).
    try:
        runs = (
            sb.table("rfp_ingest_runs").select("id, status").eq("test_session_id", session_id).execute()
        ).data or []
        harvests = (
            sb.table("rfp_harvests").select("id, sandbox_run_id").eq("test_session_id", session_id).execute()
        ).data or []
        run_ids = {r["id"] for r in runs} | {h["sandbox_run_id"] for h in harvests if h.get("sandbox_run_id")}
        for run_id in sorted(run_ids):
            try:
                rfp_ingest.delete_run(run_id)
                deleted["ingest_runs"] += 1
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                kept.append({"table": "rfp_ingest_runs", "id": run_id, "why": f"storage objects kept: {str(exc)[:200]}"})
        # A run the sandbox refused keeps its row (reported above); the
        # harvest rows go whatever happened to their runs.
        deleted["harvests"] += _count_delete(sb, "rfp_harvests", session_id)
    except Exception as exc:  # noqa: BLE001
        fail("harvests", exc)

    # 4. The intake rows (sightings, training rows and match rows cascade).
    try:
        deleted["rfp_emails"] += _count_delete(sb, "rfp_emails", session_id)
    except Exception as exc:  # noqa: BLE001
        fail("rfp_emails", exc)

    # 5. The filer rows and their stored attachments.
    try:
        emails = (
            sb.table("ingested_emails").select("id").eq("test_session_id", session_id).execute()
        ).data or []
        ids = [e["id"] for e in emails]
        for chunk in _chunks(ids):
            atts = (
                sb.table("ingested_email_attachments").select("storage_path").in_("email_id", chunk).execute()
            ).data or []
            for att in atts:
                if att.get("storage_path"):
                    try:
                        storage.delete_file(att["storage_path"])
                    except Exception as exc:  # noqa: BLE001
                        fail(f"attachment {att['storage_path']}", exc)
        deleted["ingested_emails"] += _count_delete(sb, "ingested_emails", session_id)
    except Exception as exc:  # noqa: BLE001
        fail("ingested_emails", exc)

    # 6. Redirected sends (the rows the senders tagged).
    try:
        deleted["email_log"] += _count_delete(sb, "email_log", session_id)
    except Exception as exc:  # noqa: BLE001
        fail("email_log", exc)

    # 7. notifications.project_id cascades with the project rows.
    report = {"deleted": deleted, "kept": kept, "errors": errors,
              "notes": ["notifications cascade with their projects"]}
    sb.table(TABLE_SESSIONS).update(
        {"cleanup_finished_at": _iso(_now()), "cleanup_report": report}
    ).eq("id", session_id).execute()
    record(sb, session_id=session_id, source=SOURCE_SESSION, kind="cleanup_finished",
           title="Cleanup finished: " + ", ".join(f"{k} {v}" for k, v in deleted.items() if v),
           level=LEVEL_WARN if errors else LEVEL_INFO, detail=report)
    return report

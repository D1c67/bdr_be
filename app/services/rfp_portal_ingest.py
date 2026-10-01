"""RFP Ingestion: NGEM portal invitations (docs/RFP_NGEM_PORTAL.md).

NGEM (the Nevada Government eMarketplace, an Ionwave supplier portal) sends
no usable invitation mail, so this module logs into the company's supplier
account on a schedule, reads the "My Invitations" grid, decides with the
shared matcher (services/rfp_match) whether each open invitation is already
a project, and for the new ones pulls the notes and the bid attachments into
an Ingestion Sandbox run. Its own tables (rfp_portal_runs,
rfp_portal_invitations), its own scheduler and sweep, its own two queue job
types; the harvest record (rfp_harvests), the session store
(rfp_harvest_sessions, provider `ngem`), the sandbox and the queue are shared
with the email pipeline.

  scheduler (every RFP_NGEM_POLL_SECONDS, every worker)      Run now (route)
     | claim rfp_portal_runs(portal, slot)                       | manual run
     v                                                           v
   job rfp_portal_scan(run_id)    log in if needed, page the grid, upsert
     |                            invitations, detect changes, close the run
     v  invitation status
   match         sweep step: candidates from the shared reference bundle,
     |           the LLM verdict when something clears the review threshold
     |             confident + auto-resolve on   -> exists      (terminal)
     |             confident, switch off / uncertain / unusable -> review_match
     |             nothing above the threshold  -> harvest
     v
   harvest       sweep step: make sure an rfp_portal_harvest job exists
     v  job rfp_portal_harvest(invitation_id)
   create        facts on the rfp_harvests row first, then the files into a
     |           sandbox run; then the creation step (docs/RFP_CREATE.md 3):
     |             RFP_CREATE_AUTO_ENABLED off -> done (the button acts there)
     v             on -> rfp_create.create_from_portal -> created
   created / done

Rules this module keeps on its own: the sweep and every human action CAS on
(id, status); every write that moves a row onto a new pending step resets
attempts, last_error and next_attempt_at; no sweep step ever calls the
portal (only the two jobs do); the model is touched only inside the match
step and only when a candidate clears the review threshold; the password
never reaches a log line, an error message or a row; view_url and download
URLs never leave the backend.

The portal client (services/ngem_client, pure httpx) is reached through the
`PORTALS` registry, so a second portal is one more entry and the tests stub
the whole session through the same seam.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
import nh3

from app.core.config import Settings, get_settings
from app.core.redact import redact_text
from app.core.roles import RFP_REVIEW_ROLES, Role
from app.core.supabase_client import get_supabase
from app.services import (
    bc_facts,
    llm,
    llm_errors,
    llm_health,
    llm_queue,
    rfp_create,
    rfp_email_ingest,
    rfp_harvest,
    rfp_ingest,
    rfp_match,
    rfp_split,
    rfp_test,
)
from app.services.notifications import audit, notify_role, notify_user

logger = logging.getLogger(__name__)

COMPANY_TZ = ZoneInfo("America/Los_Angeles")

PORTAL_NGEM = "ngem"
# The BuildingConnected Bid Board (docs/RFP_BUILDINGCONNECTED.md): the same
# tables, sweep and human actions; its own scan, matcher hooks and lanes in
# services/rfp_bc_portal, reached through the PORTALS registry below.
PORTAL_BUILDINGCONNECTED = "buildingconnected"
INVITATIONS_TABLE = "rfp_portal_invitations"
RUNS_TABLE = "rfp_portal_runs"
HARVESTS_TABLE = "rfp_harvests"
SESSIONS_TABLE = "rfp_harvest_sessions"

JOB_SCAN = "rfp_portal_scan"
JOB_HARVEST = "rfp_portal_harvest"
MODEL_LABEL = "ngem"
LEASE_KEY = "rfp_portal_sweep"
AUDIT_ENTITY = "rfp_portal_invitation"
AUDIT_RUN_ENTITY = "rfp_portal_run"

NOTIFY_LOGIN_FAILED = "rfp_ngem.login_failed"
NOTIFY_NEW_INVITATIONS = "rfp_ngem.new_invitations"
NOTIFY_INVITATION_CHANGED = "rfp_ngem.invitation_changed"
NOTIFY_SCAN_FAILED = "rfp_ngem.scan_failed"

STATUS_MATCH = "match"
STATUS_HARVEST = "harvest"
STATUS_SPLIT = "split"
STATUS_CREATE = "create"
STATUS_REVIEW_MATCH = "review_match"
STATUS_EXISTS = "exists"
STATUS_DONE = "done"
STATUS_CREATED = "created"
STATUS_IGNORED = "ignored"
# The parked statuses (0136, docs/RFP_BUILDINGCONNECTED.md 3.3 and 3.9):
# mirror-only rows the sweep never touches. `historical` is a row that did
# not pass the entry rule at insert; `expired` a pipeline row past its due
# date; `withdrawn` one the board no longer lists. `restore` moves any of
# them back to `match`.
STATUS_HISTORICAL = "historical"
STATUS_EXPIRED = "expired"
STATUS_WITHDRAWN = "withdrawn"
STATUS_PENDING = (STATUS_MATCH, STATUS_HARVEST, STATUS_SPLIT, STATUS_CREATE)
STATUS_HUMAN = (STATUS_REVIEW_MATCH,)
# `done` keeps meaning "harvested, no project" (where the Create project
# button acts); `created` is the creation step's exit (docs/RFP_CREATE.md 3).
STATUS_TERMINAL = (STATUS_EXISTS, STATUS_DONE, STATUS_CREATED, STATUS_IGNORED)
STATUS_PARKED = (STATUS_HISTORICAL, STATUS_EXPIRED, STATUS_WITHDRAWN)
ALL_STATUSES = STATUS_PENDING + STATUS_HUMAN + STATUS_TERMINAL + STATUS_PARKED
# A person may ignore a row at every pending or human status before it is
# created (a row waiting at `create` for the flag, a claim or a retry
# included); `created` and the other terminals refuse.
IGNORABLE_STATUSES = (STATUS_MATCH, STATUS_REVIEW_MATCH, STATUS_HARVEST, STATUS_SPLIT, STATUS_CREATE)
HARVEST_AGAIN_STATUSES = (STATUS_DONE, STATUS_HARVEST)

RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_COMPLETE = "complete"
RUN_FAILED = "failed"
RUN_ACTIVE = (RUN_QUEUED, RUN_RUNNING)
TRIGGER_SCHEDULED = "scheduled"
TRIGGER_MANUAL = "manual"
# rfp_portal_runs.kind (0136): what a scan job does. NGEM runs are always
# incremental; BuildingConnected adds the nightly full sync (BUILD_CONTRACT D2).
RUN_KIND_INCREMENTAL = "incremental"
RUN_KIND_FULL = "full"

# The list views of the NGEM tab (doc section 5).
VIEWS: dict[str, tuple[str, ...]] = {
    "review": (STATUS_REVIEW_MATCH,),
    "new": (STATUS_MATCH, STATUS_HARVEST, STATUS_SPLIT, STATUS_CREATE, STATUS_DONE),
    # A row the creation step turned into a project has one now, like a
    # row resolved to an existing project; its card links the project.
    "existing": (STATUS_EXISTS, STATUS_CREATED),
    "ignored": (STATUS_IGNORED,),
    "all": ALL_STATUSES,
}

# The BuildingConnected lanes (BUILD_CONTRACT D21): needs_action is what a
# person must decide; open is the pipeline (match, create) plus done (no
# project yet, the Create button acts); existing is any row with a project.
BC_VIEWS: dict[str, tuple[str, ...]] = {
    "needs_action": (STATUS_REVIEW_MATCH,),
    "open": (STATUS_MATCH, STATUS_CREATE, STATUS_DONE),
    "existing": (STATUS_EXISTS, STATUS_CREATED),
    "historical": (STATUS_HISTORICAL,),
    "expired_withdrawn": (STATUS_EXPIRED, STATUS_WITHDRAWN),
    "ignored": (STATUS_IGNORED,),
    "all": ALL_STATUSES,
}
VIEWS_BY_PORTAL: dict[str, dict[str, tuple[str, ...]]] = {
    PORTAL_NGEM: VIEWS,
    PORTAL_BUILDINGCONNECTED: BC_VIEWS,
}

# Error codes carried by RfpPortalError; the values equal the ErrorCode
# members of the same name in app/core/error_codes.
CODE_NOT_ACTIONABLE = "rfp_portal_not_actionable"
CODE_PROJECT_REQUIRED = "rfp_portal_project_required"
CODE_HARVEST_ACTIVE = "rfp_portal_harvest_active"
CODE_RUN_ACTIVE = "rfp_portal_run_active"
CODE_LOCKED = "rfp_portal_locked"
CODE_NOT_AVAILABLE = "rfp_portal_not_available"
CODE_REASON_INVALID = "rfp_portal_reason_invalid"

FLAG_NO_PROJECT_NAME = rfp_match.REASON_NO_PROJECT_NAME
FLAG_MATCH_FAILED = "match_failed"
FLAG_MATCH_LLM_UNUSABLE = rfp_match.REASON_LLM_UNUSABLE
FLAG_STALE_LINK = "stale_link"

FILE_ACCEPTED = rfp_harvest.FILE_ACCEPTED
FILE_REJECTED = rfp_harvest.FILE_REJECTED
FILE_TOO_LARGE = rfp_harvest.FILE_TOO_LARGE
FILE_DOWNLOAD_FAILED = rfp_harvest.FILE_DOWNLOAD_FAILED
FILE_SKIPPED_CAP = rfp_harvest.FILE_SKIPPED_CAP

HARVEST_PENDING = rfp_harvest.STATUS_PENDING
HARVEST_RUNNING = rfp_harvest.STATUS_RUNNING
HARVEST_COMPLETE = rfp_harvest.STATUS_COMPLETE
HARVEST_FAILED = rfp_harvest.STATUS_FAILED

# Failure kinds the portal adapter maps the client's exceptions onto.
KIND_LOCKED = "locked"
KIND_UNAVAILABLE = "unavailable"
KIND_TRANSIENT = "transient"
KIND_PERMANENT = "permanent"
KIND_UNKNOWN = "unknown"

_CHANGE_LOG_CAP = 50
_CHANGED_RECENTLY_DAYS = 7
_TRACKED_FIELDS = ("title", "close_at", "addendum_no", "bid_number_raw")
_FIELD_LABELS = {
    "title": "title",
    "close_at": "close date",
    "addendum_no": "addendum",
    "bid_number_raw": "bid number",
}
_RENEW_EVERY = 20
_DOWNLOAD_ATTEMPTS = 3
_PAGE = 1000
_IN_CHUNK = 200
_ERROR_MAX_CHARS = 500
_TITLE_MAX_CHARS = 500
_AGENCY_MAX_CHARS = 200
_NUMBER_MAX_CHARS = 200
_SHORT_MAX_CHARS = 100
_CONTACT_MAX_CHARS = 300
_NOTES_HTML_MAX_CHARS = 20_000
_FILENAME_MAX_CHARS = 200
_DESCRIPTION_MAX_CHARS = 500
_REASON_MIN_CHARS = 3
_REASON_MAX_CHARS = 500
_QUERY_MAX_CHARS = 80
_MATCH_MAX_TOKENS = 800
_LADDER_CAP_WAIT_SECONDS = 3600
_TOUCH_EVERY_SECONDS = 60.0
_LIST_MAX = 200
# Bell text: the portal writes every value, so each one is capped before it
# reaches a bell row (a change that swapped a 2,000-character title in and
# out reproduced all of it, twice).
_BELL_VALUE_MAX_CHARS = 80
_BELL_TITLE_MAX_CHARS = 120
_BELL_MESSAGE_MAX_CHARS = 600
_UNKNOWN_AGENCY = "Unknown agency"
# A stored file entry's keys (doc section 4): the detail hands back these and
# nothing else, so a download URL can never ride along even if a row carried one.
_PUBLIC_FILE_KEYS = ("index", "file_name", "description", "size", "sandbox_file_id", "status", "error")

# The notes fragment shown in the app: a small allowlist, https links only.
_NOTES_TAGS = {"p", "br", "b", "strong", "i", "em", "u", "ul", "ol", "li", "a"}
_NOTES_ATTRIBUTES = {"a": {"href"}}
_NOTES_URL_SCHEMES = {"https"}

_RESET = {"attempts": 0, "last_error": None, "next_attempt_at": None}

_MSG_NOT_CONFIGURED = "The NGEM supplier account is not configured."
_MSG_INTERRUPTED = "The portal request was interrupted; it will be retried."
_MSG_CLAIMED = "Another harvest of the same invitation is running; waiting for it."
_MSG_TOO_MANY_FILES = "The bid has more attachments than the harvest accepts ({n} of {cap})."
_MSG_TOO_MANY_BYTES = (
    "The bid's attachments are larger than the harvest accepts ({mb} MB of {cap} MB)."
)
_MSG_NO_FILES = "The bid has no attachments to download."
_MSG_STALE_LINK = "The invitation's portal link is stale and the grid no longer lists it."
_MSG_STALE_LINK_AGAIN = "The invitation's portal link answered stale again after a refresh from the grid."
_MSG_RUN_NOT_FOUND = "Scan run not found."
_MSG_RUN_NOT_RUNNABLE = "The scan run has already failed; start a new one."
_MSG_RUN_ACTIVE = "A scan is already queued or running."
_MSG_RUN_LOST = "The scan job was lost; run it again."
_MSG_RUN_NOT_RECORDED = "The scan run could not be recorded; try again."
_MSG_QUEUE_OFF = "The NGEM scan cannot run: the job queue is off or the slice is inactive."
_MSG_SCAN_NOT_QUEUED = "The scan could not be queued: {error}"
_MSG_INVITATION_NOT_FOUND = "Invitation not found."
_MSG_NOT_ACTIONABLE = "This invitation is not waiting for that decision."
_MSG_RACED = "Someone else decided this invitation first."
_MSG_HARVEST_ACTIVE = "A harvest for this invitation is already queued or running."
_MSG_NO_DOWNLOAD_LINK = "The portal gave no download link for this file."
_MSG_LARGER_THAN_SANDBOX = "Larger than the sandbox accepts."

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_QUERY_CLEAN_RE = re.compile(r"[^\w\s\-./#&']", re.UNICODE)
_FILENAME_CLEAN_RE = re.compile(r"[\x00-\x1f\x7f\"<>|:?*\\/]+")


# ── Errors ───────────────────────────────────────────────────────────────────


class RfpPortalError(LookupError):
    """A refused human action. `code` is the X-Error-Code the router sends
    (409, or 400 for rfp_portal_project_required and
    rfp_portal_reason_invalid, 503 for rfp_portal_locked); `http_status`
    overrides that mapping for one raise (Run now while the slice is
    inactive answers 503 under rfp_portal_not_available). The message is
    app-authored."""

    def __init__(
        self, code: str, message: str, *, locked_until: datetime | None = None,
        http_status: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.locked_until = locked_until
        self.http_status = http_status


class RfpPortalTransient(RuntimeError):
    """Worth retrying: network, a 5xx, storage. The queue's ladder applies."""

    llm_error_kind = "infrastructure"


class RfpPortalPermanent(ValueError):
    """A permanent, app-authored refusal. `flag_reason` lands on the
    invitation when the harvest job ends on it."""

    llm_error_kind = "bad_input"

    def __init__(
        self, message: str, *, http_status: int = 409, flag_reason: str | None = None
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.flag_reason = flag_reason


class _Unavailable(Exception):
    """Internal: the portal cannot be used right now (locked, unconfigured);
    the row waits without spending an attempt."""

    def __init__(self, reason: str | None, locked_until: datetime | None = None) -> None:
        super().__init__(reason or _MSG_NOT_CONFIGURED)
        self.locked_until = locked_until


class _ClaimLost(Exception):
    """The harvest row's claim_token changed under us."""


class _LeaseLost(RuntimeError):
    """A lease renewal failed: the sweep lease before an LLM call, or the
    queue lease before a portal request (`_renew`). Stand down at once: the
    per-file retry loop never retries it, and the job's own writes are
    fenced out anyway."""


# ── Small helpers ────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso_or_none(value: Any) -> str | None:
    if isinstance(value, datetime):
        return _iso(value if value.tzinfo else value.replace(tzinfo=timezone.utc))
    if isinstance(value, date):
        return value.isoformat()
    dt = _parse_ts(value)
    return _iso(dt) if dt else None


def _date_or_none(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None:
        return None
    text = str(value).strip()
    return text[:10] if text else None


def _cap(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] if text else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _error(exc: BaseException) -> str:
    return (redact_text(exc) or type(exc).__name__)[:_ERROR_MAX_CHARS]


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """A field of a client dataclass, or of a dict (the tests' stand-ins)."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc)
    return getattr(exc, "code", None) == "23505" or "23505" in text or "duplicate key" in text


def _chunks(items: list, size: int = _IN_CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _page_all(query_factory) -> list[dict]:
    out: list[dict] = []
    start = 0
    while True:
        page = (query_factory().range(start, start + _PAGE - 1).execute()).data or []
        out.extend(page)
        if len(page) < _PAGE:
            return out
        start += _PAGE


_DOUBLE_SLASH_RE = re.compile(r"/{2,}")


def normalize_entry_url(raw: str | None) -> str | None:
    """NGEM_ENTRY_URL as the client wants it: trimmed, repeated slashes in
    the PATH collapsed (the portal's own link to the Available Bids page
    carries a double slash before VendorResponse, which the client's strict
    path check refuses). The query is left exactly as given: the `e=` token
    is opaque and may itself contain slashes."""
    text = (raw or "").strip()
    if not text:
        return None
    parts = urlsplit(text)
    if not parts.scheme or not parts.netloc:
        return text
    return urlunsplit(parts._replace(path=_DOUBLE_SLASH_RE.sub("/", parts.path)))


def agency_name(agency: Any) -> str:
    """The agency as stored: capped, whitespace tidied, the app-authored
    fallback when the grid cell is empty. Used by the scan AND by the grid
    re-find on a stale link, so both derive the same `agency_key`."""
    return _cap(agency, _AGENCY_MAX_CHARS) or _UNKNOWN_AGENCY


def agency_key(agency: str | None) -> str:
    """The agency name lowercased with runs of non-alphanumerics collapsed
    to '-' (doc 2.4). An empty name keys as the stored fallback does."""
    key = _NON_ALNUM_RE.sub("-", (agency or "").lower()).strip("-")
    return key or _NON_ALNUM_RE.sub("-", _UNKNOWN_AGENCY.lower()).strip("-")


def external_key(portal: str, agency_key_value: str, bid_number: str) -> str:
    return f"{portal}:{agency_key_value}:{bid_number}"


def safe_filename(name: Any, fallback: str = "attachment") -> str:
    """The last path component with control and separator characters
    removed, whitespace collapsed, capped; never '.' or '..'."""
    text = str(name or "")
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = _FILENAME_CLEAN_RE.sub("", text)
    text = " ".join(text.split()).strip(". ")
    if not text or text in (".", ".."):
        return fallback
    return text[:_FILENAME_MAX_CHARS]


def sanitize_notes(html: Any) -> str | None:
    """The notes as an allowlisted HTML fragment (p, br, b, strong, i, em,
    u, ul, ol, li, a[href https]), capped and re-cleaned after the cap so a
    cut tag cannot survive."""
    if html is None:
        return None
    text = str(html)
    if not text.strip():
        return None

    def clean(fragment: str) -> str:
        return nh3.clean(
            fragment,
            tags=_NOTES_TAGS,
            attributes=_NOTES_ATTRIBUTES,
            url_schemes=_NOTES_URL_SCHEMES,
            strip_comments=True,
        )

    out = clean(text)
    if len(out) > _NOTES_HTML_MAX_CHARS:
        out = clean(out[:_NOTES_HTML_MAX_CHARS])
    return out.strip() or None


# ── Portal registry ──────────────────────────────────────────────────────────


class NgemPortal:
    """The `ngem` entry: everything this module needs from the client,
    behind one object so the tests replace it wholesale."""

    key = PORTAL_NGEM

    def module(self):
        from app.services import ngem_client

        return ngem_client

    def configured(self, settings: Settings) -> bool:
        return bool(settings.ngem_configured)

    def account(self, settings: Settings) -> str | None:
        return settings.ngem_login_username.strip() or None

    def entry_url(self, settings: Settings) -> str | None:
        return normalize_entry_url(settings.ngem_entry_url)

    def config(self, settings: Settings):
        return self.module().NgemConfig(
            username=settings.ngem_login_username.strip(),
            password=settings.ngem_login_password,
            entry_url=normalize_entry_url(settings.ngem_entry_url) or "",
            min_request_interval=settings.ngem_min_request_interval_seconds,
            login_min_interval=settings.ngem_login_min_interval_seconds,
            timeout=settings.ngem_request_timeout_seconds,
        )

    def open_session(self, settings: Settings):
        """A session over the shared store; the queue lease is renewed before
        every paced request (the client's `renew` seam)."""
        return self.module().NgemSession(
            self.config(settings),
            SessionStore(settings, self.key),
            on_lock=_notify_lock,
            renew=_renew,
        )

    def availability(self, settings: Settings) -> tuple[bool, str | None, datetime | None]:
        """(usable, reason, locked_until) without touching the portal: the
        lock, and a failed attempt still inside the login interval."""
        if not self.configured(settings):
            return False, _MSG_NOT_CONFIGURED, None
        state = SessionStore(settings, self.key).load()
        ok, reason, until = self.module().availability_from(
            state, login_min_interval=settings.ngem_login_min_interval_seconds
        )
        return bool(ok), reason, _parse_ts(until)

    def classify(self, exc: BaseException) -> str:
        """Which failure family a client exception belongs to."""
        ngc = self.module()
        for name, kind in (
            ("NgemLoginLocked", KIND_LOCKED),
            ("NgemUnavailable", KIND_UNAVAILABLE),
            ("NgemLoginFailed", KIND_UNAVAILABLE),
            ("NgemForbidden", KIND_PERMANENT),
            ("NgemParseError", KIND_PERMANENT),
            ("NgemTransient", KIND_TRANSIENT),
            ("NgemSessionExpired", KIND_TRANSIENT),
        ):
            cls = getattr(ngc, name, None)
            if isinstance(cls, type) and isinstance(exc, cls):
                return kind
        if isinstance(exc, httpx.TransportError):
            return KIND_TRANSIENT
        return KIND_UNKNOWN

    def is_stale_link(self, exc: BaseException) -> bool:
        """The bid's view link no longer opens (the portal answered its
        BadRequest page): refresh the token from the grid once."""
        ngc = self.module()
        return isinstance(exc, ngc.NgemForbidden) and getattr(exc, "reason", None) == ngc.STALE_LINK

    def is_client_error(self, exc: BaseException) -> bool:
        return type(exc).__module__.endswith("ngem_client")

    def parse_bid_number(self, raw: Any) -> tuple[str | None, int | None]:
        number, addendum = self.module().parse_bid_number(str(raw or ""))
        return number or None, _int_or_none(addendum)


def _bc_portal_factory():
    """The BuildingConnected adapter, imported on first use so this module
    never imports bc_client at load time (the same shape as
    NgemPortal.module and llm_queue's lazy _spec imports)."""
    from app.services import rfp_bc_portal

    return rfp_bc_portal.BuildingConnectedPortal()


# Duck-typed adapters (NgemPortal, rfp_bc_portal.BuildingConnectedPortal,
# the tests' FakePortal): `key`, `configured`, `account`, `entry_url`,
# `open_session`, `availability`, `classify`, `is_stale_link`,
# `parse_bid_number`, plus the optional hooks `_hook` looks up.
PORTALS: dict[str, Callable[[], Any]] = {
    PORTAL_NGEM: NgemPortal,
    PORTAL_BUILDINGCONNECTED: _bc_portal_factory,
}


def _portal(key: str | None) -> Any:
    factory = PORTALS.get(key or "")
    if factory is None:
        raise RfpPortalPermanent(f"No client exists for the portal {key!r}.")
    return factory()


def _hook(portal: Any, name: str) -> Callable | None:
    """A portal-specific hook when the adapter defines one, else None: the
    generic code then keeps its NGEM behaviour, byte for byte."""
    hook = getattr(portal, name, None)
    return hook if callable(hook) else None


def _adapter(key: str | None) -> Any:
    """The adapter for a portal key, or None for an unknown key or one whose
    adapter cannot be built: the sweep steps and a failed run's bell look
    hooks up through this and never raise over the registry."""
    factory = PORTALS.get(key or "")
    if factory is None:
        return None
    try:
        return factory()
    except Exception:  # noqa: BLE001 - an adapter that cannot be built has no hooks
        logger.debug("rfp portal: adapter %r could not be built", key, exc_info=True)
        return None


def _portal_hook(key: str | None, name: str) -> Callable | None:
    return _hook(_adapter(key), name)


# The portals whose invitations carry nothing to harvest (BuildingConnected:
# the API exposes no documents, so its rows go match -> create and its
# client has no fetch_event). `harvest_again` and the harvest route refuse
# these with rfp_portal_not_actionable instead of queuing a job that can
# only crash.
_PORTALS_WITHOUT_HARVEST = frozenset({PORTAL_BUILDINGCONNECTED})
MSG_NO_HARVEST = "BuildingConnected invitations carry no documents to harvest."


def portal_has_harvest(key: str | None) -> bool:
    """Whether a harvest job exists for the portal `key` (fails open only
    for the portals that have one: an unknown key is answered by
    `_portal` raising further down the same path)."""
    return str(key or "") not in _PORTALS_WITHOUT_HARVEST


# ── Session store (rfp_harvest_sessions, provider ngem) ──────────────────────


class SessionStore:
    """The one shared portal session, per docs section 3.2: the jar, the
    failure counter and the lock it produces."""

    def __init__(self, settings: Settings, provider: str = PORTAL_NGEM) -> None:
        self.s = settings
        self.provider = provider
        self._last_touch = 0.0

    def load(self) -> dict | None:
        rows = (
            get_supabase()
            .table(SESSIONS_TABLE)
            .select("*")
            .eq("provider", self.provider)
            .limit(1)
            .execute()
        ).data or []
        return rows[0] if rows else None

    def save_cookies(self, account: str, cookies: list[dict]) -> None:
        now = _iso(_now())
        get_supabase().table(SESSIONS_TABLE).upsert(
            {
                "provider": self.provider,
                "account": account,
                "cookies": cookies,
                "logged_in_at": now,
                "last_used_at": now,
                "login_failures": 0,
                "locked_until": None,
                "last_error": None,
            },
            on_conflict="provider",
        ).execute()

    def record_login(
        self, *, ok: bool, error: str | None, counts_toward_lock: bool = True
    ) -> dict:
        """The attempt stamp, and the failure counter unless the attempt says
        nothing about the credentials (`counts_toward_lock=False`: the login
        chain broke on a 5xx or a timeout, so every process must still wait
        the minimum interval, but nothing moves toward the lock)."""
        state = self.load() or {}
        now = _now()
        row: dict = {"provider": self.provider, "last_login_attempt_at": _iso(now)}
        if ok:
            row.update(login_failures=0, locked_until=None, last_error=None)
        elif not counts_toward_lock:
            row["last_error"] = (error or "")[:_ERROR_MAX_CHARS]
        else:
            failures = int(state.get("login_failures") or 0) + 1
            row.update(login_failures=failures, last_error=(error or "")[:_ERROR_MAX_CHARS])
            if failures >= self.s.ngem_login_max_failures:
                row["locked_until"] = _iso(now + timedelta(seconds=self.s.ngem_login_lock_seconds))
        rows = (
            get_supabase().table(SESSIONS_TABLE).upsert(row, on_conflict="provider").execute()
        ).data or []
        return rows[0] if rows else {**state, **row}

    def touch(self) -> None:
        now = time.monotonic()
        if now - self._last_touch < _TOUCH_EVERY_SECONDS:
            return
        self._last_touch = now
        try:
            get_supabase().table(SESSIONS_TABLE).update({"last_used_at": _iso(_now())}).eq(
                "provider", self.provider
            ).execute()
        except Exception:  # noqa: BLE001 - bookkeeping only
            logger.debug("rfp portal: session touch failed", exc_info=True)


# ── Bells ────────────────────────────────────────────────────────────────────


def _unread_pending(sb, type_: str) -> bool:
    """An unread, undismissed bell row of this type already exists, for
    anyone: the per-type dedupe the login and scan-failed bells keep (one
    reminder per incident, not one per reader)."""
    query = (
        sb.table("notifications")
        .select("id")
        .eq("type", type_)
        .is_("read_at", "null")
        .is_("dismissed_at", "null")
    )
    return bool(query.limit(1).execute().data)


def _users_with_unread(sb, type_: str, key: str, value: str) -> set[str]:
    """The users who still hold an unread, undismissed bell of this type
    whose metadata `key` equals `value`."""
    rows = (
        sb.table("notifications")
        .select("user_id")
        .eq("type", type_)
        .is_("read_at", "null")
        .is_("dismissed_at", "null")
        .eq(f"metadata->>{key}", value)
        .execute()
    ).data or []
    return {str(r["user_id"]) for r in rows if r.get("user_id")}


def _role_user_ids(sb, roles) -> list[str]:
    values = sorted({r.value for r in roles if r != Role.ESTIMATOR})
    if not values:
        return []
    rows = (
        sb.table("profiles")
        .select("id")
        .in_("role", values)
        .eq("is_active", True)
        .execute()
    ).data or []
    return sorted({str(r["id"]) for r in rows if r.get("id")})


def _notify_roles_once(sb, roles, type_: str, message: str, metadata: dict, *, key: str) -> int:
    """One bell row per user holding one of `roles`, deduped per `(type,
    user, metadata[key])`: a user who still holds an unread bell for the
    same run (or invitation) is not rung again, and a user who read theirs
    IS, whatever the others did (`notify_role` inserts one row per user, so
    the dedupe has to be per user too). Returns how many rows were added."""
    pending = _users_with_unread(sb, type_, key, str(metadata.get(key)))
    added = 0
    for user_id in _role_user_ids(sb, roles):
        if user_id in pending:
            continue
        notify_user(user_id, None, type_, message, mirror_email=False, metadata=metadata)
        added += 1
    return added


def _bell_text(value: Any, limit: int = _BELL_VALUE_MAX_CHARS) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _bell_message(text: str) -> str:
    return _bell_text(text, _BELL_MESSAGE_MAX_CHARS)


def _pacific_stamp(dt: datetime) -> str:
    return dt.astimezone(COMPANY_TZ).strftime("%Y-%m-%d %I:%M %p PT")


def _notify_lock(until: datetime, error: str) -> None:
    """One bell to every IT Admin when logins lock; deduped while an unread
    one exists. The client calls this from the lock path; the password is
    never part of `error`."""
    sb = get_supabase()
    settings = get_settings()
    if _unread_pending(sb, NOTIFY_LOGIN_FAILED):
        return
    notify_role(
        Role.IT_ADMIN,
        None,
        NOTIFY_LOGIN_FAILED,
        f"NGEM login failed {settings.ngem_login_max_failures} times; scans and harvests "
        f"are paused until {_pacific_stamp(until)}. Check NGEM_LOGIN_USERNAME / "
        "NGEM_LOGIN_PASSWORD.",
        mirror_email=False,
        metadata={"locked_until": _iso(until), "error": (error or "")[:_ERROR_MAX_CHARS]},
    )


def _notify_new_invitations(sb, run_id: str, count: int) -> int:
    """One bell per review-role user for this run, deduped per (type, user,
    run_id): a user who still holds an unread one for the same run is not
    rung again."""
    if count <= 0:
        return 0
    noun = "invitation" if count == 1 else "invitations"
    return _notify_roles_once(
        sb, RFP_REVIEW_ROLES, NOTIFY_NEW_INVITATIONS,
        _bell_message(f"{count} new NGEM {noun}"),
        {"run_id": run_id, "count": count},
        key="run_id",
    )


def _describe_value(field_name: str, value: Any) -> str:
    if value is None or value == "":
        return "none"
    if field_name == "close_at":
        dt = _parse_ts(value)
        return rfp_match.format_pacific(dt, has_time=True) if dt else _bell_text(value)
    return _bell_text(value)


def _describe_change(change: dict) -> str:
    label = _FIELD_LABELS.get(change.get("field"), change.get("field"))
    return (
        f"{label} {_describe_value(change.get('field'), change.get('old'))} -> "
        f"{_describe_value(change.get('field'), change.get('new'))}"
    )


def _notify_changed(sb, row: dict, changes: list[dict], run_id: str) -> int:
    """One bell per invitation per change to every Estimating Admin, deduped
    per (type, user, invitation_id) while that user's is unread. Every
    portal-written value is capped before it reaches the message."""
    what = "; ".join(_describe_change(c) for c in changes)
    title = _bell_text(row.get("title"), _BELL_TITLE_MAX_CHARS)
    head = f"NGEM invitation changed: {_bell_text(row.get('agency'))} {_bell_text(row.get('bid_number_raw'))}"
    if title:
        head = f"{head} ({title})"
    return _notify_roles_once(
        sb, (Role.ESTIMATING_ADMIN,), NOTIFY_INVITATION_CHANGED,
        _bell_message(f"{head}: {what}."),
        {
            "invitation_id": row["id"],
            "run_id": run_id,
            "fields": [c.get("field") for c in changes],
        },
        key="invitation_id",
    )


def _notify_scan_failed(sb, run: dict, error: str | None) -> None:
    """One bell to every IT Admin when a scan run ends failed, deduped while
    an unread one exists (per type, like the login bell). Wrapped by the
    caller: a bell failure never changes what happened to the run."""
    if _unread_pending(sb, NOTIFY_SCAN_FAILED):
        return
    detail = _bell_text(error or _MSG_RUN_LOST, _BELL_MESSAGE_MAX_CHARS // 2)
    notify_role(
        Role.IT_ADMIN,
        None,
        NOTIFY_SCAN_FAILED,
        _bell_message(f"The NGEM scan failed: {detail} Check the NGEM block in RFP ingestion settings."),
        mirror_email=False,
        metadata={
            "run_id": run.get("id"),
            "trigger": run.get("trigger"),
            "error": (error or "")[:_ERROR_MAX_CHARS],
        },
    )


# ── Scheduler ────────────────────────────────────────────────────────────────


def schedule_slots(now: datetime, settings: Settings) -> list[datetime]:
    """Yesterday's and today's Pacific slots as aware instants, sorted.
    Yesterday's are included so a late slot's catch-up window survives
    midnight; the ledger dedups whatever was already claimed."""
    local = now.astimezone(COMPANY_TZ)
    out: list[datetime] = []
    for day in (local.date() - timedelta(days=1), local.date()):
        for hour, minute in settings.rfp_ngem_schedule:
            slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=COMPANY_TZ)
            out.append(slot.astimezone(timezone.utc))
    return sorted(out)


def due_slots(now: datetime, settings: Settings) -> list[datetime]:
    """The slots inside their catch-up window at `now`."""
    window = timedelta(hours=settings.rfp_ngem_catchup_hours)
    return [slot for slot in schedule_slots(now, settings) if slot <= now < slot + window]


def next_run_at(now: datetime, settings: Settings) -> datetime | None:
    """The first slot after `now` (today or tomorrow), for the status block."""
    local = now.astimezone(COMPANY_TZ)
    for day in (local.date(), local.date() + timedelta(days=1)):
        for hour, minute in sorted(settings.rfp_ngem_schedule):
            slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=COMPANY_TZ)
            instant = slot.astimezone(timezone.utc)
            if instant > now:
                return instant
    return None


def claim_slot(sb, portal: str, slot: datetime, kind: str = RUN_KIND_INCREMENTAL) -> dict | None:
    """Insert the ledger row for the slot. None when another worker claimed
    it first (23505 on the slot index, unique per (portal, kind, slot) since
    0136) or a run is already active (23505 on the active index); either way
    this tick does nothing for it."""
    try:
        rows = (
            sb.table(RUNS_TABLE)
            .insert(
                {
                    "portal": portal,
                    "trigger": TRIGGER_SCHEDULED,
                    "scheduled_for": _iso(slot),
                    "status": RUN_QUEUED,
                    "kind": kind,
                }
            )
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001 - only the ledger race is expected
        if _is_unique_violation(exc):
            return None
        raise
    return rows[0] if rows else None


def _load_run(sb, run_id: str) -> dict | None:
    rows = sb.table(RUNS_TABLE).select("*").eq("id", run_id).limit(1).execute().data or []
    return rows[0] if rows else None


def _fail_run(
    sb, run_id: str, message: str, *, from_statuses=RUN_ACTIVE, claimed_by: str | None = None,
) -> bool:
    """The run ends failed, fenced on the active statuses (and on the claim
    token when the caller holds one). The winning write logs the sentence
    and rings rfp_ngem.scan_failed to the IT Admins."""
    query = (
        sb.table(RUNS_TABLE)
        .update(
            {
                "status": RUN_FAILED,
                "last_error": message[:_ERROR_MAX_CHARS],
                "finished_at": _iso(_now()),
                "next_attempt_at": None,
                "claimed_by": None,
            }
        )
        .eq("id", run_id)
        .in_("status", list(from_statuses))
    )
    if claimed_by:
        query = query.eq("claimed_by", claimed_by)
    rows = query.execute().data or []
    if not rows:
        return False
    logger.info("rfp portal: scan run %s failed: %s", run_id, message[:_ERROR_MAX_CHARS])
    try:
        # The bell by portal: an adapter with its own scan-failed bell (its
        # own type, its own copy) rings that one; NGEM keeps rfp_ngem.scan_failed.
        bell = _portal_hook(rows[0].get("portal"), "notify_scan_failed")
        if bell is not None:
            bell(sb, rows[0], message)
        else:
            _notify_scan_failed(sb, rows[0], message)
    except Exception:  # noqa: BLE001 - the bell must never change the run's outcome
        logger.exception("rfp portal: scan-failed bell failed for run %s", run_id)
    return True


def _hold_run(sb, run_id: str, reason: str, seconds: float) -> None:
    """A queued run that could not get its job keeps its slot and waits: the
    next tick tries again (a transient PostgREST error must not cost the
    day's scan). `_fail_stale_runs` is the cap."""
    sb.table(RUNS_TABLE).update(
        {
            "last_error": reason[:_ERROR_MAX_CHARS],
            "next_attempt_at": _iso(_now() + timedelta(seconds=max(5.0, seconds))),
        }
    ).eq("id", run_id).eq("status", RUN_QUEUED).execute()


def _dispatch_run(sb, run: dict, settings: Settings) -> dict | None:
    """Queue the scan for a claimed run. An enqueue failure parks the run
    (queued, one poll interval out) instead of failing it, so a transient
    error does not lose the slot; the scheduler retries it every tick until
    `_fail_stale_runs` gives up."""
    try:
        job = enqueue_scan(
            run["id"], created_by=run.get("requested_by"), settings=settings,
            portal=run.get("portal"), kind=run.get("kind"),
        )
    except llm_queue.JobAlreadyActive as exc:
        return exc.job
    except Exception as exc:  # noqa: BLE001 - recorded on the run
        logger.exception("rfp portal: could not queue the scan for run %s", run.get("id"))
        _hold_run(
            sb, run["id"], _MSG_SCAN_NOT_QUEUED.format(error=_error(exc)),
            settings.rfp_ngem_poll_seconds,
        )
        return None
    logger.info(
        "rfp portal: scan run %s (%s) dispatched as job %s at priority %s",
        run.get("id"), run.get("trigger"), job.get("id"), job.get("priority"),
    )
    return job


def _requeue_parked_runs(sb, settings: Settings, now: datetime) -> None:
    """Every queued run with no scan job gets one: a parked run (locked
    logins, the portal unavailable, an enqueue that failed) once its wait
    has passed, and a run with no wait at all (a crash between the claim and
    the enqueue, an insert that answered no row) at once. Without this a
    jobless queued run held the active index, and Run now with it, until
    `_fail_stale_runs` gave up on it."""
    rows = (
        sb.table(RUNS_TABLE)
        .select("*")
        .eq("status", RUN_QUEUED)
        .or_(f"next_attempt_at.is.null,next_attempt_at.lte.{_iso(now)}")
        .limit(10)
        .execute()
    ).data or []
    for run in rows:
        if active_scan_job(run["id"]) is None:
            _dispatch_run(sb, run, settings)


def _fail_stale_runs(sb, settings: Settings, now: datetime) -> None:
    """A run whose job was lost (lease expired and the queue gave up, or the
    job vanished) would block every later run through the active index:
    past the scan timeout with no active job it is failed."""
    cutoff = _iso(now - timedelta(seconds=settings.rfp_ngem_scan_timeout_seconds))
    running = (
        sb.table(RUNS_TABLE)
        .select("*")
        .eq("status", RUN_RUNNING)
        .lt("started_at", cutoff)
        .limit(10)
        .execute()
    ).data or []
    queued = (
        sb.table(RUNS_TABLE)
        .select("*")
        .eq("status", RUN_QUEUED)
        .is_("next_attempt_at", "null")
        .lt("created_at", cutoff)
        .limit(10)
        .execute()
    ).data or []
    for run in running + queued:
        if active_scan_job(run["id"]) is None:
            _fail_run(sb, run["id"], _MSG_RUN_LOST)


def _bc_active(settings: Settings) -> bool:
    return bool(getattr(settings, "rfp_bc_active", False))


def _any_portal_active(settings: Settings) -> bool:
    """Any portal slice active (BUILD_CONTRACT D4): the tick, the sweep and
    Run now key on this; each slot loop keys on its own portal."""
    return bool(settings.rfp_ngem_active) or _bc_active(settings)


def _portal_active(settings: Settings, portal: str | None) -> bool:
    if portal == PORTAL_BUILDINGCONNECTED:
        return _bc_active(settings)
    return bool(settings.rfp_ngem_active)


def poll_once() -> None:
    """One scheduler tick: claim the due slots, queue their scans, re-queue
    parked runs, fail lost ones, then sweep the invitations under the
    portal's own lease. While an RFP test session is active the tick does
    nothing at all (docs/RFP_TESTING.md 4.3): the scan is paused and the
    page says so from the session state."""
    settings = get_settings()
    if not _any_portal_active(settings):
        return
    sb = get_supabase()
    if rfp_test.active_session(sb) is not None:
        # The bench pause applies to every portal (BUILD_CONTRACT D10).
        return
    now = _now()
    if settings.rfp_ngem_active:
        for slot in due_slots(now, settings):
            run = claim_slot(sb, PORTAL_NGEM, slot)
            if run is not None:
                logger.info(
                    "rfp portal: claimed the %s Pacific scan slot as run %s",
                    slot.astimezone(COMPANY_TZ).strftime("%Y-%m-%d %H:%M"), run.get("id"),
                )
                _dispatch_run(sb, run, settings)
    if _bc_active(settings):
        from app.services import rfp_bc_portal

        # The newest incremental slot only (no catch-up burst after downtime)
        # and the nightly full slot inside its catch-up window (D3).
        for slot, kind in rfp_bc_portal.due_slots(now, settings):
            run = claim_slot(sb, PORTAL_BUILDINGCONNECTED, slot, kind=kind)
            if run is not None:
                logger.info(
                    "rfp portal: claimed the %s BuildingConnected %s slot as run %s",
                    slot.astimezone(COMPANY_TZ).strftime("%Y-%m-%d %H:%M"), kind, run.get("id"),
                )
                _dispatch_run(sb, run, settings)
    _requeue_parked_runs(sb, settings, now)
    _fail_stale_runs(sb, settings, now)
    if not rfp_email_ingest.acquire_lease(sb, LEASE_KEY):
        return
    # Failures that reach the ladder's end this tick go to IT Admin as one alert.
    with rfp_email_ingest.alert_batch(sb):
        sweep(sb, lease_key=LEASE_KEY)


def _tick() -> None:
    from app.services import llm_gate

    with llm_gate.tier(llm_gate.TIER_PIPELINE):
        poll_once()


async def polling_loop() -> None:
    """Background loop started from the lifespan; the tick runs in a thread
    because every Supabase call is sync."""
    interval = get_settings().rfp_ngem_poll_seconds
    while True:
        try:
            await asyncio.to_thread(_tick)
        except Exception:  # noqa: BLE001 - the loop must survive any tick failure
            logger.exception("RFP portal poll failed")
        await asyncio.sleep(interval)


# ── The scan job (doc 2.2) ───────────────────────────────────────────────────


def _scan_priority(settings: Settings, portal: str | None, kind: str | None) -> int:
    """The queue priority by portal and kind (BUILD_CONTRACT D9): NGEM at
    RFP_NGEM_SCAN_QUEUE_PRIORITY; BuildingConnected's incremental scan at
    RFP_BC_SCAN_QUEUE_PRIORITY and its full sync behind it."""
    if portal == PORTAL_BUILDINGCONNECTED:
        if (kind or RUN_KIND_INCREMENTAL) == RUN_KIND_FULL:
            return int(settings.rfp_bc_full_sync_queue_priority)
        return int(settings.rfp_bc_scan_queue_priority)
    return int(settings.rfp_ngem_scan_queue_priority)


def enqueue_scan(
    run_id: str, *, created_by: str | None, settings: Settings | None = None,
    portal: str | None = PORTAL_NGEM, kind: str | None = RUN_KIND_INCREMENTAL,
) -> dict:
    """The scan job, at its own priority (RFP_NGEM_SCAN_QUEUE_PRIORITY, 140,
    ahead of the harvests at 150): the two families share one third-pass
    slot per worker, and the noon scan must not queue behind a morning's
    harvest backlog. One job type for every portal and kind; the run row
    says what the job does."""
    s = settings or get_settings()
    return llm_queue.enqueue(
        JOB_SCAN,
        target_id=run_id,
        payload={"run_id": run_id},
        created_by=created_by,
        priority=_scan_priority(s, portal, kind),
        settings=s,
        raise_on_active=True,
    )


def active_scan_job(run_id: str) -> dict | None:
    return llm_queue.active_job(JOB_SCAN, run_id)


def scan_status(run_id: str) -> str | None:
    """For the queue: every terminal run status as 'done' so the AI monitor
    refuses to requeue a finished scan (Run now is the retry path)."""
    run = _load_run(get_supabase(), run_id)
    if run is None:
        return None
    if run.get("status") in (RUN_COMPLETE, RUN_FAILED):
        return "done"
    return "pending" if run.get("status") == RUN_QUEUED else run.get("status")


def mark_scan_from_queue(run_id: str, status: str, error: str | None) -> None:
    """The queue's domain marks after a job attempt ends: 'pending' means a
    retry is scheduled (the run already carries last_error, nothing to do);
    'failed' means the ladder is exhausted or the job was canceled: the run
    fails, fenced on the active statuses so a finished run is untouched."""
    if status != "failed":
        return
    _fail_run(get_supabase(), run_id, error or _MSG_INTERRUPTED)


def _claim_run(sb, run: dict, token: str) -> bool:
    """CAS the run to running under a fresh per-attempt claim token. Every
    later write of this attempt is fenced on (id, status, claimed_by), so a
    zombie attempt (its queue lease expired, the job reclaimed by another
    worker or by this process again) can never park, complete or fail the
    run out from under the attempt that owns it now."""
    rows = (
        sb.table(RUNS_TABLE)
        .update(
            {
                "status": RUN_RUNNING,
                "claimed_by": token,
                "started_at": _iso(_now()),
                "finished_at": None,
                "next_attempt_at": None,
                "last_error": None,
            }
        )
        .eq("id", run["id"])
        .in_("status", list(RUN_ACTIVE))
        .execute()
    ).data or []
    return bool(rows)


def _park_run(sb, run_id: str, token: str, reason: str | None, seconds: float) -> bool:
    """Back to queued with the wait; no attempt spent. The scheduler
    re-queues the scan once the wait has passed. Fenced on the claim."""
    rows = (
        sb.table(RUNS_TABLE)
        .update(
            {
                "status": RUN_QUEUED,
                "claimed_by": None,
                "last_error": (reason or _MSG_NOT_CONFIGURED)[:_ERROR_MAX_CHARS],
                "next_attempt_at": _iso(_now() + timedelta(seconds=max(5.0, seconds))),
            }
        )
        .eq("id", run_id)
        .eq("status", RUN_RUNNING)
        .eq("claimed_by", token)
        .execute()
    ).data or []
    return bool(rows)


def _note_run_error(sb, run_id: str, token: str, message: str) -> None:
    """The transient sentence on a run that stays running (the queue's
    ladder re-runs the same job); fenced on the claim."""
    sb.table(RUNS_TABLE).update({"last_error": message[:_ERROR_MAX_CHARS]}).eq("id", run_id).eq(
        "status", RUN_RUNNING
    ).eq("claimed_by", token).execute()


def _complete_run(sb, run_id: str, token: str, pages: int, counts: dict) -> bool:
    # A scan may complete with a note (`counts["last_error"]`): the
    # BuildingConnected pull cut short by its page cap completes with what
    # it upserted and records the truncation here; otherwise the sentence
    # of an earlier attempt is cleared.
    note = counts.get("last_error")
    fields = {
        "status": RUN_COMPLETE,
        "finished_at": _iso(_now()),
        "claimed_by": None,
        "last_error": str(note)[:_ERROR_MAX_CHARS] if note else None,
        "next_attempt_at": None,
        "pages_scanned": int(pages or 0),
        "invitations_seen": int(counts.get("seen") or 0),
        "invitations_new": int(counts.get("new") or 0),
        "invitations_changed": int(counts.get("changed") or 0),
        "invitations_skipped_closed": int(counts.get("skipped_closed") or 0),
    }
    # The 0136 counters and the high water mark the run started from, only
    # when the scan reported them (an NGEM scan never does, so its update
    # stays byte for byte what it was).
    for key in ("rows_pulled", "rows_pipeline", "rows_expired", "rows_withdrawn"):
        if key in counts:
            fields[key] = int(counts.get(key) or 0)
    if "high_water_before" in counts:
        fields["high_water_before"] = _iso_or_none(counts.get("high_water_before"))
    rows = (
        sb.table(RUNS_TABLE)
        .update(fields)
        .eq("id", run_id)
        .eq("status", RUN_RUNNING)
        .eq("claimed_by", token)
        .execute()
    ).data or []
    return bool(rows)


def _wait_seconds(settings: Settings, locked_until: datetime | None, floor: float) -> float:
    wait = float(floor)
    if locked_until:
        wait = max(wait, (locked_until - _now()).total_seconds())
    return wait


def _renew() -> None:
    """Before every portal request: the queue lease. A lost lease is
    _LeaseLost, not a transient: nothing retries it (the per-file loop
    re-raises it at once) and the job stands down."""
    if not llm_queue.renew_lease():
        raise _LeaseLost(_MSG_INTERRUPTED)


def _kind_of(portal: NgemPortal, exc: BaseException) -> str:
    if isinstance(exc, _Unavailable):
        return KIND_UNAVAILABLE
    if isinstance(exc, RfpPortalPermanent):
        return KIND_PERMANENT
    if isinstance(exc, (RfpPortalTransient, _LeaseLost)):
        return KIND_TRANSIENT
    try:
        return portal.classify(exc)
    except Exception:  # noqa: BLE001 - a client that cannot be imported is unknown trouble
        return KIND_UNKNOWN


def execute_scan(run_id: str) -> None:
    """The rfp_portal_scan job: log in if needed, page the My Invitations
    grid, upsert the invitations, detect changes, close the run."""
    settings = get_settings()
    sb = get_supabase()
    run = _load_run(sb, run_id)
    if run is None:
        raise RfpPortalPermanent(_MSG_RUN_NOT_FOUND, http_status=404)
    if run.get("status") == RUN_COMPLETE:
        # A retry of a run that already completed (the queue re-ran the job
        # after a late failure such as a bell): nothing to do, idempotent.
        logger.info("rfp portal: scan run %s is already complete; nothing to do", run_id)
        return
    if run.get("status") not in RUN_ACTIVE:
        raise RfpPortalPermanent(_MSG_RUN_NOT_RUNNABLE)
    token = uuid.uuid4().hex
    if not _claim_run(sb, run, token):
        logger.warning("rfp portal: lost the claim on scan run %s; another attempt owns it", run_id)
        return
    portal = _portal(run.get("portal"))
    try:
        usable, reason, until = portal.availability(settings)
        if not usable:
            raise _Unavailable(reason, until)
        with closing(portal.open_session(settings)) as session:
            _renew()
            scan = _hook(portal, "scan")
            if scan is not None:
                # A portal with its own scan (BuildingConnected: the API
                # pull, the upsert by opportunity id, the change log).
                pages, counts = scan(sb, settings, run, session, _renew)
            else:
                page = session.fetch_invitations(settings.rfp_ngem_max_list_pages)
                _renew()
                counts = apply_scan(sb, settings, run, page)
                pages = _attr(page, "pages_fetched") or 0
            if not _complete_run(sb, run_id, token, pages, counts):
                logger.warning(
                    "rfp portal: scan run %s finished but its claim was lost; leaving the row alone",
                    run_id,
                )
                return
            after = _hook(portal, "after_complete")
            if after is not None:
                # Only the attempt that won the completion moves the
                # portal's high water mark (a zombie attempt never does).
                after(sb, settings, run, counts)
            logger.info(
                "rfp portal: scan run %s complete: %s page(s), %s seen, %s new, %s changed, "
                "%s closed",
                run_id, pages, counts.get("seen", 0), counts.get("new", 0),
                counts.get("changed", 0), counts.get("skipped_closed", 0),
            )
    except Exception as exc:  # noqa: BLE001 - classified below
        kind = _kind_of(portal, exc)
        if kind in (KIND_LOCKED, KIND_UNAVAILABLE):
            wait = _wait_seconds(
                settings, _parse_ts(getattr(exc, "locked_until", None)), settings.rfp_ngem_poll_seconds
            )
            _park_run(sb, run_id, token, _error(exc), wait)
            return
        if kind == KIND_PERMANENT:
            _fail_run(sb, run_id, _error(exc), claimed_by=token)
            raise RfpPortalPermanent(_error(exc)) from exc
        if kind == KIND_TRANSIENT:
            _note_run_error(sb, run_id, token, _error(exc))
            raise RfpPortalTransient(_error(exc)) from exc
        logger.exception("rfp portal: unexpected failure in scan run %s", run_id)
        _note_run_error(sb, run_id, token, _MSG_INTERRUPTED)
        raise RfpPortalTransient(_MSG_INTERRUPTED) from exc
    if counts.get("new"):
        # After the run is complete: a bell failure must not send a finished
        # run down the retry ladder. The bell by portal (its own type and copy).
        try:
            notify_new = _hook(portal, "notify_new") or _notify_new_invitations
            notify_new(sb, run_id, int(counts["new"]))
        except Exception:  # noqa: BLE001 - the bell never changes the run's outcome
            logger.exception("rfp portal: new-invitations bell failed for run %s", run_id)


_SCAN_ROW_SELECT = (
    "id, portal, agency, agency_key, bid_number, bid_number_raw, addendum_no, title, "
    "close_at, status, seen_count, change_log, missing_since"
)


def _row_key(row: Any) -> tuple[str, str] | None:
    """(agency_key, bid_number) of a parsed grid row, the dedup key of the
    table; None when the row cannot be keyed (no bid number at all)."""
    raw = _cap(_attr(row, "bid_number_raw"), _NUMBER_MAX_CHARS) or ""
    number = _cap(_attr(row, "bid_number"), _NUMBER_MAX_CHARS) or raw
    if not number:
        return None
    return agency_key(agency_name(_attr(row, "agency"))), number


def _invitation_fields(row: Any, close_at: datetime) -> dict | None:
    """The stored columns for one parsed grid row; None when the row cannot
    be keyed (no bid number at all). An empty title is stored EMPTY (never
    an app-authored placeholder, which the matcher would score and the bell
    would repeat); the match step routes it to harvest as no_project_name
    and the frontend renders its own placeholder."""
    key = _row_key(row)
    if key is None:
        return None
    agency = agency_name(_attr(row, "agency"))
    raw = _cap(_attr(row, "bid_number_raw"), _NUMBER_MAX_CHARS) or ""
    number = key[1]
    return {
        "agency": agency,
        "agency_key": key[0],
        "bid_number": number,
        "bid_number_raw": raw or number,
        "addendum_no": _int_or_none(_attr(row, "addendum_no")),
        "title": _cap(_attr(row, "title"), _TITLE_MAX_CHARS) or "",
        "issued_on": _date_or_none(_attr(row, "issued_on")),
        "close_at": _iso(close_at),
        "time_left": _cap(_attr(row, "time_left"), _SHORT_MAX_CHARS),
        "bid_status": _cap(_attr(row, "bid_status"), _SHORT_MAX_CHARS),
        "response_status": _cap(_attr(row, "response_status"), _SHORT_MAX_CHARS),
        "response_code": _cap(_attr(row, "response_code"), _SHORT_MAX_CHARS),
        "status_code": _cap(_attr(row, "status_code"), _SHORT_MAX_CHARS),
        "view_url": _cap(_attr(row, "view_url"), 2000),
    }


def _comparable(field_name: str, value: Any) -> Any:
    if field_name == "close_at":
        dt = _parse_ts(value)
        return dt.timestamp() if dt else None
    if field_name == "addendum_no":
        return _int_or_none(value)
    return value if value not in ("", None) else None


def _find_invitation(sb, portal: str, key: tuple[str, str]) -> dict | None:
    rows = (
        sb.table(INVITATIONS_TABLE)
        .select(_SCAN_ROW_SELECT)
        .eq("portal", portal)
        .eq("agency_key", key[0])
        .eq("bid_number", key[1])
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else None


def _insert_invitation(
    sb, run: dict, fields: dict, now: datetime, *, status: str = STATUS_MATCH
) -> dict | None:
    """The insert with the seen bookkeeping. `status` is the entry status:
    `match` (NGEM always; a BuildingConnected row that passed the entry
    rule) or a parked one (`historical`: mirror-only, never swept)."""
    try:
        rows = (
            sb.table(INVITATIONS_TABLE)
            .insert(
                {
                    "portal": run["portal"],
                    **fields,
                    "status": status,
                    "first_seen_at": _iso(now),
                    "last_seen_at": _iso(now),
                    "first_seen_run_id": run["id"],
                    "last_seen_run_id": run["id"],
                    "seen_count": 1,
                }
            )
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001 - only the unique race is expected
        if _is_unique_violation(exc):
            return None
        raise
    return rows[0] if rows else None


def _update_seen(sb, run: dict, current: dict, fields: dict, now: datetime) -> bool:
    """A known row: the seen bookkeeping and the volatile grid columns, plus
    the tracked fields when they moved (with the change log and the bell).
    Field-scoped: never touches status or the attempt ladder."""
    update: dict = {
        "last_seen_at": _iso(now),
        "last_seen_run_id": run["id"],
        "seen_count": int(current.get("seen_count") or 0) + 1,
        "missing_since": None,
        "agency": fields["agency"],
        "issued_on": fields["issued_on"],
        "time_left": fields["time_left"],
        "bid_status": fields["bid_status"],
        "response_status": fields["response_status"],
        "response_code": fields["response_code"],
        "status_code": fields["status_code"],
        "view_url": fields["view_url"],
    }
    changes: list[dict] = []
    for name in _TRACKED_FIELDS:
        old, new = current.get(name), fields.get(name)
        if _comparable(name, old) != _comparable(name, new):
            changes.append({"at": _iso(now), "field": name, "old": old, "new": new, "run_id": run["id"]})
            update[name] = new
    if changes:
        log = list(current.get("change_log") or []) + changes
        update["change_log"] = log[-_CHANGE_LOG_CAP:]
    sb.table(INVITATIONS_TABLE).update(update).eq("id", current["id"]).execute()
    if changes and current.get("status") != STATUS_IGNORED:
        try:
            _notify_changed(sb, {**current, **fields}, changes, run["id"])
        except Exception:  # noqa: BLE001 - the bell must never fail the scan
            logger.exception("rfp portal: change bell failed for %s", current.get("id"))
    return bool(changes)


def _mark_missing(sb, rows: list[dict], now: datetime) -> None:
    ids = [r["id"] for r in rows if r.get("status") != STATUS_IGNORED and not r.get("missing_since")]
    for chunk in _chunks(ids):
        sb.table(INVITATIONS_TABLE).update({"missing_since": _iso(now)}).in_("id", chunk).is_(
            "missing_since", "null"
        ).execute()


def apply_scan(sb, settings: Settings, run: dict, page: Any) -> dict:
    """Doc 2.2 steps 3 and 4 over one fetched grid: closed rows skipped, new
    rows inserted at `match`, known rows refreshed with changes logged and
    belled, absent rows stamped missing. Returns the run counts."""
    now = _now()
    portal = run["portal"]
    rows = list(_attr(page, "rows") or [])
    existing = _page_all(
        lambda: sb.table(INVITATIONS_TABLE)
        .select(_SCAN_ROW_SELECT)
        .eq("portal", portal)
        .order("id", desc=False)
    )
    by_key = {(r["agency_key"], r["bid_number"]): r for r in existing}
    seen: set[tuple[str, str]] = set()
    counts = {"seen": len(rows), "new": 0, "changed": 0, "skipped_closed": 0}
    for row in rows:
        key = _row_key(row)
        if key is None:
            logger.warning("rfp portal: a grid row without a bid number was skipped")
            continue
        if key in seen:
            # Two grid rows that key alike (agencies differing only in
            # punctuation or case, a bid listed twice): the first row's
            # values stand.
            logger.info("rfp portal: the grid listed %s/%s twice; keeping the first row", *key)
            continue
        # Seen BEFORE the past-close skip: a closed bid the grid still lists
        # is not missing from the grid.
        seen.add(key)
        close_at = _parse_ts(_attr(row, "close_at"))
        if close_at is None or close_at <= now:
            counts["skipped_closed"] += 1
            continue
        fields = _invitation_fields(row, close_at)
        if fields is None:
            continue
        current = by_key.get(key)
        if current is None:
            inserted = _insert_invitation(sb, run, fields, now)
            if inserted is not None:
                counts["new"] += 1
                by_key[key] = inserted
                continue
            current = _find_invitation(sb, portal, key)
            if current is None:
                continue
            by_key[key] = current
        if _update_seen(sb, run, current, fields, now):
            counts["changed"] += 1
    total_items = _int_or_none(_attr(page, "total_items"))
    if total_items is None or len(rows) >= total_items:
        _mark_missing(sb, [r for k, r in by_key.items() if k not in seen], now)
    return counts


# ── The invitation sweep (doc 2.3) ───────────────────────────────────────────


@dataclass
class _SweepState:
    """Per-tick memory: the shared reference bundle, built by the first row
    that needs it (rfp_email_ingest.reference_bundle reads `bundle`)."""

    bundle: dict | None = None


# The BuildingConnected columns the match and create steps read (0136);
# `payload` rides along for the bid notes the scorer weighs, and the dated
# facts plus the address and request type are what the automatic creation
# copies onto the project (bc_facts.project_facts reads them from the row,
# not the payload). Null on NGEM rows.
_SWEEP_BC_COLUMNS = (
    "external_id, external_url, gc_id, gc_kind, gc_external_id, gc_external_name, "
    "gc_confirmed_at, gc_candidates, lead, submission_state, is_nda_required, is_archived, "
    "trade_name, invited_at, job_walk_at, expected_start_at, expected_finish_at, address, "
    "request_type, sibling_of, project_gc_id, resolution, payload"
)
_SWEEP_SELECT = (
    "id, portal, agency, agency_key, bid_number, bid_number_raw, addendum_no, title, "
    "close_at, status, flag_reason, attempts, last_error, next_attempt_at, "
    "excluded_project_ids, match_project_id, harvest_id, first_seen_at, created_project_id, "
    # The deleted-project block (docs/PROJECT_DELETE.md 5, migration 0141).
    "create_blocked_archive_id, "
    + _SWEEP_BC_COLUMNS
)

_ROUTE_STATUS = {
    rfp_match.STATUS_DUPLICATE: STATUS_EXISTS,
    rfp_match.STATUS_MERGED: STATUS_EXISTS,
    rfp_match.STATUS_REVIEW_MATCH: STATUS_REVIEW_MATCH,
    rfp_match.STATUS_DONE: STATUS_HARVEST,
}


def _map_route_status(status: str, *, no_match: str = STATUS_HARVEST) -> str:
    """Route statuses onto the invitation vocabulary; anything the router
    might add later waits for a person rather than guessing. `no_match` is
    where a row with no candidate goes: `harvest` for NGEM, `create` for a
    portal that has nothing to harvest (BuildingConnected)."""
    if status == rfp_match.STATUS_DONE:
        return no_match
    return _ROUTE_STATUS.get(status, STATUS_REVIEW_MATCH)


def _cas(sb, invitation_id: str, expected_status: str, fields: dict, *, extra: dict | None = None) -> bool:
    """Compare-and-set on status: the sweep, a reviewer and a second runner
    can never clobber each other. True when the row was ours to move.
    `extra` adds column fences to the same write ({col: value}, a None
    value meaning "still null"): the BuildingConnected match branch fences
    its GC columns on gc_confirmed_at so a reviewer's answer given while
    the sweep held a stale row is never overwritten."""
    query = (
        sb.table(INVITATIONS_TABLE)
        .update(fields)
        .eq("id", invitation_id)
        .eq("status", expected_status)
    )
    for col, value in (extra or {}).items():
        query = query.is_(col, "null") if value is None else query.eq(col, value)
    rows = (query.execute()).data or []
    return bool(rows)


def _total(candidate: dict | None) -> float | None:
    if not candidate:
        return None
    total = (candidate.get("breakdown") or {}).get("total")
    return round(float(total), 3) if total is not None else None


def sweep(sb, *, lease_key: str | None = None) -> None:
    """Rows at `match` or `harvest` whose wait has passed, oldest first, up
    to RFP_NGEM_SWEEP_BATCH per tick. Each step CASes on (id, status); a
    poison row is logged and skipped; once the match model is found
    unavailable no further match row is tried this tick."""
    settings = get_settings()
    now_iso = _iso(_now())
    rows: list[dict] = []
    for portal_key in PORTALS:
        # Per portal so rfp_portal_invitations_sweep_idx (portal, status,
        # next_attempt_at) carries the query.
        rows.extend(
            (
                sb.table(INVITATIONS_TABLE)
                .select(_SWEEP_SELECT)
                .eq("portal", portal_key)
                .in_("status", list(STATUS_PENDING))
                .or_(f"next_attempt_at.is.null,next_attempt_at.lte.{now_iso}")
                .order("first_seen_at", desc=False)
                .order("id", desc=False)
                .limit(settings.rfp_ngem_sweep_batch)
                .execute()
            ).data or []
        )
    state = _SweepState()
    llm_down = False

    def renew() -> bool:
        return rfp_email_ingest.renew_lease(sb, lease_key) if lease_key else True

    for i, row in enumerate(rows):
        if lease_key and i and i % _RENEW_EVERY == 0 and not renew():
            return
        try:
            if _process_invitation(sb, row, state, settings, llm_down=llm_down, renew=renew):
                llm_down = True
        except _LeaseLost:
            return
        except Exception:  # noqa: BLE001 - one poison row must not stall the sweep
            logger.exception("rfp portal: sweep failed for invitation %s", row.get("id"))


def _process_invitation(
    sb, row: dict, state: _SweepState, settings: Settings, *, llm_down: bool, renew
) -> bool:
    """One row as far as it goes this tick. Returns True when the match step
    found the model unavailable (the tick stops calling it)."""
    if row.get("status") == STATUS_MATCH:
        if llm_down:
            return False
        next_status, down = _step_match(sb, row, state, settings, renew=renew)
        if down:
            return True
        if next_status == STATUS_HARVEST:
            row["status"] = STATUS_HARVEST
            _step_harvest(sb, row, settings)
        elif next_status == STATUS_CREATE:
            # A portal with nothing to harvest (BuildingConnected): the
            # create step runs in the same pass, as the harvest step does.
            row["status"] = STATUS_CREATE
            row.update(attempts=0, last_error=None, next_attempt_at=None)
            _step_create(sb, row, settings)
        return False
    if row.get("status") == STATUS_HARVEST:
        _step_harvest(sb, row, settings)
        return False
    if row.get("status") == STATUS_SPLIT:
        if _step_split(sb, row, settings, renew=renew):
            row["status"] = STATUS_CREATE
            _step_create(sb, row, settings)
        return False
    if row.get("status") == STATUS_CREATE:
        _step_create(sb, row, settings)
    return False


def _wait_for_model(sb, row: dict, reason: str, settings: Settings) -> None:
    """Push next_attempt_at WITHOUT spending an attempt: the model is away,
    not the row at fault."""
    _cas(sb, row["id"], STATUS_MATCH, {
        "last_error": reason[:_ERROR_MAX_CHARS],
        "next_attempt_at": _iso(
            _now() + timedelta(seconds=settings.rfp_email_ingestion_classify_retry_seconds)
        ),
    })


def _llm_gate(sb, row: dict, settings: Settings) -> str | None:
    """Before a live call: is the match model there? When it is not, the row
    waits and the reason is returned so the tick stops calling it."""
    feature = rfp_match.FEATURE_MATCH
    if not llm.is_configured(feature, settings):
        reason = "No model is configured for RFP project matching."
        _wait_for_model(sb, row, reason, settings)
        return reason
    try:
        snapshot = llm_health.cached(settings)
    except Exception:  # noqa: BLE001 - a broken probe must not stop the sweep
        logger.exception("LLM health snapshot failed; attempting the match call")
        snapshot = None
    unavailable = rfp_email_ingest.model_unavailable(snapshot, feature)
    if unavailable:
        _state, detail = unavailable
        _wait_for_model(sb, row, detail, settings)
        return detail
    return None


def _llm_call_failed(
    sb, row: dict, exc: Exception, model: str, settings: Settings
) -> tuple[str, str | None]:
    """What a failed live call did to the row, on its own attempt counter:
    ('unusable', message) for unusable output the second time, ('down',
    message) when the row waits without spending an attempt, ('spent', None)
    after a backoff write or the terminal review_match write."""
    attempts = int(row.get("attempts") or 0)
    kind = llm_errors.classify(exc)
    message = llm_errors.user_message(exc, model)[:_ERROR_MAX_CHARS]
    if kind == llm_errors.KIND_INVALID_OUTPUT and attempts >= 1:
        return "unusable", message
    decision, delay = rfp_email_ingest.attempt_decision(
        kind, attempts, settings.rfp_email_ingestion_classify_max_attempts,
        busy=rfp_email_ingest.gate_busy(exc),
    )
    if decision == "wait":
        _wait_for_model(sb, row, message, settings)
        return "down", message
    if decision == "fail":
        # A person decides the match in review_match: that lane is the
        # alert, so IT Admin is not rung for it.
        _cas(sb, row["id"], STATUS_MATCH, {
            "status": STATUS_REVIEW_MATCH,
            "flag_reason": FLAG_MATCH_FAILED,
            "decided_at_step": STATUS_MATCH,
            "attempts": attempts + 1,
            "last_error": message,
            "next_attempt_at": None,
        })
        return "spent", None
    _cas(sb, row["id"], STATUS_MATCH, {
        "attempts": attempts + 1,
        "last_error": message,
        "next_attempt_at": _iso(_now() + timedelta(seconds=delay)),
    })
    return "spent", None


def _step_match(
    sb, row: dict, state: _SweepState, settings: Settings, *, renew=None
) -> tuple[str | None, bool]:
    """Candidate scoring against the shared bundle, the LLM verdict when a
    candidate clears the review threshold, and the route (doc 2.3).
    Returns (next_status, model_down): next_status is `harvest` (NGEM) or
    `create` (a portal with nothing to harvest) when the row moved there;
    the caller runs that step in the same pass.

    The portal adapter may supply hooks (docs/RFP_BUILDINGCONNECTED.md 3.5,
    BUILD_CONTRACT D11): `auto_merge_enabled(settings)`, `no_match_status()`,
    `no_name_status()`, `has_name(title)`, `pre_match_park(row)`,
    `gc_for(sb, row, bundle, settings)`, `facts_for(row)`,
    `route_params(gc, best_for_gc, projects_by_id, settings)`,
    `refine(row, status, flag_reason)` and `on_exists(...)`. Without them
    the step is the NGEM step, unchanged."""
    now = _now()
    now_iso = _iso(now)
    portal = _adapter(row.get("portal"))
    auto_merge_hook = _hook(portal, "auto_merge_enabled")
    auto_merge = (
        bool(auto_merge_hook(settings)) if auto_merge_hook is not None
        else bool(settings.rfp_ngem_auto_resolve_enabled)
    )
    no_match_hook = _hook(portal, "no_match_status")
    no_match_status = no_match_hook() if no_match_hook is not None else STATUS_HARVEST
    base = {
        # The shared snapshot (kept as is for the email side) plus the switch
        # THIS step routed by: auto_merge_enabled in it is the email switch.
        "match_weights": {
            **rfp_match.settings_snapshot(settings),
            "auto_resolve_enabled": auto_merge,
        },
        "possible_rebid_project_id": None,
        "possible_rebid_score": None,
        "match_llm_model": None,
        "match_llm_prompt_version": None,
        "matched_at": now_iso,
        "decided_at_step": STATUS_MATCH,
    }
    empty = {"match_candidates": [], "match_project_id": None, "match_score": None}
    park = _hook(portal, "pre_match_park")
    parked = park(row) if park is not None else None
    if parked:
        # A row the portal parks before any scoring (an NDA-masked
        # BuildingConnected row: no GC, no facts to score).
        status, flag_reason = parked
        _cas(sb, row["id"], STATUS_MATCH, {
            **base, **empty, "status": status, "flag_reason": flag_reason, **_RESET,
        })
        return None, False
    title = row.get("title")
    has_name_hook = _hook(portal, "has_name")
    named = bool(title) and bool(rfp_match.normalize_project_name(title))
    if named and has_name_hook is not None:
        named = bool(has_name_hook(title))
    if not named:
        no_name_hook = _hook(portal, "no_name_status")
        target = no_name_hook() if no_name_hook is not None else STATUS_HARVEST
        moved = _cas(sb, row["id"], STATUS_MATCH, {
            **base,
            "status": target,
            "flag_reason": FLAG_NO_PROJECT_NAME,
            **empty,
            **_RESET,
        })
        return (target if moved and target in (STATUS_HARVEST, STATUS_CREATE) else None), False

    bundle = rfp_email_ingest.reference_bundle(sb, state, settings)
    close_at = _parse_ts(row.get("close_at"))
    gc = None
    gc_fields: dict = {}
    gc_for = _hook(portal, "gc_for")
    if gc_for is not None:
        gc = gc_for(sb, row, bundle, settings)
        if gc is not None and not row.get("gc_confirmed_at"):
            # A confirmed GC (a person answered the card) is never rewritten
            # by the sweep; an unconfirmed one is refreshed every match.
            gc_fields = {
                "gc_id": gc.gc_id,
                "gc_kind": gc.kind,
                "gc_candidates": list(gc.candidates or []),
            }
    facts_for = _hook(portal, "facts_for")
    if facts_for is not None:
        facts = facts_for(row)
    else:
        facts = rfp_match.ExtractedFacts(
            project_name=title, gc_name=None, bid_due_at=close_at, has_time=True,
            bid_notes=None, reasoning="",
        )
    excluded = {str(x) for x in (row.get("excluded_project_ids") or [])}
    window_lo = now - timedelta(days=settings.rfp_match_candidate_window_days)
    in_window: list[dict] = []
    rebid_band: list[dict] = []
    for project in bundle.get("projects") or []:
        bid_at = rfp_match.project_bid_at(project)
        if bid_at is None or str(project.get("id")) in excluded:
            continue
        (in_window if bid_at >= window_lo else rebid_band).append(project)
    candidates = rfp_match.rank_candidates(facts, in_window, settings, excluded_ids=excluded)
    projects_by_id = {p.get("id"): p for p in in_window}
    to_judge = rfp_match.model_candidates(candidates, projects_by_id, settings)

    llm_fields: dict = {}
    unusable: str | None = None
    if to_judge:
        if renew is not None and not renew():
            raise _LeaseLost()
        if _llm_gate(sb, row, settings):
            return None, True
        model = llm.active_model(rfp_match.FEATURE_MATCH, settings)
        system, messages = rfp_email_ingest.split_system(
            rfp_match.build_match_messages(facts, to_judge, settings),
            getattr(rfp_match, "MATCH_SYSTEM", ""),
        )
        try:
            result = llm.complete_json(
                rfp_match.FEATURE_MATCH,
                system=system,
                messages=messages,
                schema=rfp_match.MATCH_SCHEMA,
                schema_name="rfp_match",
                max_tokens=_MATCH_MAX_TOKENS,
                settings=settings,
            )
        except Exception as exc:  # noqa: BLE001 - classified by the ladder
            outcome, detail = _llm_call_failed(sb, row, exc, model, settings)
            if outcome == "down":
                return None, True
            if outcome != "unusable":
                return None, False
            unusable = detail
        else:
            verdicts = rfp_match.parse_verdicts(result, [c["index"] for c in to_judge])
            rfp_match.apply_verdicts(candidates, verdicts)
        llm_fields = {
            "match_llm_model": model,
            "match_llm_prompt_version": rfp_match.MATCH_PROMPT_VERSION,
        }

    merged = False
    if unusable is not None:
        best = candidates[0] if candidates else None
        status, flag_reason = STATUS_REVIEW_MATCH, FLAG_MATCH_LLM_UNUSABLE
    else:
        params = {
            "gc_resolved": True,
            "gc_on_project": True,
            "sender_verified": True,
            "auto_merge": auto_merge,
        }
        route_params = _hook(portal, "route_params")
        if route_params is not None:
            best_for_gc = rfp_email_ingest._best_not_different(candidates, settings)
            params.update(route_params(gc, best_for_gc, projects_by_id, settings))
        route = rfp_match.route(
            candidates,
            has_name=True,
            has_date=close_at is not None,
            settings=settings,
            **params,
        )
        # Read merged vs duplicate BEFORE the map collapses both to `exists`:
        # only a merge adds the GC to the project.
        merged = route.status == rfp_match.STATUS_MERGED
        best, flag_reason = route.best, route.flag_reason
        status = _map_route_status(route.status, no_match=no_match_status)
        refine = _hook(portal, "refine")
        if refine is not None:
            # The portal's refinements, folded into the SAME write below
            # (a second CAS would race a reviewer): no due date, submitted
            # in the portal with no project. An `exists` is never overridden.
            status, flag_reason = refine(row, status, flag_reason)

    fields = {
        **base,
        **llm_fields,
        **gc_fields,
        "match_candidates": candidates,
        "match_project_id": best.get("project_id") if best else None,
        "match_score": _total(best),
        "flag_reason": flag_reason,
        "status": status,
        "attempts": 0,
        "last_error": unusable,
        "next_attempt_at": None,
    }
    if status in (STATUS_REVIEW_MATCH, no_match_status):
        rebid = rfp_match.rebid_lookup(title, rebid_band, settings)
        if rebid:
            fields["possible_rebid_project_id"] = rebid[0]
            fields["possible_rebid_score"] = round(float(rebid[1]), 3)
    if status == STATUS_EXISTS:
        fields.update(resolved_by=None, resolved_at=now_iso, resolution="system")
    # When this write carries the sweep's GC guess, it is fenced on
    # gc_confirmed_at still being null: a reviewer who answered the GC card
    # while this tick held the stale row (inside the LLM call) wins, the
    # write misses, the row stays at `match` and is routed again next tick
    # with the confirmed GC. A portal that resolves the GC (gc_for) also
    # fences on the platform company the tick read: a scan that swapped
    # the GC on the board meanwhile (and cleared the resolution) wins the
    # same way, so the old company's resolution is never written back.
    fence: dict = {}
    if gc_fields:
        fence["gc_confirmed_at"] = None
    if gc_for is not None and gc is not None:
        fence["gc_external_id"] = row.get("gc_external_id")
    moved = _cas(sb, row["id"], STATUS_MATCH, fields, extra=fence or None)
    if not moved and fence:
        logger.info(
            "rfp portal: %s was not routed this tick (its status or GC moved under the sweep); "
            "it is read again next tick",
            row.get("id"),
        )
    if moved:
        row.update(gc_fields)
        row["flag_reason"] = flag_reason
    on_exists = _hook(portal, "on_exists")
    if moved and status == STATUS_EXISTS and merged and on_exists is not None:
        # The merge's side effects on the project (the GC link, the lead as
        # bid contact, the files flag), after the row is ours.
        project_id = fields["match_project_id"]
        try:
            on_exists(sb, {**row, **fields}, project_id, gc, settings, merged=True, bundle=bundle)
        except rfp_email_ingest.RfpMatchError as exc:
            # The project closed or left the window between the bundle and
            # now: park for a person instead of retrying forever.
            logger.warning("rfp portal: %s could not merge into %s: %s", row.get("id"), project_id, exc)
            _cas(sb, row["id"], STATUS_EXISTS, {
                "status": STATUS_REVIEW_MATCH,
                "flag_reason": rfp_match.REASON_UNCERTAIN,
                "resolution": None,
                "resolved_at": None,
                "last_error": redact_text(exc)[:_ERROR_MAX_CHARS],
            })
    next_status = status if moved and status in (STATUS_HARVEST, STATUS_CREATE) else None
    return next_status, False


def _park(sb, row: dict, expected_status: str, seconds: float, error: str | None) -> None:
    _cas(sb, row["id"], expected_status, {
        "next_attempt_at": _iso(_now() + timedelta(seconds=max(5.0, float(seconds)))),
        "last_error": error,
    })


def _hold_at_cap(sb, row: dict, expected_status: str, error: str, settings: Settings) -> None:
    """The ladder's end on the portal side, which has no `failed` status: the
    row stays at its step with the sentence and a one-hour wait (never lost),
    its attempts pinned at the cap, and IT Admin is alerted on the way in.
    The hourly re-tries that follow find the attempts already at the cap and
    hold again quietly; a row that moves on resets its attempts, so a later
    hold at the same step alerts again."""
    cap = int(settings.rfp_email_ingestion_classify_max_attempts)
    first = int(row.get("attempts") or 0) < cap
    held = _cas(sb, row["id"], expected_status, {
        "attempts": max(cap, int(row.get("attempts") or 0)),
        "last_error": error,
        "next_attempt_at": _iso(_now() + timedelta(seconds=_LADDER_CAP_WAIT_SECONDS)),
    })
    if not held or not first:
        return
    title = str(row.get("title") or row.get("bid_number") or "(untitled)")[:120]
    rfp_email_ingest.alert_step_failed(
        sb, source="portal", row_id=row["id"], step=expected_status,
        label=f'invitation "{title}"', error=error,
    )


def _retry_or_hold(sb, row: dict, exc: Exception, settings: Settings) -> None:
    """The harvest step's enqueue failed: the attempt ladder; at the cap the
    row stays at `harvest` with the sentence and a one-hour wait (never
    lost)."""
    attempts = int(row.get("attempts") or 0) + 1
    error = _error(exc)
    if attempts >= settings.rfp_email_ingestion_classify_max_attempts:
        _hold_at_cap(sb, row, STATUS_HARVEST, error, settings)
        return
    _cas(sb, row["id"], STATUS_HARVEST, {
        "attempts": attempts,
        "last_error": error,
        "next_attempt_at": _iso(
            _now() + timedelta(seconds=rfp_email_ingest.backoff_seconds(attempts))
        ),
    })


def _step_harvest(sb, row: dict, settings: Settings) -> None:
    """Make sure an rfp_portal_harvest job exists for the row; the job moves
    it on. Never calls the portal. Locked logins push by the lock remainder."""
    try:
        portal = _portal(row.get("portal"))
        usable, reason, until = portal.availability(settings)
        if not usable:
            _park(sb, row, STATUS_HARVEST, _wait_seconds(settings, until, settings.rfp_harvest_poll_seconds), reason)
            return
        if active_harvest_job(row["id"]) is None:
            try:
                enqueue_harvest(row["id"], created_by=None, settings=settings)
            except llm_queue.JobAlreadyActive:
                pass
        _park(sb, row, STATUS_HARVEST, settings.rfp_harvest_poll_seconds, None)
    except Exception as exc:  # noqa: BLE001 - an enqueue failure retries on the ladder
        logger.exception("rfp portal: harvest step failed for %s", row.get("id"))
        _retry_or_hold(sb, row, exc, settings)


def _retry_or_fail_create(sb, row: dict, exc: Exception, settings: Settings) -> None:
    """The create step's ladder: attempts and backoff like the email side
    (rfp_email_ingest._retry_or_fail); at the cap the row stays at `create`
    with the sentence and a one-hour wait, never lost (the portal side has
    no `failed` status)."""
    attempts = int(row.get("attempts") or 0) + 1
    error = _error(exc)
    if attempts >= settings.rfp_email_ingestion_classify_max_attempts:
        _hold_at_cap(sb, row, STATUS_CREATE, error, settings)
        return
    _cas(sb, row["id"], STATUS_CREATE, {
        "attempts": attempts,
        "last_error": error,
        "next_attempt_at": _iso(
            _now() + timedelta(seconds=rfp_email_ingest.backoff_seconds(attempts))
        ),
    })


def _retry_or_hold_at(sb, row: dict, expected_status: str, exc: Exception, settings: Settings) -> None:
    """The attempt ladder at any pending status: backoff like the email
    side; at the cap the row stays put with the sentence and a one-hour
    wait (the portal side has no `failed` status)."""
    attempts = int(row.get("attempts") or 0) + 1
    error = _error(exc)
    if attempts >= settings.rfp_email_ingestion_classify_max_attempts:
        _hold_at_cap(sb, row, expected_status, error, settings)
        return
    _cas(sb, row["id"], expected_status, {
        "attempts": attempts,
        "last_error": error,
        "next_attempt_at": _iso(
            _now() + timedelta(seconds=rfp_email_ingest.backoff_seconds(attempts))
        ),
    })


def _step_split(sb, row: dict, settings: Settings, *, renew=None) -> bool:
    """The `split` step (docs/RFP_SPLIT.md 3.2), the email step's twin:
    `rfp_split.advance` stages the Bid File Splitter job for the row's
    harvest, waits on it, or skips. A terminal outcome CASes `split ->
    create` and returns True so the caller runs the create step in the
    same pass; a wait pushes `next_attempt_at` by RFP_SPLIT_POLL_SECONDS
    without spending an attempt (by the model-wait interval, with the reason
    as the row's sentence, when the splitter's model is away); an exception
    walks the ladder. At the ladder's end the split step does NOT hold like
    the other steps (docs/RFP_SPLIT.md 10): `_split_gave_up` marks the
    harvest split failed with the reason and moves the row on to `create`,
    so the project is born with every document whole and the flag. Our own
    AI gate refusing admission (`rfp_email_ingest.gate_busy`) is a wait,
    never an attempt, exactly as on the email side. `renew` is the sweep's
    lease renewal, called by the staging heartbeat (docs/RFP_SPLIT.md 10.5)."""
    harvest = None
    if row.get("harvest_id"):
        try:
            rows = (
                sb.table(HARVESTS_TABLE).select("*").eq("id", row["harvest_id"]).limit(1).execute()
            ).data or []
        except Exception as exc:  # noqa: BLE001
            logger.exception("rfp portal: harvest lookup failed for %s", row.get("id"))
            if _split_gate_wait(sb, row, exc, settings):
                return False
            if _split_ladder_spent(row, settings):
                return _split_gave_up(sb, row, exc)
            _retry_or_hold_at(sb, row, STATUS_SPLIT, exc, settings)
            return False
        harvest = rows[0] if rows else None
    try:
        outcome = rfp_split.advance(
            sb, harvest, settings=settings, context=rfp_split.context_for_portal(row, harvest),
            renew=renew,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("rfp portal: split step failed for %s", row.get("id"))
        if _split_gate_wait(sb, row, exc, settings):
            return False
        if harvest is not None and _split_ladder_spent(row, settings):
            return _split_gave_up(sb, row, exc)
        _retry_or_hold_at(sb, row, STATUS_SPLIT, exc, settings)
        return False
    if outcome.waiting:
        if outcome.model_away:
            _park(sb, row, STATUS_SPLIT, settings.rfp_email_ingestion_classify_retry_seconds, outcome.reason)
        else:
            _park(sb, row, STATUS_SPLIT, settings.rfp_split_poll_seconds, None)
        return False
    moved = _cas(sb, row["id"], STATUS_SPLIT, {
        "status": STATUS_CREATE, "decided_at_step": STATUS_SPLIT, "attempts": 0,
        "last_error": None, "next_attempt_at": None,
    })
    if moved:
        row.update({"status": STATUS_CREATE, "attempts": 0, "last_error": None, "next_attempt_at": None})
    return moved


def _split_gate_wait(sb, row: dict, exc: Exception, settings: Settings) -> bool:
    """Our own AI gate refused admission (`rfp_email_ingest.gate_busy`):
    nothing is wrong with the row, so it waits the model-wait interval with
    the sentence and spends no attempt (the email side's `_retry_or_fail`
    does the same). True when it waited."""
    if not rfp_email_ingest.gate_busy(exc):
        return False
    _park(sb, row, STATUS_SPLIT, settings.rfp_email_ingestion_classify_retry_seconds, _error(exc))
    return True


def _split_ladder_spent(row: dict, settings: Settings) -> bool:
    """This failure is the last attempt `_retry_or_hold_at` allows (a row
    already held at the cap by an older build counts as spent too)."""
    return int(row.get("attempts") or 0) + 1 >= settings.rfp_email_ingestion_classify_max_attempts


def _split_gave_up(sb, row: dict, exc: Exception) -> bool:
    """The email side's `_split_gave_up` twin: the harvest marked split
    failed with the reason (`rfp_split.give_up`), the row `split -> create`
    with attempts reset and the reason as its sentence. True when the row
    moved (the caller runs the create step in the same pass)."""
    reason = rfp_split.give_up(
        sb, row.get("harvest_id"), _error(exc), attempts=int(row.get("attempts") or 0) + 1,
        session_id=row.get("test_session_id"),
    )
    moved = _cas(sb, row["id"], STATUS_SPLIT, {
        "status": STATUS_CREATE, "decided_at_step": STATUS_SPLIT, "attempts": 0,
        "last_error": reason, "next_attempt_at": None,
    })
    if moved:
        row.update({"status": STATUS_CREATE, "attempts": 0, "last_error": reason, "next_attempt_at": None})
    return moved


def _link_to_harvest_project(sb, row: dict) -> bool:
    """The link-only path of the create step (the email step's twin): a
    harvest that already carries a project (an addendum notice, a second
    sighting) links the invitation to it (`created`), flag on or off."""
    harvest_id = row.get("harvest_id")
    if not harvest_id:
        return False
    try:
        rows = (
            sb.table(HARVESTS_TABLE).select("id, project_id").eq("id", harvest_id).limit(1).execute()
        ).data or []
    except Exception:  # noqa: BLE001 - the drain below still happens; the mates pass catches it later
        logger.exception("rfp portal: harvest lookup failed for %s", row.get("id"))
        return False
    project_id = rows[0].get("project_id") if rows else None
    if not project_id:
        return False
    return _cas(sb, row["id"], STATUS_CREATE, rfp_create.created_fields(project_id))


def _park_if_blocked(sb, row: dict, settings: Settings) -> bool:
    """The deleted-project block at the create step (docs/PROJECT_DELETE.md
    5), the email step's twin: an invitation whose own mark, harvest or
    package sibling points at a deleted project drains to `done` with
    flag_reason `project_deleted` and the archive id, whatever the
    auto-create switch says. A failed check never blocks (the service checks
    again before it inserts anything). True when the row was parked."""
    try:
        archive_id = rfp_create.create_block_for(sb, rfp_create.SOURCE_PORTAL, row, settings=settings)
    except Exception:  # noqa: BLE001
        logger.exception("rfp portal: deleted-project check failed for %s", row.get("id"))
        return False
    if not archive_id:
        return False
    _cas(sb, row["id"], STATUS_CREATE, {
        "status": STATUS_DONE, "flag_reason": rfp_create.FLAG_PROJECT_DELETED,
        "decided_at_step": STATUS_CREATE, "next_attempt_at": None, "last_error": None,
        rfp_create.BLOCK_COLUMN: archive_id,
    })
    return True


def _step_create(sb, row: dict, settings: Settings) -> None:
    """The `create` step (docs/RFP_CREATE.md 3), the email step's twin:
    first the link-only path (a harvest that already has a project links
    the row, flag on or off); then, with automatic creation off, or no
    title, the row drains to `done` (the "Create project" button on the
    modal takes over); otherwise rfp_create makes the project and CASes the
    row to `created` itself. A losing claim on the harvest waits
    RFP_CREATE_POLL_SECONDS without spending an attempt; a refusal drains
    with the sentence; anything else walks the ladder above."""
    if _link_to_harvest_project(sb, row):
        return
    if _park_if_blocked(sb, row, settings):
        return
    before = _portal_hook(row.get("portal"), "before_create")
    if before is not None:
        # The portal's own gates (BuildingConnected, BUILD_CONTRACT D12b and
        # D18): a package sibling that already has a project links instead
        # of creating; a row that must not auto-create parks for a person.
        try:
            if before(sb, row, settings):
                return
        except Exception as exc:  # noqa: BLE001
            logger.exception("rfp portal: create gate failed for %s", row.get("id"))
            _retry_or_fail_create(sb, row, exc, settings)
            return
    title = (row.get("title") or "").strip()
    if not settings.rfp_create_auto_enabled or not title:
        _cas(sb, row["id"], STATUS_CREATE, {
            "status": STATUS_DONE, "decided_at_step": STATUS_CREATE, "next_attempt_at": None,
        })
        return
    try:
        rfp_create.create_from_portal(sb, row, actor_id=None, automatic=True)
    except rfp_create.CreateInProgress as exc:
        _park(sb, row, STATUS_CREATE, settings.rfp_create_poll_seconds, _error(exc))
    except rfp_create.CreateWaitingForSplit as exc:
        # The split files the documents first (docs/RFP_SPLIT.md 10.5): the
        # row waits, no attempt spent.
        _park(sb, row, STATUS_CREATE, settings.rfp_split_poll_seconds, _error(exc))
    except rfp_create.CreateBlocked as exc:
        _cas(sb, row["id"], STATUS_CREATE, {
            "status": STATUS_DONE, "flag_reason": rfp_create.FLAG_PROJECT_DELETED,
            "decided_at_step": STATUS_CREATE, "next_attempt_at": None,
            rfp_create.BLOCK_COLUMN: exc.archive_id, "last_error": _error(exc),
        })
    except rfp_create.CreateRefused as exc:
        logger.warning("rfp portal: create refused for %s: %s", row.get("id"), exc)
        _cas(sb, row["id"], STATUS_CREATE, {
            "status": STATUS_DONE, "decided_at_step": STATUS_CREATE, "next_attempt_at": None,
            "last_error": _error(exc),
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("rfp portal: create step failed for %s", row.get("id"))
        _retry_or_fail_create(sb, row, exc, settings)


# ── The harvest job (doc 2.4) ────────────────────────────────────────────────

_INVITATION_JOB_SELECT = (
    "id, portal, agency, agency_key, bid_number, bid_number_raw, addendum_no, title, "
    "close_at, status, flag_reason, harvest_id, harvested_at, attempts, last_error, "
    "next_attempt_at, view_url"
)


def enqueue_harvest(
    invitation_id: str, *, created_by: str | None, force: bool = False,
    settings: Settings | None = None,
) -> dict:
    """One job per invitation; `raise_on_active` so the router can answer
    409. Payload carries `force` (a lease requeue keeps refreshing)."""
    s = settings or get_settings()
    return llm_queue.enqueue(
        JOB_HARVEST,
        target_id=invitation_id,
        payload={"invitation_id": invitation_id, "force": bool(force)},
        created_by=created_by,
        priority=s.rfp_ngem_queue_priority,
        settings=s,
        raise_on_active=True,
    )


def active_harvest_job(invitation_id: str) -> dict | None:
    return llm_queue.active_job(JOB_HARVEST, invitation_id)


def _load_invitation(sb, invitation_id: str, columns: str = _INVITATION_JOB_SELECT) -> dict | None:
    rows = (
        sb.table(INVITATIONS_TABLE).select(columns).eq("id", invitation_id).limit(1).execute()
    ).data or []
    return rows[0] if rows else None


def _load_harvest(sb, harvest_id: str) -> dict | None:
    rows = sb.table(HARVESTS_TABLE).select("*").eq("id", harvest_id).limit(1).execute().data or []
    return rows[0] if rows else None


def _find_harvest(sb, method: str, key: str) -> dict | None:
    rows = (
        sb.table(HARVESTS_TABLE)
        .select("*")
        .eq("method", method)
        .eq("external_key", key)
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else None


def current_status(invitation_id: str) -> str | None:
    """For the queue: the harvest row behind the invitation, every terminal
    harvest status as 'done' (so requeue_terminal refuses; Harvest again is
    the retry path)."""
    sb = get_supabase()
    inv = _load_invitation(sb, invitation_id, "id, harvest_id")
    if inv is None:
        return None
    if not inv.get("harvest_id"):
        return HARVEST_PENDING
    row = _load_harvest(sb, inv["harvest_id"])
    if row is None:
        return HARVEST_PENDING
    return "done" if row.get("status") in (HARVEST_COMPLETE, HARVEST_FAILED) else row.get("status")


def mark_harvest_from_queue(invitation_id: str, status: str, error: str | None) -> None:
    """The queue's terminal failure: the permanent-failure writes, fenced so
    a job that finished normally is not overwritten."""
    if status != HARVEST_FAILED:
        return
    sb = get_supabase()
    inv = _load_invitation(sb, invitation_id)
    if inv is None:
        return
    message = (error or _MSG_INTERRUPTED)[:_ERROR_MAX_CHARS]
    harvest_id = inv.get("harvest_id")
    if not harvest_id:
        row = _find_harvest(
            sb, inv["portal"], external_key(inv["portal"], inv["agency_key"], inv["bid_number"])
        )
        harvest_id = row["id"] if row else None
    if harvest_id:
        sb.table(HARVESTS_TABLE).update(
            {"status": HARVEST_FAILED, "last_error": message, "finished_at": _iso(_now()),
             "claim_token": None}
        ).eq("id", harvest_id).in_("status", [HARVEST_PENDING, HARVEST_RUNNING]).execute()
    if inv.get("status") == STATUS_HARVEST:
        _finish_invitation(sb, inv["id"], harvest_id, error=message)


def error_message(exc: Exception, _label: str) -> str:
    """The sentence the queue stores for a failed portal job: the service's
    own exceptions and the client's carry user sentences; anything else is
    app-authored."""
    if isinstance(exc, (RfpPortalTransient, RfpPortalPermanent)):
        return redact_text(exc) or _MSG_INTERRUPTED
    if type(exc).__module__.endswith("ngem_client"):
        return redact_text(exc) or _MSG_INTERRUPTED
    return _MSG_INTERRUPTED


def _find_or_create_harvest(sb, inv: dict) -> dict:
    method = inv["portal"]
    key = external_key(method, inv["agency_key"], inv["bid_number"])
    row = _find_harvest(sb, method, key)
    if row:
        return row
    try:
        return (
            sb.table(HARVESTS_TABLE)
            .insert(
                {
                    "rfp_email_id": None,
                    "portal_invitation_id": inv["id"],
                    "method": method,
                    "external_key": key,
                    "status": HARVEST_PENDING,
                }
            )
            .execute()
        ).data[0]
    except Exception as exc:  # noqa: BLE001 - only the unique race is expected
        if not _is_unique_violation(exc):
            raise
        row = _find_harvest(sb, method, key)
        if row is None:
            raise
        return row


def _reusable(row: dict, settings: Settings) -> bool:
    if row.get("status") != HARVEST_COMPLETE:
        return False
    finished = _parse_ts(row.get("finished_at"))
    if finished is None:
        return False
    return _now() - finished <= timedelta(days=settings.rfp_harvest_reuse_days)


def _claim_harvest(sb, harvest_id: str, token: str, attempts: int, settings: Settings) -> bool:
    """CAS the harvest row to running under a fresh token: from pending,
    failed or complete, or from a `running` row whose claim is older than
    the queue lease (a dead worker's; see rfp_harvest.stale_claim_filter)."""
    now = _now()
    rows = (
        sb.table(HARVESTS_TABLE)
        .update(
            {
                "status": HARVEST_RUNNING,
                "claim_token": token,
                "attempts": attempts + 1,
                "started_at": _iso(now),
                "finished_at": None,
                "last_error": None,
            }
        )
        .eq("id", harvest_id)
        .or_(rfp_harvest.stale_claim_filter(settings, now))
        .execute()
    ).data or []
    return bool(rows)


def _update_claimed(sb, harvest_id: str, token: str, fields: dict) -> None:
    rows = (
        sb.table(HARVESTS_TABLE)
        .update(fields)
        .eq("id", harvest_id)
        .eq("claim_token", token)
        .eq("status", HARVEST_RUNNING)
        .execute()
    ).data or []
    if not rows:
        raise _ClaimLost()


def _release(sb, harvest_id: str, token: str, status: str, error: str | None) -> None:
    try:
        sb.table(HARVESTS_TABLE).update(
            {
                "status": status,
                "claim_token": None,
                "last_error": error,
                "finished_at": _iso(_now()) if status == HARVEST_FAILED else None,
            }
        ).eq("id", harvest_id).eq("claim_token", token).execute()
    except Exception:  # noqa: BLE001 - the queue records the failure regardless
        logger.exception("rfp portal: harvest release failed for %s", harvest_id)


def _finish_invitation(
    sb, invitation_id: str, harvest_id: str | None, *, error: str | None,
    flag_reason: str | None = None,
) -> bool:
    """Pipeline mode: harvest -> split with the link and the stamp, success
    and permanent failure alike (docs/RFP_SPLIT.md 3: the split step stages
    the harvested documents or falls through to `create`; RFP_CREATE.md 3:
    a failed harvest still creates, flagged "missing files"; the create
    step drains the row to `done` while automatic creation is off)."""
    fields: dict = {
        "status": STATUS_SPLIT,
        "harvest_id": harvest_id,
        "harvested_at": _iso(_now()),
        "decided_at_step": STATUS_HARVEST,
        "attempts": 0,
        "last_error": error,
        "next_attempt_at": None,
    }
    if flag_reason:
        fields["flag_reason"] = flag_reason
    return _cas(sb, invitation_id, STATUS_HARVEST, fields)


def _link_invitation(sb, inv: dict, harvest_id: str, *, any_status: bool = False) -> None:
    """Manual mode: the link and the stamp only, fenced on the status read
    (or, with `any_status`, on the id alone: the harvest happened whatever
    the row moved to meanwhile, and the two fields belong to no step)."""
    query = (
        sb.table(INVITATIONS_TABLE)
        .update({"harvest_id": harvest_id, "harvested_at": _iso(_now())})
        .eq("id", inv["id"])
    )
    if not any_status:
        query = query.eq("status", inv["status"])
    query.execute()


def _park_invitation(sb, inv: dict, *, seconds: float, error: str | None) -> None:
    if inv.get("status") != STATUS_HARVEST:
        return
    _park(sb, inv, STATUS_HARVEST, seconds, error)


def _done(sb, inv: dict, harvest_id: str, pipeline: bool) -> None:
    """Pipeline mode: harvest -> create. When that CAS loses (the row was
    ignored or otherwise moved while the job ran) the harvest still
    happened, so the link and the stamp are written field-scoped, exactly
    what manual mode does."""
    if pipeline:
        if _finish_invitation(sb, inv["id"], harvest_id, error=None):
            return
        logger.info(
            "rfp portal: invitation %s moved off harvest during the job; linking harvest %s only",
            inv["id"], harvest_id,
        )
        _link_invitation(sb, inv, harvest_id, any_status=True)
    else:
        _link_invitation(sb, inv, harvest_id)


def _permanent(
    sb, inv: dict, harvest_id: str | None, message: str, pipeline: bool, *,
    token: str | None = None, flag_reason: str | None = None,
) -> None:
    if harvest_id:
        if token:
            _release(sb, harvest_id, token, HARVEST_FAILED, message)
        else:
            sb.table(HARVESTS_TABLE).update(
                {"status": HARVEST_FAILED, "last_error": message, "finished_at": _iso(_now())}
            ).eq("id", harvest_id).execute()
    if pipeline:
        _finish_invitation(sb, inv["id"], harvest_id, error=message, flag_reason=flag_reason)
    else:
        if harvest_id:
            _link_invitation(sb, inv, harvest_id)
        raise RfpPortalPermanent(message, flag_reason=flag_reason)


# ── Facts and files (pure parts) ─────────────────────────────────────────────


def normalize_facts(inv: dict, facts: Any, portal: NgemPortal) -> dict:
    """The doc 4 `data` document from the event page. Every string capped;
    the notes as an allowlisted fragment; the view URL never included."""
    raw_number = _cap(_attr(facts, "bid_number_raw"), _NUMBER_MAX_CHARS) or inv.get("bid_number_raw")
    number, addendum = inv.get("bid_number"), inv.get("addendum_no")
    if raw_number and raw_number != inv.get("bid_number_raw"):
        # The event page names the bid with its own addendum suffix; the
        # client splits it the way the grid rows are split.
        parsed_number = _cap(_attr(facts, "bid_number"), _NUMBER_MAX_CHARS)
        parsed_addendum = _int_or_none(_attr(facts, "addendum_no"))
        if not parsed_number:
            try:
                parsed_number, parsed_addendum = portal.parse_bid_number(raw_number)
            except Exception:  # noqa: BLE001 - the invitation's own values stand in
                parsed_number, parsed_addendum = None, None
        number = parsed_number or number
        addendum = parsed_addendum if parsed_addendum is not None else addendum
    contact = _attr(facts, "contact") or {}
    if not isinstance(contact, dict):
        contact = {k: _attr(contact, k) for k in ("workgroup", "name", "address", "phone", "email")}
    return {
        "platform": inv["portal"],
        "agency": inv.get("agency"),
        "bid_number": number,
        "bid_number_raw": raw_number,
        "addendum_no": addendum,
        "title": _cap(_attr(facts, "title"), _TITLE_MAX_CHARS) or inv.get("title"),
        "bid_type": _cap(_attr(facts, "bid_type"), _SHORT_MAX_CHARS),
        "bid_status": _cap(_attr(facts, "status"), _SHORT_MAX_CHARS),
        "issued_at": _iso_or_none(_attr(facts, "issued_at")),
        "close_at": _iso_or_none(_attr(facts, "close_at")),
        "question_cutoff_at": _iso_or_none(_attr(facts, "question_cutoff_at")),
        "contact": {
            key: _cap(contact.get(key), _CONTACT_MAX_CHARS)
            for key in ("workgroup", "name", "address", "phone", "email")
        },
        "notes_html": sanitize_notes(_attr(facts, "notes_html")),
        "documents": {"count": 0, "bytes": 0},
    }


def notes_text(facts: Any) -> str | None:
    text = rfp_harvest.html_to_text(_attr(facts, "notes_html"))
    if text:
        return text
    return rfp_harvest.html_to_text(_attr(facts, "notes_text"))


def attachment_entries(rows: list) -> tuple[list[dict], list[str | None]]:
    """The attachments grid as harvest file entries (no URLs) plus the
    aligned download URLs, which are used once and never stored."""
    entries: list[dict] = []
    urls: list[str | None] = []
    for i, row in enumerate(rows or []):
        index = _int_or_none(_attr(row, "index"))
        entries.append(
            {
                "index": index if index is not None else i + 1,
                "file_name": safe_filename(_attr(row, "file_name"), f"attachment-{i + 1}"),
                "description": _cap(_attr(row, "description"), _DESCRIPTION_MAX_CHARS),
                "size": max(0, _int_or_none(_attr(row, "size_bytes")) or 0),
                "sandbox_file_id": None,
                "status": None,
                "error": None,
            }
        )
        url = _attr(row, "download_url")
        urls.append(url if isinstance(url, str) and url else None)
    return entries, urls


def documents_summary(entries: list[dict]) -> dict:
    return {
        "count": len(entries),
        "bytes": sum(int(e.get("size") or 0) for e in entries),
    }


# ── Facts and files (the job's I/O) ──────────────────────────────────────────


def _grid_row_for(page: Any, inv: dict) -> Any:
    """The grid row that keys like the invitation, derived exactly as the
    scan derives the stored key (the same agency fallback included)."""
    wanted = (inv.get("agency_key"), inv.get("bid_number"))
    for row in _attr(page, "rows") or []:
        if _row_key(row) == wanted:
            return row
    return None


def _fetch_event(session, sb, inv: dict, portal: NgemPortal, settings: Settings) -> Any:
    """The bid's Event Details through the stored view link; a stale token
    is refreshed from the grid once, else the harvest ends on stale_link."""
    view_url = inv.get("view_url")
    if view_url:
        try:
            return session.fetch_event(view_url)
        except Exception as exc:  # noqa: BLE001 - only the stale link is handled here
            if _kind_of(portal, exc) in (KIND_LOCKED, KIND_UNAVAILABLE, KIND_TRANSIENT):
                raise
            if not portal.is_stale_link(exc):
                raise
    _renew()
    page = session.fetch_invitations(settings.rfp_ngem_max_list_pages)
    fresh = _grid_row_for(page, inv)
    fresh_url = _cap(_attr(fresh, "view_url"), 2000) if fresh is not None else None
    if not fresh_url:
        raise RfpPortalPermanent(_MSG_STALE_LINK, flag_reason=FLAG_STALE_LINK)
    sb.table(INVITATIONS_TABLE).update({"view_url": fresh_url}).eq("id", inv["id"]).execute()
    inv["view_url"] = fresh_url
    _renew()
    try:
        return session.fetch_event(fresh_url)
    except Exception as exc:  # noqa: BLE001 - a second stale answer keeps its flag
        if portal.is_stale_link(exc):
            raise RfpPortalPermanent(_MSG_STALE_LINK_AGAIN, flag_reason=FLAG_STALE_LINK) from exc
        raise


def _scratch_dir(settings: Settings) -> Path:
    base = settings.rfp_ingest_scratch_dir or tempfile.gettempdir()
    root = Path(base) / "rfp-portal"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="rfp-portal-", dir=root))


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _download_one(
    session, portal: NgemPortal, url: str, dest: Path, *, max_bytes: int,
    referer: str | None = None,
) -> tuple[bytes, str | None]:
    """Up to _DOWNLOAD_ATTEMPTS paced tries on transient trouble; the bytes
    are read back once and the scratch file removed. Returns the bytes and
    the filename the portal declared (sanitized), if any. A lost queue lease
    (_LeaseLost, raised by the renew seam before the request) is never
    retried: the job has to stand down at once."""
    last: Exception | None = None
    for attempt in range(_DOWNLOAD_ATTEMPTS):
        _unlink_quietly(dest)
        try:
            result = session.download(url, dest, max_bytes, referer=referer)
            data = dest.read_bytes()
            _unlink_quietly(dest)
            declared = _attr(result, "filename")
            return data, (safe_filename(declared) if declared else None)
        except Exception as exc:  # noqa: BLE001 - classified below
            if isinstance(exc, _LeaseLost) or _kind_of(portal, exc) != KIND_TRANSIENT:
                _unlink_quietly(dest)
                raise
            last = exc
            if attempt + 1 < _DOWNLOAD_ATTEMPTS:
                time.sleep(2.0 * (attempt + 1))
    _unlink_quietly(dest)
    raise last or RfpPortalTransient("The portal did not answer the download.")


def _existing_run(sb, run_id: str | None) -> dict | None:
    if not run_id:
        return None
    rows = (
        sb.table(rfp_ingest.RUNS_TABLE).select("id, status").eq("id", run_id).limit(1).execute()
    ).data or []
    return rows[0] if rows else None


def _harvest_files(
    sb, session, portal: NgemPortal, settings: Settings, harvest: dict, token: str,
    inv: dict, entries: list[dict], urls: list[str | None], *, referer: str | None = None,
    prior_files: Any = None,
) -> tuple[str | None, int, int]:
    """Download every attachment into one sandbox run. Returns (run_id,
    accepted, bytes). Raises _ClaimLost, _LeaseLost, RfpPortalTransient and
    the locked / unavailable family; per-file trouble is recorded on the
    entry. `prior_files` is the harvest row's file list from before this
    attempt, read only when the run it went into is reused."""
    if not entries:
        return None, 0, 0
    run = _existing_run(sb, harvest.get("sandbox_run_id"))
    if run is not None and run.get("status") in ("staging", "pending"):
        # An interrupted attempt's run: the files it already accepted stay
        # accepted (matched by name and declared size), never downloaded twice.
        carried = rfp_harvest.carry_over_accepted(
            entries, prior_files, key_fields=("file_name", "size")
        )
        if carried:
            logger.info(
                "rfp portal: resuming harvest %s on run %s past %s accepted file(s)",
                harvest["id"], run["id"], carried,
            )
    if run is not None and run.get("status") == "pending":
        # Started but never dispatched (the worker died between the two):
        # nothing is left to download, the run only needs its job.
        accepted = sum(1 for e in entries if e.get("status") == FILE_ACCEPTED and e.get("sandbox_file_id"))
        total = sum(int(e.get("size") or 0) for e in entries if e.get("status") == FILE_ACCEPTED)
        _update_claimed(
            sb, harvest["id"], token,
            {"files": entries, "files_accepted": accepted, "bytes_downloaded": total},
        )
        _renew()
        rfp_ingest.dispatch(run["id"], created_by=None, background=None)
        return run["id"], accepted, total
    if run is None or run.get("status") != "staging":
        run = rfp_ingest.create_portal_run(portal_invitation_id=inv["id"], harvest_id=harvest["id"])
        _update_claimed(sb, harvest["id"], token, {"sandbox_run_id": run["id"]})
    run_id = run["id"]
    scratch = _scratch_dir(settings)
    accepted = 0
    total = 0
    try:
        for index, (entry, url) in enumerate(zip(entries, urls)):
            _renew()
            if entry.get("status") == FILE_ACCEPTED and entry.get("sandbox_file_id"):
                accepted += 1
                total += int(entry.get("size") or 0)
                continue
            if not url:
                entry["status"] = FILE_DOWNLOAD_FAILED
                entry["error"] = _MSG_NO_DOWNLOAD_LINK
                continue
            if int(entry.get("size") or 0) > settings.rfp_ingest_max_file_bytes:
                entry["status"] = FILE_TOO_LARGE
                entry["error"] = _MSG_LARGER_THAN_SANDBOX
                continue
            dest = scratch / f"{index:04d}.bin"
            try:
                data, declared = _download_one(
                    session, portal, url, dest, max_bytes=settings.rfp_ingest_max_file_bytes,
                    referer=referer,
                )
            except Exception as exc:  # noqa: BLE001 - per-file outcomes; the rest propagates
                if isinstance(exc, _LeaseLost):
                    raise
                kind = _kind_of(portal, exc)
                if kind == KIND_PERMANENT:
                    entry["status"] = FILE_TOO_LARGE if "larger" in str(exc).lower() else FILE_DOWNLOAD_FAILED
                    entry["error"] = _error(exc)
                    continue
                if kind == KIND_TRANSIENT:
                    entry["status"] = FILE_DOWNLOAD_FAILED
                    entry["error"] = _error(exc)
                    continue
                raise
            filename = declared or entry["file_name"]
            source = {"kind": portal.key, "file_name": entry["file_name"], "harvest_id": harvest["id"]}
            try:
                frow = rfp_ingest.add_upload_file(
                    run_id, filename=filename, declared_mime=None, data=data, source=source
                )
            except rfp_ingest.RfpIngestDuplicate as exc:
                entry["sandbox_file_id"] = exc.existing_file_id
                entry["status"] = FILE_ACCEPTED
                accepted += 1
                total += len(data)
                continue
            except rfp_ingest.RfpIngestPermanent as exc:
                if exc.http_status == 413:
                    entry["status"] = FILE_SKIPPED_CAP
                    entry["error"] = _error(exc)
                    for later in entries[index + 1:]:
                        later["status"] = FILE_SKIPPED_CAP
                        later["error"] = _error(exc)
                    break
                if exc.http_status == 409:
                    raise RfpPortalTransient(_error(exc)) from exc
                entry["status"] = FILE_DOWNLOAD_FAILED
                entry["error"] = _error(exc)
                continue
            finally:
                del data
            entry["sandbox_file_id"] = frow.get("id")
            if frow.get("status") == "rejected":
                entry["status"] = FILE_REJECTED
                entry["error"] = _cap(frow.get("error"), _ERROR_MAX_CHARS)
            elif frow.get("status") == "failed":
                entry["status"] = FILE_DOWNLOAD_FAILED
                entry["error"] = _cap(frow.get("error"), _ERROR_MAX_CHARS)
            else:
                entry["status"] = FILE_ACCEPTED
                accepted += 1
                total += int(frow.get("size_bytes") or 0)
            _update_claimed(
                sb, harvest["id"], token,
                {"files": entries, "files_accepted": accepted, "bytes_downloaded": total},
            )
    finally:
        try:
            for child in scratch.iterdir():
                _unlink_quietly(child)
            scratch.rmdir()
        except OSError:
            pass
    if accepted == 0:
        try:
            rfp_ingest.delete_run(run_id)
        except Exception:  # noqa: BLE001 - an empty staging run is pruned later anyway
            logger.warning("rfp portal: could not delete the empty run %s", run_id)
        _update_claimed(sb, harvest["id"], token, {"sandbox_run_id": None})
        return None, 0, 0
    _renew()
    rfp_ingest.start_run(run_id)
    rfp_ingest.dispatch(run_id, created_by=None, background=None)
    return run_id, accepted, total


def execute_harvest(invitation_id: str, *, force: bool = False) -> None:
    """The rfp_portal_harvest job (doc 2.4). Pipeline mode (the row at
    `harvest`) moves it to create; any other status is manual mode (`force`
    from Harvest again, or a row that moved while the job waited): the
    status never moves, only harvest_id / harvested_at."""
    settings = get_settings()
    sb = get_supabase()
    inv = _load_invitation(sb, invitation_id)
    if inv is None:
        raise RfpPortalPermanent(_MSG_INVITATION_NOT_FOUND, http_status=404)
    pipeline = inv.get("status") == STATUS_HARVEST
    if not force and inv.get("status") == STATUS_IGNORED:
        # A queued pipeline job whose invitation was ignored meanwhile: the
        # person said not to open this bid on the portal. Only a forced
        # (manual) run may.
        logger.info("rfp portal: invitation %s is ignored; the queued harvest does nothing", invitation_id)
        return
    portal = _portal(inv.get("portal"))

    harvest = _find_or_create_harvest(sb, inv)
    if not force and _reusable(harvest, settings):
        _done(sb, inv, harvest["id"], pipeline)
        return

    token = uuid.uuid4().hex
    prior_files = list(harvest.get("files") or [])
    if not _claim_harvest(sb, harvest["id"], token, int(harvest.get("attempts") or 0), settings):
        logger.warning(
            "rfp portal: lost the claim on harvest %s for invitation %s (row %s since %s under "
            "another token); %s",
            harvest["id"], invitation_id, harvest.get("status"), harvest.get("started_at"),
            "parking the invitation" if pipeline else "reporting transient",
        )
        if pipeline:
            _park_invitation(sb, inv, seconds=settings.rfp_harvest_poll_seconds, error=_MSG_CLAIMED)
            return
        raise RfpPortalTransient(_MSG_CLAIMED)

    try:
        usable, reason, until = portal.availability(settings)
        if not usable:
            raise _Unavailable(reason, until)
        with closing(portal.open_session(settings)) as session:
            _renew()
            facts = _fetch_event(session, sb, inv, portal, settings)
            data = normalize_facts(inv, facts, portal)
            head = {
                "external_url": portal.entry_url(settings),
                "data": data,
                "description_text": notes_text(facts),
                "facts_at": _iso(_now()),
            }
            _update_claimed(sb, harvest["id"], token, head)
            harvest.update(head)
            _renew()
            rows = list(session.fetch_attachments(facts, settings.rfp_ngem_max_list_pages) or [])
            entries, urls = attachment_entries(rows)
            data["documents"] = documents_summary(entries)
            _update_claimed(
                sb, harvest["id"], token,
                {"data": data, "files": entries, "file_count": len(entries)},
            )
            if len(entries) > settings.rfp_harvest_file_cap:
                raise RfpPortalPermanent(
                    _MSG_TOO_MANY_FILES.format(n=len(entries), cap=settings.rfp_harvest_file_cap)
                )
            declared = sum(int(e.get("size") or 0) for e in entries)
            if declared > settings.rfp_harvest_max_total_bytes:
                raise RfpPortalPermanent(
                    _MSG_TOO_MANY_BYTES.format(
                        mb=declared // (1024 * 1024),
                        cap=settings.rfp_harvest_max_total_bytes // (1024 * 1024),
                    )
                )
            run_id, accepted, total = _harvest_files(
                sb, session, portal, settings, harvest, token, inv, entries, urls,
                referer=_attr(facts, "attachments_url"), prior_files=prior_files,
            )
            _update_claimed(
                sb, harvest["id"], token,
                {
                    "status": HARVEST_COMPLETE,
                    "sandbox_run_id": run_id,
                    "files": entries,
                    "files_accepted": accepted,
                    "bytes_downloaded": total,
                    "finished_at": _iso(_now()),
                    "last_error": None if entries else _MSG_NO_FILES,
                    "claim_token": None,
                },
            )
    except _ClaimLost:
        logger.warning("rfp portal: claim lost on %s for invitation %s", harvest["id"], invitation_id)
        if pipeline:
            _park_invitation(sb, inv, seconds=settings.rfp_harvest_poll_seconds, error=_MSG_CLAIMED)
            return
        raise RfpPortalTransient(_MSG_CLAIMED) from None
    except Exception as exc:  # noqa: BLE001 - classified below
        kind = _kind_of(portal, exc)
        if kind in (KIND_LOCKED, KIND_UNAVAILABLE):
            # Nothing a retry fixes on its own: pipeline rows wait (no attempt
            # spent); manual runs report it.
            until = _parse_ts(getattr(exc, "locked_until", None))
            if pipeline:
                _release(sb, harvest["id"], token, HARVEST_PENDING, _error(exc))
                _park_invitation(
                    sb, inv,
                    seconds=_wait_seconds(settings, until, settings.rfp_harvest_poll_seconds),
                    error=_error(exc),
                )
                return
            _release(sb, harvest["id"], token, HARVEST_FAILED, _error(exc))
            _link_invitation(sb, inv, harvest["id"])
            raise RfpPortalPermanent(_error(exc)) from exc
        if kind == KIND_PERMANENT:
            _permanent(
                sb, inv, harvest["id"], _error(exc), pipeline, token=token,
                flag_reason=getattr(exc, "flag_reason", None),
            )
            return
        if kind == KIND_TRANSIENT:
            _release(sb, harvest["id"], token, HARVEST_PENDING, _error(exc))
            if not pipeline:
                _link_invitation(sb, inv, harvest["id"])
            raise RfpPortalTransient(_error(exc)) from exc
        logger.exception("rfp portal: unexpected failure harvesting invitation %s", invitation_id)
        _release(sb, harvest["id"], token, HARVEST_PENDING, _MSG_INTERRUPTED)
        raise RfpPortalTransient(_MSG_INTERRUPTED) from exc
    _done(sb, inv, harvest["id"], pipeline)


# ── Availability and status (router reads) ───────────────────────────────────


def availability(
    portal: str = PORTAL_NGEM, settings: Settings | None = None
) -> tuple[bool, str | None, datetime | None]:
    """(usable, reason, locked_until) without touching the portal."""
    s = settings or get_settings()
    return _portal(portal).availability(s)


def _active_job_count(sb) -> int:
    try:
        rows = (
            sb.table("llm_jobs")
            .select("id", count="exact")
            .in_("job_type", [JOB_SCAN, JOB_HARVEST])
            .in_("status", ["queued", "running"])
            .execute()
        )
        return rows.count if rows.count is not None else len(rows.data or [])
    except Exception:  # noqa: BLE001
        logger.debug("rfp portal: active job count failed", exc_info=True)
        return 0


def _latest_run(sb, portal: str) -> dict | None:
    rows = (
        sb.table(RUNS_TABLE)
        .select("*")
        .eq("portal", portal)
        .order("created_at", desc=True)
        .order("id", desc=True)
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else None


def _active_run(sb, portal: str) -> dict | None:
    rows = (
        sb.table(RUNS_TABLE)
        .select("*")
        .eq("portal", portal)
        .in_("status", list(RUN_ACTIVE))
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else None


def _profile_names(sb, ids) -> dict[str, str | None]:
    clean = sorted({str(i) for i in ids if i})
    if not clean:
        return {}
    rows = sb.table("profiles").select("id, full_name").in_("id", clean).execute().data or []
    return {r["id"]: r.get("full_name") for r in rows if r.get("id")}


def _run_view(run: dict | None, names: dict[str, str | None]) -> dict | None:
    if not run:
        return None
    out = dict(run)
    out["requested_by_name"] = names.get(run.get("requested_by")) if run.get("requested_by") else None
    return out


def session_status(portal: str = PORTAL_NGEM, settings: Settings | None = None) -> dict:
    """For the settings block: never the cookies, never the password."""
    s = settings or get_settings()
    sb = get_supabase()
    adapter = _portal(portal)
    state = SessionStore(s, portal).load() or {}
    now = _now()
    usable, _reason, locked_until = adapter.availability(s)
    last_run = _latest_run(sb, portal)
    active_run = _active_run(sb, portal)
    names = _profile_names(sb, {(r or {}).get("requested_by") for r in (last_run, active_run)})
    upcoming = next_run_at(now, s)
    return {
        "enabled": bool(s.rfp_ingest_enabled and s.rfp_ngem_enabled),
        "configured": adapter.configured(s),
        "account": adapter.account(s),
        "logged_in_at": state.get("logged_in_at"),
        "last_used_at": state.get("last_used_at"),
        "last_login_attempt_at": state.get("last_login_attempt_at"),
        "login_failures": int(state.get("login_failures") or 0),
        "locked_until": _iso(locked_until) if locked_until else None,
        "last_error": state.get("last_error"),
        "schedule_times": [f"{h:02d}:{m:02d}" for h, m in s.rfp_ngem_schedule],
        "next_run_at": _iso(upcoming) if upcoming else None,
        "last_run": _run_view(last_run, names),
        "active_run": _run_view(active_run, names),
        "active_jobs": _active_job_count(sb),
    }


# ── Reads for the router (doc section 5) ─────────────────────────────────────

# The BuildingConnected columns the tab shows (0136, BUILD_CONTRACT section
# 4); `payload` (the raw mirror, lead contact data included) is NEVER in the
# list and reaches the detail only for the IT Admin (the router pops it).
_LIST_BC_COLUMNS = (
    "external_id, external_url, gc_id, gc_kind, gc_external_id, gc_external_name, "
    "gc_confirmed_at, gc_candidates, trade_name, invited_at, job_walk_at, expected_start_at, "
    "expected_finish_at, submission_state, workflow_bucket, is_archived, is_nda_required, "
    "address, ignore_source"
)
_LIST_SELECT = (
    "id, portal, agency, bid_number, bid_number_raw, addendum_no, title, close_at, issued_on, "
    "status, flag_reason, response_status, match_project_id, match_score, harvest_id, "
    "last_seen_at, missing_since, change_log, created_project_id, create_blocked_archive_id, "
    + _LIST_BC_COLUMNS
)
# The detail: every column but view_url, named, so a later column is a
# deliberate addition rather than a leak.
_DETAIL_SELECT = (
    "id, portal, agency, agency_key, bid_number, bid_number_raw, addendum_no, title, "
    "issued_on, close_at, time_left, bid_status, response_status, response_code, status_code, "
    "status, flag_reason, decided_at_step, attempts, next_attempt_at, last_error, "
    "match_candidates, match_project_id, match_score, match_llm_model, "
    "match_llm_prompt_version, match_weights, matched_at, possible_rebid_project_id, "
    "possible_rebid_score, excluded_project_ids, resolution, resolved_by, resolved_at, "
    "reopen_reason, ignored_by, ignored_at, ignore_reason, harvest_id, harvested_at, "
    "change_log, first_seen_at, last_seen_at, first_seen_run_id, last_seen_run_id, "
    "seen_count, missing_since, created_at, updated_at, created_project_id, "
    "create_blocked_archive_id, "
    + _LIST_BC_COLUMNS
    + ", lead, sibling_of, project_gc_id, gc_confirmed_by, rfis_due_at, source, request_type, "
    "is_sealed, bc_updated_at, payload"
)
_BC_STATE_RE = re.compile(r"^[A-Z_]{1,40}$")
_PUBLIC_HARVEST_COLUMNS = (
    "id, rfp_email_id, portal_invitation_id, method, external_key, external_url, status, "
    "attempts, last_error, data, description_text, instructions_text, files, file_count, "
    "files_accepted, bytes_downloaded, sandbox_run_id, facts_at, started_at, finished_at, "
    "created_at, updated_at"
)


def _projects_by_id(sb, ids) -> dict[str, dict]:
    clean = sorted({str(i) for i in ids if i})
    if not clean:
        return {}
    out: dict[str, dict] = {}
    for chunk in _chunks(clean):
        rows = sb.table("projects").select("id, name, number").in_("id", chunk).execute().data or []
        for r in rows:
            if r.get("id"):
                out[str(r["id"])] = {"id": r["id"], "name": r.get("name"), "number": r.get("number")}
    return out


def _harvest_status_by_id(sb, ids) -> dict[str, str]:
    wanted = sorted({str(i) for i in ids if i})
    if not wanted:
        return {}
    rows = sb.table(HARVESTS_TABLE).select("id, status").in_("id", wanted).execute().data or []
    return {r["id"]: r.get("status") for r in rows if r.get("id")}


def _changed_recently(row: dict, now: datetime) -> bool:
    lo = now - timedelta(days=_CHANGED_RECENTLY_DAYS)
    for entry in row.get("change_log") or []:
        at = _parse_ts((entry or {}).get("at"))
        if at and at >= lo:
            return True
    return False


def _clean_query(q: str | None) -> str | None:
    if not q:
        return None
    text = _QUERY_CLEAN_RE.sub("", str(q)).strip()[:_QUERY_MAX_CHARS]
    return text or None


def _gc_names(sb, ids) -> dict[str, str | None]:
    clean = sorted({str(i) for i in ids if i})
    if not clean:
        return {}
    out: dict[str, str | None] = {}
    for chunk in _chunks(clean):
        rows = sb.table("general_contractors").select("id, name").in_("id", chunk).execute().data or []
        for r in rows:
            if r.get("id"):
                out[str(r["id"])] = r.get("name")
    return out


def _changes_recent(row: dict, now: datetime) -> int:
    lo = now - timedelta(days=_CHANGED_RECENTLY_DAYS)
    n = 0
    for entry in row.get("change_log") or []:
        at = _parse_ts((entry or {}).get("at"))
        if at and at >= lo:
            n += 1
    return n


def _gc_block(row: dict, gc_names: dict[str, str | None]) -> dict:
    """The GC as BuildingConnected names it and as we resolved it (contract
    section 4): `{external_id, external_name, gc_id, gc_name, kind,
    confirmed_at, candidates}`."""
    gc_id = str(row.get("gc_id")) if row.get("gc_id") else None
    candidates = row.get("gc_candidates")
    return {
        "external_id": row.get("gc_external_id"),
        "external_name": row.get("gc_external_name"),
        "gc_id": gc_id,
        "gc_name": gc_names.get(gc_id) if gc_id else None,
        "kind": row.get("gc_kind"),
        "confirmed_at": row.get("gc_confirmed_at"),
        "candidates": [c for c in candidates if isinstance(c, dict)] if isinstance(candidates, list) else [],
    }


def _bc_item_fields(row: dict, gc_names: dict[str, str | None], now: datetime) -> dict:
    """The list item fields a BuildingConnected row adds (contract 4)."""
    return {
        "external_id": row.get("external_id"),
        "external_url": row.get("external_url"),
        "gc": _gc_block(row, gc_names),
        "trade_name": row.get("trade_name"),
        "invited_at": row.get("invited_at"),
        "job_walk_at": row.get("job_walk_at"),
        "expected_start_at": row.get("expected_start_at"),
        "expected_finish_at": row.get("expected_finish_at"),
        "submission_state": row.get("submission_state"),
        "workflow_bucket": row.get("workflow_bucket"),
        "is_archived": row.get("is_archived"),
        "is_nda_required": row.get("is_nda_required"),
        "address": row.get("address"),
        "ignore_source": row.get("ignore_source"),
        "changes_recent": _changes_recent(row, now),
    }


def _clean_state(state: str | None) -> str | None:
    """The optional submission_state filter of the BuildingConnected list:
    one upper-case token (UNDECIDED, WILL_SUBMIT, SUBMITTED, DECLINED) or
    nothing; anything else filters nothing."""
    if not state:
        return None
    text = str(state).strip().upper()
    return text if _BC_STATE_RE.match(text) else None


def list_invitations(
    sb, *, portal: str, view: str, q: str | None, limit: int, offset: int,
    state: str | None = None,
) -> dict:
    """`{items, total, offset, limit, counts}` for one view, newest close_at
    first (nulls last on the BuildingConnected tab, where a row may have no
    due date). Each row carries `match_project`, `harvest_status` and
    `changed_recently`; the router withholds match_score by role. The lanes
    are the portal's (VIEWS_BY_PORTAL); `state` filters a BuildingConnected
    list on submission_state."""
    views = VIEWS_BY_PORTAL.get(portal, VIEWS)
    statuses = views.get(view) or views["all"]
    bc = portal == PORTAL_BUILDINGCONNECTED
    limit = max(1, min(int(limit), _LIST_MAX))
    offset = max(0, int(offset))
    query = (
        sb.table(INVITATIONS_TABLE)
        .select(_LIST_SELECT, count="exact")
        .eq("portal", portal)
        .in_("status", list(statuses))
    )
    needle = _clean_query(q)
    if needle and bc:
        query = query.or_(
            f"title.ilike.*{needle}*,gc_external_name.ilike.*{needle}*,trade_name.ilike.*{needle}*"
        )
    elif needle:
        query = query.or_(
            f"agency.ilike.*{needle}*,bid_number_raw.ilike.*{needle}*,title.ilike.*{needle}*"
        )
    state_value = _clean_state(state) if bc else None
    if state_value:
        query = query.eq("submission_state", state_value)
    if bc:
        query = query.order("close_at", desc=True, nullsfirst=False)
    else:
        query = query.order("close_at", desc=True)
    resp = (
        query.order("id", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    rows = resp.data or []
    projects = _projects_by_id(
        sb, {r.get("match_project_id") for r in rows} | {r.get("created_project_id") for r in rows}
    )
    harvests = _harvest_status_by_id(sb, {r.get("harvest_id") for r in rows})
    gc_names = _gc_names(sb, {r.get("gc_id") for r in rows}) if bc else {}
    now = _now()
    items = []
    for row in rows:
        item = {
            "id": row.get("id"),
            "portal": row.get("portal"),
            "agency": row.get("agency"),
            "bid_number": row.get("bid_number"),
            "bid_number_raw": row.get("bid_number_raw"),
            "addendum_no": row.get("addendum_no"),
            "title": row.get("title"),
            "close_at": row.get("close_at"),
            "issued_on": row.get("issued_on"),
            "status": row.get("status"),
            "flag_reason": row.get("flag_reason"),
            "response_status": row.get("response_status"),
            "match_project": projects.get(str(row.get("match_project_id") or "")),
            "created_project_id": row.get("created_project_id"),
            "created_project": projects.get(str(row.get("created_project_id") or "")),
            # A project made from this invitation was deleted (docs/PROJECT_DELETE.md 5).
            "create_blocked_archive_id": row.get("create_blocked_archive_id"),
            "match_score": row.get("match_score"),
            "harvest_status": (
                "pending"
                if row.get("status") == STATUS_HARVEST and not row.get("harvest_id")
                else harvests.get(row.get("harvest_id"))
            ),
            "last_seen_at": row.get("last_seen_at"),
            "missing_since": row.get("missing_since"),
            "changed_recently": _changed_recently(row, now),
        }
        if bc:
            item.update(_bc_item_fields(row, gc_names, now))
        items.append(item)
    total = resp.count if resp.count is not None else len(rows)
    return {"items": items, "total": total, "offset": offset, "limit": limit,
            "counts": view_counts(sb, portal)}


def _status_tally(sb, portal: str) -> dict[str, int]:
    """How many of the portal's rows sit in each status, from ONE paged read
    of the `status` column only (_PAGE rows a page, stable on id). The first
    page asks for the exact total and the pages advance by the rows actually
    received, so a PostgREST max-rows cap below _PAGE costs extra pages but
    can never undercount a busy portal; without a total the read stops on a
    short page, like _page_all. An empty page always ends it."""
    tally: dict[str, int] = {}
    received = 0
    total: int | None = None
    first = True
    while True:
        select_kwargs = {"count": "exact"} if first else {}
        resp = (
            sb.table(INVITATIONS_TABLE)
            .select("status", **select_kwargs)
            .eq("portal", portal)
            .order("id")
            .range(received, received + _PAGE - 1)
            .execute()
        )
        page = resp.data or []
        if first and resp.count is not None:
            total = int(resp.count)
        first = False
        for row in page:
            status = row.get("status")
            tally[status] = tally.get(status, 0) + 1
        received += len(page)
        if not page:
            return tally
        if total is not None:
            if received >= total:
                return tally
        elif len(page) < _PAGE:
            return tally


def view_counts(sb, portal: str) -> dict[str, int]:
    """The per-view counts of the tab, tallied in Python over the portal's
    lanes from one paged read of the status column (_status_tally): 2,543
    rows cost 3 round trips, where one exact-count HEAD query per lane cost
    7 on the BuildingConnected tab (5 on NGEM) on every list call."""
    tally = _status_tally(sb, portal)
    return {
        name: sum(tally.get(status, 0) for status in set(members))
        for name, members in VIEWS_BY_PORTAL.get(portal, VIEWS).items()
    }


def harvest_for_invitation(sb, row: dict) -> dict | None:
    """The harvest row the detail shows: never raw, never claim_token. Falls
    back to the platform object's row when the invitation is not linked yet."""
    harvest_id = row.get("harvest_id")
    hit = None
    if harvest_id:
        rows = (
            sb.table(HARVESTS_TABLE).select(_PUBLIC_HARVEST_COLUMNS).eq("id", harvest_id).limit(1).execute()
        ).data or []
        hit = rows[0] if rows else None
    if hit is None and row.get("agency_key") and row.get("bid_number"):
        rows = (
            sb.table(HARVESTS_TABLE)
            .select(_PUBLIC_HARVEST_COLUMNS)
            .eq("method", row.get("portal"))
            .eq("external_key", external_key(row["portal"], row["agency_key"], row["bid_number"]))
            .limit(1)
            .execute()
        ).data or []
        hit = rows[0] if rows else None
    if hit is not None and isinstance(hit.get("files"), list):
        # The file entries as doc section 4 lists them and nothing else: a
        # download URL is never stored, and this read path could not hand
        # one out even if a row carried it.
        hit["files"] = [
            {key: f.get(key) for key in _PUBLIC_FILE_KEYS} for f in hit["files"] if isinstance(f, dict)
        ]
    return hit


def detail(sb, invitation_id: str) -> dict | None:
    """The detail (doc section 5) before the router's per-role redaction:
    the row without view_url, the candidates with names refreshed,
    `match_project`, `excluded_projects`, `possible_rebid`, `harvest`,
    `harvest_job`, `harvest_available` and the actor names."""
    rows = (
        sb.table(INVITATIONS_TABLE).select(_DETAIL_SELECT).eq("id", invitation_id).limit(1).execute()
    ).data or []
    if not rows:
        return None
    row = rows[0]
    row.pop("view_url", None)
    candidates = [dict(c) for c in (row.get("match_candidates") or []) if isinstance(c, dict)]
    wanted = {row.get("match_project_id"), row.get("possible_rebid_project_id")}
    wanted |= {c.get("project_id") for c in candidates}
    wanted |= {str(x) for x in (row.get("excluded_project_ids") or [])}
    wanted.add(row.get("created_project_id"))
    projects = _projects_by_id(sb, wanted)
    for c in candidates:
        live = projects.get(str(c.get("project_id") or ""))
        if live:
            c["name"] = live.get("name") or c.get("name")
            c["number"] = live.get("number") or c.get("number")
    row["match_candidates"] = candidates
    row["match_project"] = projects.get(str(row.get("match_project_id") or ""))
    # The creation slice (docs/RFP_CREATE.md 8): the project this row became,
    # and whether the modal's "Create project" button shows.
    row["created_project"] = projects.get(str(row.get("created_project_id") or ""))
    row["create_available"] = rfp_create.portal_create_available(row)
    # The deleted project this invitation made, while it stays deleted
    # (docs/PROJECT_DELETE.md 5): who, when and why, for the modal's notice.
    from app.services import project_delete

    row["deleted_project"] = project_delete.archive_ref(sb, row.get("create_blocked_archive_id"))
    row["excluded_projects"] = [
        projects.get(str(pid)) or {"id": str(pid), "name": None, "number": None}
        for pid in (row.get("excluded_project_ids") or [])
    ]
    rebid_id = row.get("possible_rebid_project_id")
    rebid = projects.get(str(rebid_id)) if rebid_id else None
    row["possible_rebid"] = (
        {
            "id": rebid_id,
            "name": (rebid or {}).get("name"),
            "number": (rebid or {}).get("number"),
            "score": row.get("possible_rebid_score"),
        }
        if rebid_id
        else None
    )
    names = _profile_names(sb, {row.get("resolved_by"), row.get("ignored_by")})
    row["resolved_by_name"] = names.get(row.get("resolved_by")) if row.get("resolved_by") else None
    row["ignored_by_name"] = names.get(row.get("ignored_by")) if row.get("ignored_by") else None
    row["harvest"] = harvest_for_invitation(sb, row)
    try:
        row["harvest_job"] = llm_queue.poll_info(JOB_HARVEST, invitation_id)
    except Exception:  # noqa: BLE001 - poll info is a convenience
        logger.debug("rfp portal: poll info failed", exc_info=True)
        row["harvest_job"] = None
    row["harvest_available"] = {
        "ok": row.get("status") in HARVEST_AGAIN_STATUSES,
        "reason": None if row.get("status") in HARVEST_AGAIN_STATUSES
        else "The invitation is not at a stage that can be harvested.",
    }
    if row.get("portal") == PORTAL_BUILDINGCONNECTED:
        # The GC block, the confirmer's name and no harvest (the API has no
        # files). `lead` takes the contract's shape ({name, email, phone};
        # the raw first/last names stay in `payload`), and `lead` and
        # `payload` stay on the row for the router to redact by role
        # (contract section 4, D33).
        row["lead"] = bc_facts.lead_view(row.get("lead"))
        gc_names = _gc_names(sb, {row.get("gc_id")})
        row["gc"] = _gc_block(row, gc_names)
        confirmers = _profile_names(sb, {row.get("gc_confirmed_by")})
        row["gc_confirmed_by_name"] = (
            confirmers.get(row.get("gc_confirmed_by")) if row.get("gc_confirmed_by") else None
        )
        row["changes_recent"] = _changes_recent(row, _now())
        row["harvest_available"] = {"ok": False, "reason": "BuildingConnected has no files to harvest."}
    return row


_SOURCES_SELECT = (
    "id, portal, agency, bid_number, bid_number_raw, addendum_no, close_at, "
    "resolution, resolved_at, match_project_id, created_project_id, status, "
    "external_url, trade_name, invited_at, gc_external_name, gc_id, change_log"
)
_SOURCES_CHANGES = 10


def _portal_source(r: dict, gc_names: dict[str, str | None] | None = None) -> dict:
    out = {
        "portal": r.get("portal"),
        "invitation_id": r.get("id"),
        "agency": r.get("agency"),
        "bid_number": r.get("bid_number"),
        "bid_number_raw": r.get("bid_number_raw"),
        "addendum_no": r.get("addendum_no"),
        "close_at": r.get("close_at"),
        "resolution": r.get("resolution"),
        "resolved_at": r.get("resolved_at"),
    }
    if r.get("portal") == PORTAL_BUILDINGCONNECTED:
        # BUILD_CONTRACT D32: what the project page needs for "Also invited
        # through BuildingConnected" and the change list (the last ten);
        # `gc_name` is the directory GC the row's gc_id points at (the
        # page shows it before the platform's spelling).
        log = [c for c in (r.get("change_log") or []) if isinstance(c, dict)]
        gc_id = r.get("gc_id")
        out.update(
            status=r.get("status"),
            external_url=r.get("external_url"),
            trade_name=r.get("trade_name"),
            invited_at=r.get("invited_at"),
            gc_external_name=r.get("gc_external_name"),
            gc_id=gc_id,
            gc_name=(gc_names or {}).get(str(gc_id)) if gc_id else None,
            change_log=log[-_SOURCES_CHANGES:],
        )
    return out


def portal_sources_for_projects(sb, project_ids) -> dict[str, list[dict]]:
    """{project_id: [{portal, invitation_id, agency, bid_number,
    bid_number_raw, addendum_no, close_at, resolution, resolved_at}]} for
    the rows at `exists` whose match_project_id is one of the ids, plus the
    BuildingConnected rows at `created` whose created_project_id is (with
    the D32 fields on every BuildingConnected row, `gc_name` resolved from
    general_contractors in one read over the rows' gc_ids)."""
    ids = sorted({str(p) for p in (project_ids or []) if p})
    if not ids:
        return {}
    found: list[tuple[str, dict]] = []
    for chunk in _chunks(ids):
        rows = (
            sb.table(INVITATIONS_TABLE)
            .select(_SOURCES_SELECT)
            .eq("status", STATUS_EXISTS)
            .in_("match_project_id", chunk)
            .order("resolved_at", desc=True)
            .execute()
        ).data or []
        found.extend((str(r.get("match_project_id")), r) for r in rows)
        created = (
            sb.table(INVITATIONS_TABLE)
            .select(_SOURCES_SELECT)
            .eq("portal", PORTAL_BUILDINGCONNECTED)
            .eq("status", STATUS_CREATED)
            .in_("created_project_id", chunk)
            .order("id", desc=True)
            .execute()
        ).data or []
        found.extend((str(r.get("created_project_id")), r) for r in created)
    gc_names = _gc_names(
        sb, {r.get("gc_id") for _pid, r in found if r.get("portal") == PORTAL_BUILDINGCONNECTED}
    )
    out: dict[str, list[dict]] = {}
    for pid, r in found:
        out.setdefault(pid, []).append(_portal_source(r, gc_names))
    return out


# ── Human actions (doc 2.5) ──────────────────────────────────────────────────


def _get(sb, invitation_id: str) -> dict:
    row = _load_invitation(sb, invitation_id, "*")
    if row is None:
        raise RfpPortalError(CODE_NOT_ACTIONABLE, _MSG_INVITATION_NOT_FOUND)
    return row


def _require_status(row: dict, *statuses: str) -> str:
    if row.get("status") not in statuses:
        raise RfpPortalError(CODE_NOT_ACTIONABLE, _MSG_NOT_ACTIONABLE)
    return row["status"]


def _validate_reason(reason, *, required: bool) -> str | None:
    """A reason as stored, or the 400 `rfp_portal_reason_invalid`: a bad
    reason is the caller's input, never the row's state (which is what the
    409 not-actionable code means and what makes the tab reload)."""
    text = " ".join(str(reason).split()) if reason is not None else ""
    if not text:
        if required:
            raise RfpPortalError(CODE_REASON_INVALID, "A reason is required.")
        return None
    if len(text) < _REASON_MIN_CHARS:
        raise RfpPortalError(
            CODE_REASON_INVALID, f"The reason is too short (at least {_REASON_MIN_CHARS} characters)."
        )
    return text[:_REASON_MAX_CHARS]


def _valid_uuid(value) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _raced() -> RfpPortalError:
    return RfpPortalError(CODE_NOT_ACTIONABLE, _MSG_RACED)


def resolve_exists(sb, invitation_id: str, project_id, actor_id: str) -> dict:
    """Same as project X: review_match -> exists. The project must be one of
    the stored candidates or any project inside the candidate window, and
    never one excluded from this invitation. A portal may refuse first
    (BuildingConnected, `guard_resolve_exists`: the GC is still a guess)."""
    row = _get(sb, invitation_id)
    _require_status(row, STATUS_REVIEW_MATCH)
    guard = _portal_hook(row.get("portal"), "guard_resolve_exists")
    if guard is not None:
        guard(row)
    pid = _valid_uuid(project_id)
    if not pid:
        raise RfpPortalError(CODE_PROJECT_REQUIRED, "Pick the project this invitation belongs to.")
    if pid in {str(x) for x in (row.get("excluded_project_ids") or [])}:
        raise RfpPortalError(
            CODE_PROJECT_REQUIRED, "That project was excluded from this invitation before."
        )
    candidate_ids = {
        str(c.get("project_id")) for c in (row.get("match_candidates") or []) if isinstance(c, dict)
    }
    if pid not in candidate_ids:
        settings = get_settings()
        lo = _now() - timedelta(days=settings.rfp_match_candidate_window_days)
        hits = (
            rfp_match.candidate_query(sb, rfp_match.pg_ts(lo), select="id")
            .eq("id", pid)
            .limit(1)
            .execute()
        ).data or []
        if not hits:
            raise RfpPortalError(
                CODE_PROJECT_REQUIRED,
                "That project is not one of the candidates and its bid date is outside the "
                "matching window.",
            )
    elif not (sb.table("projects").select("id").eq("id", pid).limit(1).execute().data or []):
        # A stored candidate outlives its project (a deleted project,
        # docs/PROJECT_DELETE.md): refuse with a sentence, never a raw FK error.
        raise RfpPortalError(CODE_PROJECT_REQUIRED, "That project no longer exists.")
    now_iso = _iso(_now())
    ok = _cas(sb, invitation_id, STATUS_REVIEW_MATCH, {
        "status": STATUS_EXISTS,
        "match_project_id": pid,
        "resolution": "human",
        "resolved_by": actor_id,
        "resolved_at": now_iso,
        "decided_at_step": STATUS_REVIEW_MATCH,
        "last_error": None,
        "next_attempt_at": None,
    })
    if not ok:
        raise _raced()
    audit(actor_id, "rfp_portal.resolve", AUDIT_ENTITY, invitation_id, {
        "project_id": pid,
        "was_best": pid == str(row.get("match_project_id") or ""),
        "flag_reason": row.get("flag_reason"),
    })
    on_exists = _portal_hook(row.get("portal"), "on_exists")
    if on_exists is not None:
        # The human path runs the same side effects as the system merge
        # (BUILD_CONTRACT D13): the GC link, the lead as bid contact, the
        # files flag. A refusal leaves the marker; it is logged.
        gc_from_row = _portal_hook(row.get("portal"), "gc_from_row")
        gc = gc_from_row(row) if gc_from_row is not None else None
        try:
            on_exists(
                sb, {**row, "status": STATUS_EXISTS, "match_project_id": pid}, pid, gc,
                get_settings(), merged=True,
            )
        except rfp_email_ingest.RfpMatchError as exc:
            logger.warning("rfp portal: %s resolved to %s but the merge was refused: %s", invitation_id, pid, exc)
    return detail(sb, invitation_id)


def resolve_new(sb, invitation_id: str, actor_id: str) -> dict:
    """Not a match: review_match -> harvest (NGEM) or create (a portal with
    nothing to harvest) with the best candidate and every candidate the
    person saw excluded, so the re-run cannot pick them; attempts reset.
    The next step runs at once."""
    row = _get(sb, invitation_id)
    _require_status(row, STATUS_REVIEW_MATCH)
    no_match_hook = _portal_hook(row.get("portal"), "no_match_status")
    target = no_match_hook() if no_match_hook is not None else STATUS_HARVEST
    seen = [str(x) for x in (row.get("excluded_project_ids") or [])]
    if row.get("match_project_id"):
        seen.append(str(row["match_project_id"]))
    for c in row.get("match_candidates") or []:
        if isinstance(c, dict) and c.get("project_id"):
            seen.append(str(c["project_id"]))
    excluded = list(dict.fromkeys(seen))
    now_iso = _iso(_now())
    ok = _cas(sb, invitation_id, STATUS_REVIEW_MATCH, {
        "status": target,
        "excluded_project_ids": excluded,
        "match_project_id": None,
        "resolution": "human",
        "resolved_by": actor_id,
        "resolved_at": now_iso,
        "decided_at_step": STATUS_REVIEW_MATCH,
        **_RESET,
    })
    if not ok:
        raise _raced()
    audit(actor_id, "rfp_portal.new", AUDIT_ENTITY, invitation_id, {
        "excluded_project_ids": excluded,
        "was_project_id": row.get("match_project_id"),
        "to_status": target,
    })
    moved = {**row, "status": target, "attempts": 0, "last_error": None, "next_attempt_at": None,
             "resolution": "human", "match_project_id": None}
    try:
        if target == STATUS_CREATE:
            _step_create(sb, moved, get_settings())
        else:
            _step_harvest(sb, moved, get_settings())
    except Exception:  # noqa: BLE001 - the sweep picks the row up on the next tick
        logger.exception("rfp portal: inline %s step failed for %s", target, invitation_id)
    return detail(sb, invitation_id)


def reopen(sb, invitation_id: str, reason, actor_id: str) -> dict:
    """exists -> match with the project excluded; the marker disappears. A
    GC link the merge added (BuildingConnected, `project_gc_id`) is removed
    by its id first, and the reopen is refused when a proposal went to that
    GC (BUILD_CONTRACT D13)."""
    reason = _validate_reason(reason, required=False)
    row = _get(sb, invitation_id)
    _require_status(row, STATUS_EXISTS)
    removed_link = None
    remove_link = _portal_hook(row.get("portal"), "remove_link")
    if remove_link is not None and row.get("project_gc_id"):
        removed_link = remove_link(sb, row)
    excluded = [str(x) for x in (row.get("excluded_project_ids") or [])]
    if row.get("match_project_id") and str(row["match_project_id"]) not in excluded:
        excluded.append(str(row["match_project_id"]))
    ok = _cas(sb, invitation_id, STATUS_EXISTS, {
        "status": STATUS_MATCH,
        "excluded_project_ids": excluded,
        "match_project_id": None,
        "project_gc_id": None,
        "reopen_reason": reason,
        "resolution": None,
        "resolved_by": None,
        "resolved_at": None,
        "flag_reason": None,
        "decided_at_step": None,
        **_RESET,
    })
    if not ok:
        raise _raced()
    audit(actor_id, "rfp_portal.reopen", AUDIT_ENTITY, invitation_id, {
        "reason": reason,
        "was_project_id": row.get("match_project_id"),
        "removed_project_gc_id": removed_link,
    })
    return detail(sb, invitation_id)


def _cancel_queued_harvest(invitation_id: str) -> str | None:
    """A harvest job still QUEUED for the invitation is canceled through the
    queue's own cancel (the AI monitor's), so an ignored bid is never opened
    on the portal; a running one is left to finish (its writes are
    field-scoped and its done CAS loses). Returns the canceled job id."""
    job = active_harvest_job(invitation_id)
    if not job or job.get("status") != "queued":
        return None
    canceled = llm_queue.cancel(job["id"])
    return canceled.get("id") if canceled else None


def ignore(sb, invitation_id: str, reason, actor_id: str) -> dict:
    """match / review_match / harvest / create -> ignored. A still-queued
    harvest job is canceled; a running one is left to finish (its writes
    are field-scoped and its done CAS loses; the pipeline job also refuses
    an ignored row before opening anything). A creation in flight on a
    `create` row loses its final CAS and compensates (RFP_CREATE.md 4.2)."""
    reason = _validate_reason(reason, required=False)
    row = _get(sb, invitation_id)
    expected = _require_status(row, *IGNORABLE_STATUSES)
    ok = _cas(sb, invitation_id, expected, {
        "status": STATUS_IGNORED,
        "ignored_by": actor_id,
        "ignored_at": _iso(_now()),
        "ignore_reason": reason,
        "decided_at_step": expected,
        "next_attempt_at": None,
    })
    if not ok:
        raise _raced()
    canceled_job = None
    try:
        canceled_job = _cancel_queued_harvest(invitation_id)
    except Exception:  # noqa: BLE001 - the job would refuse the ignored row anyway
        logger.exception("rfp portal: could not cancel the queued harvest for %s", invitation_id)
    audit(actor_id, "rfp_portal.ignore", AUDIT_ENTITY, invitation_id, {
        "from_status": expected, "reason": reason, "canceled_job_id": canceled_job,
    })
    return detail(sb, invitation_id)


def unignore(sb, invitation_id: str, actor_id: str) -> dict:
    """ignored -> match; attempts reset, excluded_project_ids kept."""
    row = _get(sb, invitation_id)
    _require_status(row, STATUS_IGNORED)
    ok = _cas(sb, invitation_id, STATUS_IGNORED, {
        "status": STATUS_MATCH,
        "ignored_by": None,
        "ignored_at": None,
        "ignore_reason": None,
        # A system ignore (declined or archived in BuildingConnected, D24)
        # has no ignored_by; the source clears with the rest.
        "ignore_source": None,
        "decided_at_step": None,
        **_RESET,
    })
    if not ok:
        raise _raced()
    audit(actor_id, "rfp_portal.unignore", AUDIT_ENTITY, invitation_id, {
        "was_reason": row.get("ignore_reason"),
        "was_source": row.get("ignore_source"),
    })
    return detail(sb, invitation_id)


def restore(sb, invitation_id: str, actor_id: str) -> dict:
    """historical / expired / withdrawn -> match (BUILD_CONTRACT D23): the
    row enters (or re-enters) the pipeline with the attempt ladder reset
    and the system's parking reason cleared; excluded_project_ids kept. The
    FE labels it "Process" on a historical row and "Restore" otherwise."""
    row = _get(sb, invitation_id)
    expected = _require_status(row, *STATUS_PARKED)
    ok = _cas(sb, invitation_id, expected, {
        "status": STATUS_MATCH,
        "flag_reason": None,
        "decided_at_step": None,
        "missing_since": None,
        **_RESET,
    })
    if not ok:
        raise _raced()
    audit(actor_id, "rfp_portal.restore", AUDIT_ENTITY, invitation_id, {
        "from_status": expected,
        "was_error": row.get("last_error"),
    })
    return detail(sb, invitation_id)


def create_project(sb, invitation_id: str, actor_id: str):
    """The modal's "Create project" (docs/RFP_CREATE.md 8): a project from a
    `done` (or `create`) invitation through rfp_create, by hand. Returns
    the rfp_create.Created; the router maps CreateRefused and
    CreateInProgress. Audited `rfp_portal.create`. A portal's sibling guard
    (BuildingConnected, D12b) may link the row to a package sibling's
    project instead; that answers `rfp_portal_not_actionable` and the tab
    reloads with the row at `exists`."""
    row = _get(sb, invitation_id)
    guard = _portal_hook(row.get("portal"), "sibling_guard")
    if guard is not None and row.get("status") in (STATUS_CREATE, STATUS_DONE) and guard(sb, row):
        audit(actor_id, "rfp_portal.create", AUDIT_ENTITY, invitation_id, {
            "project_id": None, "linked_sibling": True, "from_status": row.get("status"),
        })
        raise RfpPortalError(
            CODE_NOT_ACTIONABLE,
            "Another package of the same project already has a project; this invitation was "
            "linked to it instead of creating a second one.",
        )
    created = rfp_create.create_from_portal(sb, row, actor_id=actor_id, automatic=False)
    audit(actor_id, "rfp_portal.create", AUDIT_ENTITY, invitation_id, {
        "project_id": created.project_id, "number": created.number, "linked": created.linked,
        "from_status": row.get("status"),
    })
    return created


def _locked_error(reason: str | None, until: datetime | None) -> RfpPortalError:
    message = reason or "The portal is not available."
    if until:
        message = f"{message} Locked until {_pacific_stamp(until)}."
    return RfpPortalError(CODE_LOCKED, message, locked_until=until)


def harvest_again(sb, invitation_id: str, actor_id: str, *, force: bool = True) -> dict:
    """Queue a harvest by hand: from `done` (a forced refresh) or `harvest`
    (the pipeline's own job, sooner). Returns the job; `rfp_portal_locked`
    while logins are locked, `rfp_portal_harvest_active` while a job is
    queued or running."""
    row = _get(sb, invitation_id)
    if row.get("status") not in HARVEST_AGAIN_STATUSES:
        raise RfpPortalError(
            CODE_NOT_AVAILABLE, "The invitation is not at a stage that can be harvested."
        )
    if not portal_has_harvest(row.get("portal")):
        # A portal with nothing to harvest (BuildingConnected: the API
        # carries no documents, its client has no fetch_event): the job
        # would only crash up the retry ladder, so it is never queued.
        raise RfpPortalError(CODE_NOT_ACTIONABLE, MSG_NO_HARVEST)
    settings = get_settings()
    usable, reason, until = _portal(row.get("portal")).availability(settings)
    if not usable:
        raise _locked_error(reason, until)
    try:
        job = enqueue_harvest(invitation_id, created_by=actor_id, force=force, settings=settings)
    except llm_queue.JobAlreadyActive as exc:
        raise RfpPortalError(CODE_HARVEST_ACTIVE, _MSG_HARVEST_ACTIVE) from exc
    audit(actor_id, "rfp_portal.harvest_again", AUDIT_ENTITY, invitation_id, {
        "force": bool(force), "from_status": row.get("status"),
    })
    return {"id": job.get("id"), "status": job.get("status")}


def run_now(
    sb, actor_id: str, portal: str = PORTAL_NGEM, kind: str = RUN_KIND_INCREMENTAL
) -> dict:
    """Run now: a manual run and its scan job. 503 `rfp_portal_not_available`
    while the portal's slice is inactive (the queue that runs the scan is
    off); `rfp_portal_run_active` while a run is queued or running;
    `rfp_portal_locked` while logins are locked or the account is not
    configured (a portal may answer its own code instead, through its
    `unavailable_error` hook: BuildingConnected's `rfp_bc_not_connected`).
    `kind` is incremental or full (the BuildingConnected full sync)."""
    settings = get_settings()
    if kind not in (RUN_KIND_INCREMENTAL, RUN_KIND_FULL):
        raise RfpPortalError(CODE_REASON_INVALID, "The run kind must be incremental or full.")
    if not _portal_active(settings, portal):
        message = _MSG_QUEUE_OFF
        if portal == PORTAL_BUILDINGCONNECTED:
            message = (
                "The BuildingConnected scan cannot run: the job queue is off or the slice "
                "is inactive."
            )
        raise RfpPortalError(CODE_NOT_AVAILABLE, message, http_status=503)
    adapter = _portal(portal)
    usable, reason, until = adapter.availability(settings)
    if not usable:
        unavailable = _hook(adapter, "unavailable_error")
        raise unavailable(reason, until) if unavailable is not None else _locked_error(reason, until)
    try:
        rows = (
            sb.table(RUNS_TABLE)
            .insert(
                {
                    "portal": portal,
                    "trigger": TRIGGER_MANUAL,
                    "scheduled_for": None,
                    "requested_by": actor_id,
                    "status": RUN_QUEUED,
                    "kind": kind,
                }
            )
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001 - only the active-run race is expected
        if _is_unique_violation(exc):
            raise RfpPortalError(CODE_RUN_ACTIVE, _MSG_RUN_ACTIVE) from exc
        raise
    # An insert that answered no row (a minimal-return PostgREST) still
    # holds the active index: read the run back through it.
    run = rows[0] if rows else _active_run(sb, portal)
    if not run:
        raise RfpPortalError(CODE_NOT_AVAILABLE, _MSG_RUN_NOT_RECORDED, http_status=503)
    job = _dispatch_run(sb, run, settings)
    if job is None:
        # The run is parked (queued, one poll out) and the scheduler will
        # dispatch it; the caller learns it is queued without a job yet.
        logger.warning("rfp portal: Run now for %s queued run %s without a job", portal, run.get("id"))
    audit(actor_id, "rfp_portal.run_now", AUDIT_RUN_ENTITY, run["id"], {
        "portal": portal, "kind": kind, "job_id": (job or {}).get("id"),
    })
    return _load_run(sb, run["id"]) or run

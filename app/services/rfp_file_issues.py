"""Missing files on an RFP-created project (docs/RFP_MISSING_FILES.md).

When the harvest cannot bring every bid document into a project made from
an RFP invitation, every file or link that did not come in becomes one
`rfp_file_issues` row (migration 0151) with why, the share link where one
exists (https only) and, for an attachment with no link, the email it came
from. The project page renders them as a callout; a person resolves them
one by one, or dismisses them all with an Undo, and a file of the same name
landing in the project by any path resolves its item on its own.

This module owns:

- the pure builders (section 3): `items_from_harvest`, `total_failure`,
  `normalise_name`, `names_match`, `safe_url`, `creation_note`;
- the sync (section 4): `sync_project`, additive and idempotent on
  `item_key`, never deleting and never reopening;
- the resolution paths: `after_file_insert` (the hook every
  `project_files` insert path calls beside `files_needed.clear_if_satisfied`),
  `resolve_by_names`, `resolve`, `reopen`, `dismiss_all`, `undismiss`;
- the wire shapes: `issue_for_project` (section 7) and `flags_for_records`
  (the Created from RFP Ingestion list's `flags.files_issue`).

Everything here uses the sync Supabase client: callers are plain `def`
routes, the queue worker, or `run_in_threadpool`; never an `async def`
directly (docs/MEMORY: the event-loop rule). Every hook and the sync are
best effort: they log and never raise, because the project or the file they
follow is already in.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from app.core.config import Settings, get_settings
from app.services import cloud_folders, files_needed
from app.services.notifications import audit
from app.services.rfp_email_files import EMAIL_METHODS, HARVESTER_EMAIL

logger = logging.getLogger(__name__)

TABLE = "rfp_file_issues"
CREATED_TABLE = "rfp_created_projects"
HARVESTS_TABLE = "rfp_harvests"
EMAILS_TABLE = "rfp_emails"
JOBS_TABLE = "bid_split_jobs"
FILES_TABLE = "bid_split_files"

KIND_FILE = "file"
KIND_LINK = "link"
KIND_HARVEST = "harvest"

GROUP_NOT_DOWNLOADED = "not_downloaded"
GROUP_REFUSED = "refused"
_GROUP_ORDER = {GROUP_NOT_DOWNLOADED: 0, GROUP_REFUSED: 1}

ORIGIN_ATTACHMENT = "attachment"
ORIGIN_ZIP = "zip"
ORIGIN_LINK = "link"
ORIGIN_PLATFORM = "platform"
ORIGIN_HARVEST = "harvest"
ORIGIN_PROMOTION = "promotion"
_EMAIL_ORIGINS = (ORIGIN_ATTACHMENT, ORIGIN_ZIP, ORIGIN_LINK)

HOW_NAME_MATCH = "name_match"
HOW_MANUAL = "manual"
HOW_AUTO_FIRST_FILE = "auto_first_file"
HOW_DISMISS_ALL = "dismiss_all"

STATE_OPEN = "open"
STATE_DISMISSED = "dismissed"
STATE_RESOLVED = "resolved"

STATUS_HARVEST_FAILED = "harvest_failed"
SKIPPED_PREFIX = "skipped:"

# harvest.files[] statuses that are items (section 3).
FILE_NOT_DOWNLOADED = ("download_failed", "too_large", "skipped_cap")
FILE_REFUSED = ("rejected",)
# harvest.data.links[] statuses that are items; `listed` never is.
LINK_FAILED = ("needs_sign_in", "unsupported", "unreachable", "html_page", "too_many_files", "skipped_cap")
# Entries the promotion could act on (rfp_create_files.ENTRY_WITH_FILE).
ENTRY_WITH_FILE = ("accepted", "reused")
# A files_skipped reason that only repeats the harvest entry's own status
# (rfp_create_files.promotion_for answers Skip(status) for every entry that
# never reached the sandbox): never a promotion item, the harvest builder
# already decided about that entry. `too_large` is absent on purpose: it is
# also the promotion's own REASON_TOO_LARGE.
_HARVEST_ENTRY_TOKENS = frozenset({
    "download_failed", "rejected", "skipped_cap", "skipped", "expanded", "pending", "none", "null",
})

AUDIT_RESOLVE = "rfp_files.resolve"
AUDIT_REOPEN = "rfp_files.reopen"
AUDIT_DISMISS = "rfp_files.dismiss"
AUDIT_UNDISMISS = "rfp_files.undismiss"
AUDIT_DROP = "rfp_files.drop"
AUDIT_ENTITY = "project"

MSG_NOTHING_OPEN = "Nothing is open to dismiss."
MSG_NOT_DISMISSED = "The missing files are not dismissed."

_NAME_MAX_CHARS = 500
_REASON_MAX_CHARS = 500
_TOKEN_MAX_CHARS = 200
_PROVIDER_MAX_CHARS = 100
_URL_MAX_CHARS = 2048
_KEY_MAX_CHARS = 400
_IN_CHUNK = 200
_DROP_FAILED_MAX = 100
_DROP_JOBS_SCAN = 20
_PAGE_ROWS = 1000   # PostgREST's max-rows: every unbounded item read pages by this

# The platform hosts the harvest clients rebuild bid-sheet and portal URLs
# on (rfp_harvests.external_url): procore_client.APP_HOST / LOGIN_HOST,
# pipelinesuite_client (`<gc>.pipelinesuite.com`), smartbid_client (the SPA
# on smartbid.co, files on smartbidnet.com, the API on smartinsight.co) and
# ngem_client.HOST (supplier.ionwave.net). Kept here as plain constants so
# this module never imports the platform clients.
_PLATFORM_HOSTS = frozenset({"app.procore.com", "login.procore.com", "supplier.ionwave.net"})
_PLATFORM_SUFFIXES = (
    ".procore.com", ".pipelinesuite.com", ".smartbid.co", ".smartbidnet.com", ".smartinsight.co",
    ".ionwave.net",
)

_EMAIL_SELECT = "id, from_name, from_address, subject, received_at, primary_mailbox"


class IssueConflict(Exception):
    """A dismissal toggle that cannot apply; `str(exc)` is the 409 sentence."""


# ── Pure helpers ─────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def normalise_name(name: Any) -> str:
    """NFC, casefold, the last segment after `/` or `\\`, whitespace
    collapsed and stripped. Empty for anything that is not text."""
    if not isinstance(name, str):
        return ""
    text = unicodedata.normalize("NFC", name).casefold()
    text = re.split(r"[\\/]", text)[-1]
    return " ".join(text.split()).strip()


# A recognised extension: a final `.` and 1 to 5 letters or digits, at least
# one of them a letter. An all-digit suffix (`E-1.1`, `A-201.2`) is a sheet or
# revision number, part of the name, never an extension (review finding 14).
_EXT_RE = re.compile(r"\.(?=[0-9]{0,4}[a-z])[a-z0-9]{1,5}$")


def _stem(base: str) -> str | None:
    """The basename without its recognised extension, or None when it has
    none (such a name matches only exactly)."""
    match = _EXT_RE.search(base)
    if not match or match.start() == 0:
        return None
    return base[:match.start()]


def names_match(a: Any, b: Any) -> bool:
    """Equal normalised basenames, or, failing that, BOTH names carry a
    recognised extension and their stems are equal: `Specs.docx` re-saved as
    `Specs.pdf` still matches, `E-1.pdf` never matches `E-10.pdf`, and `E-1.1`
    (no extension) never matches `E-1.pdf` or `E-1.2`."""
    left, right = normalise_name(a), normalise_name(b)
    if not left or not right:
        return False
    if left == right:
        return True
    left_stem, right_stem = _stem(left), _stem(right)
    return left_stem is not None and left_stem == right_stem


def _platform_host(host: str) -> bool:
    return host in _PLATFORM_HOSTS or host.endswith(_PLATFORM_SUFFIXES)


def safe_url(url: Any) -> str | None:
    """`url` when it is safe to hand a person as a plain link, else None:
    https, a host, no credentials, no whitespace or control characters,
    at most 2048 characters, and a host the harvester recognises (a
    `cloud_folders` share host, recognised or merely listed, or one of the
    platform hosts the harvest clients build bid-sheet URLs on)."""
    if not isinstance(url, str):
        return None
    text = url.strip()
    if not text or len(text) > _URL_MAX_CHARS or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return None
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return None
    try:
        recognised = cloud_folders.link_from_url(text) is not None
    except Exception:  # noqa: BLE001 - a parser hiccup means "not recognised"
        recognised = False
    if recognised or cloud_folders._host_allowed(host) or _platform_host(host):
        return text
    return None


def _provider_name(provider: Any) -> str | None:
    key = str(provider or "").strip().lower()
    if not key:
        return None
    return cloud_folders._PROVIDER_NAMES.get(key) or key


def _email_ref(email: dict | None) -> dict | None:
    if not isinstance(email, dict):
        return None
    ref = {
        "from": _text(email.get("from_name"), 200) or _text(email.get("from_address"), 320),
        "from_address": _text(email.get("from_address"), 320),
        "subject": _text(email.get("subject"), 300),
        "received_at": email.get("received_at"),
        "mailbox": _text(email.get("primary_mailbox"), 320),
    }
    return ref if any(ref.values()) else None


def _item_key(kind: str, origin: str | None, status: str, name: str | None) -> str:
    """`kind|origin|status|name`; a long name is replaced by its digest so
    the unique index never sees an oversized key."""
    name = name or ""
    key = f"{kind}|{origin or ''}|{status}|{name}"
    if len(key) > _KEY_MAX_CHARS:
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
        key = f"{kind}|{origin or ''}|{status}|sha256:{digest}"
    return key


def _link_item_key(link: dict) -> str:
    ident = str(link.get("key") or link.get("url") or link.get("label") or "").strip()
    key = f"{KIND_LINK}|{ident}"
    if len(key) > _KEY_MAX_CHARS:
        key = f"{KIND_LINK}|sha256:{hashlib.sha256(ident.encode('utf-8')).hexdigest()}"
    return key


@dataclass(frozen=True)
class ItemSpec:
    """One item the builders found (section 3); `item_key` is its identity."""

    kind: str
    group: str
    origin: str | None
    provider: str | None
    status: str
    name: str | None
    url: str | None
    reason: str | None
    email_ref: dict | None
    item_key: str

    def row(self, project_id: str, harvest_id: str | None) -> dict:
        return {
            "project_id": project_id,
            "harvest_id": harvest_id,
            "item_key": self.item_key,
            "kind": self.kind,
            "group": self.group,
            "origin": self.origin,
            "provider": self.provider,
            "status": self.status,
            "name": self.name,
            "url": self.url,
            "reason": self.reason,
            "email_ref": self.email_ref,
        }


def _spec(kind: str, group: str, origin: str | None, provider: Any, status: str, name: Any, url: Any,
          reason: Any, email_ref: dict | None, *, key: str | None = None) -> ItemSpec:
    clean_name = _text(name, _NAME_MAX_CHARS)
    clean_status = _text(status, _TOKEN_MAX_CHARS) or "unknown"
    return ItemSpec(
        kind=kind, group=group, origin=origin, provider=_text(provider, _PROVIDER_MAX_CHARS),
        status=clean_status, name=clean_name, url=safe_url(url), reason=_text(reason, _REASON_MAX_CHARS),
        email_ref=email_ref, item_key=key or _item_key(kind, origin, clean_status, clean_name),
    )


def _entry_path(entry: dict) -> str | None:
    for key in ("file_path", "file_name"):
        raw = entry.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def items_from_harvest(harvest: dict | None, email: dict | None, skipped: list[dict] | None) -> list[ItemSpec]:
    """Every item a harvest and the promotion's `files_skipped` describe
    (section 3), deduped on `item_key` (the first one wins), pure and
    deterministic. `email` is the email source's `rfp_emails` row (None for
    a portal source)."""
    out: dict[str, ItemSpec] = {}

    def add(spec: ItemSpec) -> None:
        out.setdefault(spec.item_key, spec)

    h = harvest if isinstance(harvest, dict) else None
    method = (h or {}).get("method")
    # The email harvester's methods (organic / general / nonorganic) mean
    # nothing to a person; its platform is the email itself.
    method_provider = HARVESTER_EMAIL if method in EMAIL_METHODS else method
    data = (h or {}).get("data") if isinstance((h or {}).get("data"), dict) else {}
    links = [link for link in (data.get("links") or []) if isinstance(link, dict)]
    link_url = {str(link["key"]): link.get("url") for link in links if link.get("key")}
    entries = [e for e in ((h or {}).get("files") or []) if isinstance(e, dict)]
    ref = _email_ref(email)

    if h is not None and h.get("status") == "failed":
        add(_spec(
            KIND_HARVEST, GROUP_NOT_DOWNLOADED, ORIGIN_HARVEST, method_provider, STATUS_HARVEST_FAILED, None,
            h.get("external_url"), h.get("last_error"), ref if method in EMAIL_METHODS else None,
        ))

    for entry in entries:
        status = entry.get("status")
        if status in FILE_NOT_DOWNLOADED:
            group = GROUP_NOT_DOWNLOADED
        elif status in FILE_REFUSED:
            group = GROUP_REFUSED
        else:
            continue
        origin = entry.get("origin") if entry.get("origin") in _EMAIL_ORIGINS else ORIGIN_PLATFORM
        provider = entry.get("provider") or (method_provider if origin == ORIGIN_PLATFORM else None)
        link_key = entry.get("link_key")
        if link_key:
            url = link_url.get(str(link_key))
        elif origin == ORIGIN_PLATFORM:
            # A platform document (Procore, PipelineSuite, SmartBid, NGEM):
            # the bid sheet or portal page it is listed on (review finding 3).
            url = (h or {}).get("external_url")
        else:
            url = None
        attached = origin in (ORIGIN_ATTACHMENT, ORIGIN_ZIP) and not link_key
        add(_spec(KIND_FILE, group, origin, provider, status, _entry_path(entry), url, entry.get("error"),
                  ref if attached else None))

    for link in links:
        status = link.get("status")
        if status not in LINK_FAILED:
            continue
        name = _text(link.get("label"), _NAME_MAX_CHARS) or _provider_name(link.get("provider"))
        add(_spec(KIND_LINK, GROUP_NOT_DOWNLOADED, ORIGIN_LINK, link.get("provider"), status, name,
                  link.get("url"), link.get("error"), None, key=_link_item_key(link)))

    by_name: dict[str, list[dict]] = {}
    for entry in entries:
        base = normalise_name(_entry_path(entry))
        if base:
            by_name.setdefault(base, []).append(entry)
    for skip in skipped or []:
        if not isinstance(skip, dict):
            continue
        reason = str(skip.get("reason") or "").strip()
        name = _entry_path(skip)
        if not reason or not name:
            continue
        matches = by_name.get(normalise_name(name), [])
        if matches:
            # Only a document the harvest delivered can be refused by the
            # promotion; any other entry is the harvest builder's to judge.
            if not any(m.get("status") in ENTRY_WITH_FILE for m in matches):
                continue
        elif reason.lower() in _HARVEST_ENTRY_TOKENS:
            continue
        link_key = next((m.get("link_key") for m in matches if m.get("link_key")), None)
        provider = next((m.get("provider") for m in matches if m.get("provider")), None) or method_provider
        add(_spec(KIND_FILE, GROUP_REFUSED, ORIGIN_PROMOTION, provider, SKIPPED_PREFIX + reason, name,
                  link_url.get(str(link_key)) if link_key else None, None, None))
    return list(out.values())


def total_failure(harvest: dict | None) -> bool:
    """No entry the harvest brought in (`accepted` or `reused`), or no
    harvest at all: the first-file rule's trigger."""
    if not isinstance(harvest, dict):
        return True
    return not any(
        isinstance(e, dict) and e.get("status") in ENTRY_WITH_FILE for e in (harvest.get("files") or [])
    )


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


NOTE_TOTAL_NOT_DOWNLOADED = (
    "None of the invitation's files could be downloaded; open the project to get them by hand."
)
NOTE_TOTAL_REFUSED = "All of the invitation's files were downloaded and refused; open the project to see why."
NOTE_TOTAL_BOTH = "None of the invitation's files came in; open the project to see what happened."


def creation_note(specs: list[ItemSpec], *, total: bool = False) -> str | None:
    """The sentence appended to both creation bells (section 9), or None
    when nothing is missing. `total` is `total_failure(harvest)`; a total
    failure reads by which groups its items fall in."""
    if not specs:
        return None
    a = sum(1 for s in specs if s.group == GROUP_NOT_DOWNLOADED)
    b = sum(1 for s in specs if s.group == GROUP_REFUSED)
    if total:
        if a and b:
            return NOTE_TOTAL_BOTH
        return NOTE_TOTAL_REFUSED if b else NOTE_TOTAL_NOT_DOWNLOADED
    parts = []
    if a:
        parts.append(f"{a} could not be downloaded")
    if b:
        parts.append(f"{b} {_plural(b, 'was', 'were')} refused")
    n = a + b
    return (
        f"{n} of the invitation's files did not come in ({', '.join(parts)}); "
        "open the project to see which."
    )


def note_for_harvest(harvest: dict | None, email: dict | None) -> str | None:
    """`creation_note` over what the creation step holds (no promotion has
    run yet, so no skipped list). Best effort: None on any trouble."""
    try:
        return creation_note(items_from_harvest(harvest, email, []), total=total_failure(harvest))
    except Exception:  # noqa: BLE001 - a courtesy line on a bell
        logger.exception("rfp files: creation note failed")
        return None


# ── Reads ────────────────────────────────────────────────────────────────────


def _rows(result) -> list[dict]:
    data = getattr(result, "data", None)
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _chunks(items: list, size: int = _IN_CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _record(sb, project_id: str) -> dict | None:
    rows = _rows(sb.table(CREATED_TABLE).select("*").eq("project_id", project_id).limit(1).execute())
    return rows[0] if rows else None


def _harvest(sb, harvest_id: str | None) -> dict | None:
    if not harvest_id:
        return None
    rows = _rows(sb.table(HARVESTS_TABLE).select("*").eq("id", harvest_id).limit(1).execute())
    return rows[0] if rows else None


def _email(sb, email_id: str | None) -> dict | None:
    if not email_id:
        return None
    rows = _rows(sb.table(EMAILS_TABLE).select(_EMAIL_SELECT).eq("id", email_id).limit(1).execute())
    return rows[0] if rows else None


def _project_items(sb, project_id: str, *, open_only: bool) -> list[dict]:
    """A project's items, paged by PostgREST's row cap so a long list is
    never silently cut."""
    out: list[dict] = []
    start = 0
    while True:
        query = sb.table(TABLE).select("*").eq("project_id", project_id)
        if open_only:
            query = query.is_("resolved_at", "null")
        page = _rows(query.order("id").range(start, start + _PAGE_ROWS - 1).execute())
        out.extend(page)
        if len(page) < _PAGE_ROWS:
            return out
        start += _PAGE_ROWS


def _items(sb, project_id: str) -> list[dict]:
    return _project_items(sb, project_id, open_only=False)


def _open_items(sb, project_id: str) -> list[dict]:
    return _project_items(sb, project_id, open_only=True)


def _names(sb, ids) -> dict[str, str | None]:
    clean = sorted({str(i) for i in ids if i})
    out: dict[str, str | None] = {}
    for chunk in _chunks(clean):
        for row in _rows(sb.table("profiles").select("id, full_name").in_("id", chunk).execute()):
            out[str(row["id"])] = row.get("full_name")
    return out


# ── Sync (section 4) ─────────────────────────────────────────────────────────


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc)
    return "23505" in text or "duplicate key" in text


def _insert_rows(sb, rows: list[dict]) -> int:
    """One insert for the batch; a unique race (a concurrent sync) falls back
    to one row at a time, each duplicate skipped."""
    if not rows:
        return 0
    try:
        return len(_rows(sb.table(TABLE).insert(rows).execute())) or len(rows)
    except Exception as exc:  # noqa: BLE001
        if not _is_unique_violation(exc):
            raise
    inserted = 0
    for row in rows:
        try:
            sb.table(TABLE).insert(row).execute()
            inserted += 1
        except Exception as exc:  # noqa: BLE001
            if not _is_unique_violation(exc):
                raise
    return inserted


def _harvest_email(sb, harvest: dict | None, email: dict | None) -> dict | None:
    """The email an email-harvester harvest came from (only those items ever
    carry `email_ref`), read unless `email` already is that row."""
    if (harvest or {}).get("method") not in EMAIL_METHODS:
        return None
    email_id = (harvest or {}).get("rfp_email_id")
    if not email_id:
        return None
    if isinstance(email, dict) and str(email.get("id") or "") == str(email_id):
        return email
    return _email(sb, str(email_id))


def sync_project(sb, project_id: str, *, harvest: dict | None = None, record: dict | None = None,
                 email: dict | None = None) -> int:
    """Insert the items whose `item_key` is not on the table yet and return
    how many were inserted. Reads what it was not handed (the created
    record, its harvest, the harvest's own email). Never deletes, never
    reopens, and swallows and logs every failure (the project exists either
    way).

    `email_ref` always names the HARVEST's own message
    (`rfp_harvests.rfp_email_id`), never the created record's: on the
    recent-project link path one invitation's harvest is promoted into a
    project whose record names another email (review finding 6). An `email`
    handed in that is not that message is ignored and the right one read.

    `files_issue_total` is stamped by the sync that records the project's
    FIRST items when the harvest brought nothing in, and never flipped later:
    the first-file rule reads what the first look saw."""
    if not project_id:
        return 0
    try:
        if record is None or "files_skipped" not in record:
            record = _record(sb, project_id)
        if record is None:
            return 0   # not a project the RFP creation step made
        if harvest is None:
            harvest = _harvest(sb, record.get("harvest_id"))
        email = _harvest_email(sb, harvest, email)
        specs = items_from_harvest(harvest, email, record.get("files_skipped") or [])
        if not specs:
            return 0
        existing = {str(r.get("item_key")) for r in _rows(
            sb.table(TABLE).select("item_key").eq("project_id", project_id).execute())}
        harvest_id = (harvest or {}).get("id") or record.get("harvest_id")
        new = [s.row(project_id, harvest_id) for s in specs if s.item_key not in existing]
        inserted = _insert_rows(sb, new)
        if not existing and inserted and total_failure(harvest) and not record.get("files_issue_total"):
            sb.table(CREATED_TABLE).update({"files_issue_total": True}).eq("project_id", project_id).execute()
        return inserted
    except Exception:  # noqa: BLE001 - best effort: the project exists either way
        logger.exception("rfp files: sync failed for project %s", project_id)
        return 0


# ── Resolution ───────────────────────────────────────────────────────────────


def _resolve_ids(sb, project_id: str, ids: list[str], actor_id: str | None, how: str) -> list[str]:
    """The conditional update: only items still open move. Returns the ids
    that moved."""
    clean = [str(i) for i in dict.fromkeys(ids) if i]
    out: list[str] = []
    now = _now_iso()
    for chunk in _chunks(clean):
        rows = _rows(
            sb.table(TABLE).update({"resolved_at": now, "resolved_by": actor_id, "resolved_how": how})
            .eq("project_id", project_id).in_("id", chunk).is_("resolved_at", "null").execute()
        )
        out.extend(str(r["id"]) for r in rows if r.get("id"))
    return out


def _audit_quietly(actor_id: str | None, action: str, project_id: str, payload: dict) -> None:
    try:
        audit(actor_id, action, AUDIT_ENTITY, project_id, payload)
    except Exception:  # noqa: BLE001 - the resolution stands
        logger.exception("rfp files: audit %s failed for %s", action, project_id)


def resolve_by_names(sb, project_id: str | None, filenames: list[Any], actor_id: str | None, *,
                     open_items: list[dict] | None = None) -> list[str]:
    """Resolve every open `file` item whose name matches one of `filenames`
    (`how = name_match`). Links and whole-harvest items never resolve by
    name. Returns the resolved ids; best effort ([] on any trouble)."""
    names = [n for n in filenames or [] if isinstance(n, str) and n.strip()]
    if not project_id or not names:
        return []
    try:
        items = open_items if open_items is not None else _open_items(sb, project_id)
        ids = [
            str(i["id"]) for i in items
            if i.get("kind") == KIND_FILE and i.get("id") and any(names_match(i.get("name"), n) for n in names)
        ]
        resolved = _resolve_ids(sb, project_id, ids, actor_id, HOW_NAME_MATCH)
    except Exception:  # noqa: BLE001 - the file is in whatever happens here
        logger.exception("rfp files: name match failed for project %s", project_id)
        return []
    if resolved:
        _audit_quietly(actor_id, AUDIT_RESOLVE, project_id,
                       {"how": HOW_NAME_MATCH, "item_ids": resolved[:50], "filenames": names[:10]})
    return resolved


def no_drawings_yet(sb, project_id: str | None) -> bool:
    """The project has no drawing or specification row yet
    (`files_needed.project_has_files`). Best effort: False on any trouble."""
    if not project_id:
        return False
    try:
        return not files_needed.project_has_files(sb, project_id)
    except Exception:  # noqa: BLE001
        logger.exception("rfp files: first-file check failed for project %s", project_id)
        return False


def is_first_file(sb, project_id: str | None, category: str | None) -> bool:
    """Called by an insert path BEFORE it inserts: True when `category` is a
    drawing or specification and the project has none yet. No query for any
    other category. Best effort: False on any trouble."""
    if category not in files_needed.CLEAR_CATEGORIES:
        return False
    return no_drawings_yet(sb, project_id)


def total_failure_live(record: dict | None) -> bool:
    """The issue's `total_failure`: the creation's stamp AND nothing promoted
    since (`files_promoted == 0`, read live), so a re-harvest that brought
    files in stops the "none came in" reading (review finding 4)."""
    rec = record or {}
    try:
        promoted = int(rec.get("files_promoted") or 0)
    except (TypeError, ValueError):
        promoted = 0
    return bool(rec.get("files_issue_total")) and promoted == 0


def after_file_insert(sb, project_id: str | None, *, category: str | None, filenames: list[Any],
                      actor_id: str | None, first_file: bool = False, by_person: bool = False) -> list[str]:
    """The hook every `project_files` insert path calls beside
    `files_needed.clear_if_satisfied`. Resolves the open `file` items a
    landed name matches (`filenames` carries the stored filename and, for a
    splitter segment, the source PDF's filename). When the insert was made
    BY A PERSON (`by_person`: an upload, a drop, a draft transfer, a merge;
    never the promotion's or the split's own inserts), was the project's
    first drawing or specification (`first_file`, from `is_first_file`
    before the insert) and the harvest brought in nothing
    (`total_failure_live`), it also resolves every open `link` and `harvest`
    item (`auto_first_file`); a named `file` item never clears that way
    (review finding 2). Returns the resolved ids; best effort, never
    raises."""
    if not project_id:
        return []
    try:
        items = _open_items(sb, project_id)
        if not items:
            return []
        resolved = resolve_by_names(sb, project_id, filenames, actor_id, open_items=items)
        if by_person and first_file and category in files_needed.CLEAR_CATEGORIES:
            if total_failure_live(_record(sb, project_id)):
                done = set(resolved)
                rest = [
                    str(i["id"]) for i in items
                    if i.get("id") and i.get("kind") in (KIND_LINK, KIND_HARVEST) and str(i["id"]) not in done
                ]
                auto = _resolve_ids(sb, project_id, rest, actor_id, HOW_AUTO_FIRST_FILE)
                if auto:
                    _audit_quietly(actor_id, AUDIT_RESOLVE, project_id,
                                   {"how": HOW_AUTO_FIRST_FILE, "item_ids": auto[:50], "category": category})
                resolved += auto
        return resolved
    except Exception:  # noqa: BLE001 - the file is in whatever happens here
        logger.exception("rfp files: after-insert hook failed for project %s", project_id)
        return []


def unresolve_name_matches(sb, project_id: str, ids: list[str]) -> None:
    """Undo a drop's up-front name match when storing the file then failed.
    Only rows still marked `name_match` move. Best effort."""
    clean = [str(i) for i in ids if i]
    if not clean:
        return
    try:
        for chunk in _chunks(clean):
            sb.table(TABLE).update({"resolved_at": None, "resolved_by": None, "resolved_how": None}).eq(
                "project_id", project_id).in_("id", chunk).eq("resolved_how", HOW_NAME_MATCH).execute()
    except Exception:  # noqa: BLE001
        logger.exception("rfp files: could not reopen the drop's name matches on %s", project_id)


def move_open_items(sb, source_id: str, target_id: str) -> list[str]:
    """A project merge (docs/PROJECT_MERGE.md, docs/RFP_MISSING_FILES.md 10):
    the source's OPEN items move to the target (`project_id` updated) before
    the source is archived, so nothing a person was working from disappears
    with it. Only onto a target that is itself an RFP-created project (it
    has an `rfp_created_projects` row): only that project's page shows the
    callout and only its record can carry a dismissal; otherwise the items
    stay with the archived source (one info line). A row whose `item_key`
    the target already has stays behind (the unique index; the target's own
    row stands). Returns the moved ids; best effort ([] on any trouble)."""
    try:
        items = _open_items(sb, source_id)
        if not items:
            return []
        if _record(sb, target_id) is None:
            logger.info(
                "rfp files: merge target %s is not an RFP-created project; %s open item(s) stay with source %s",
                target_id, len(items), source_id,
            )
            return []
        taken = {str(r.get("item_key")) for r in _items(sb, target_id)}
        ids = [str(i["id"]) for i in items if i.get("id") and str(i.get("item_key")) not in taken]
        moved: list[str] = []
        for chunk in _chunks(ids):
            rows = _rows(
                sb.table(TABLE).update({"project_id": target_id}).eq("project_id", source_id)
                .in_("id", chunk).execute()
            )
            moved.extend(str(r["id"]) for r in rows if r.get("id"))
        return moved
    except Exception:  # noqa: BLE001 - the merge stands without them
        logger.exception("rfp files: moving items from %s to %s failed", source_id, target_id)
        return []


def move_items_back(sb, ids: list[str], target_id: str, source_id: str) -> None:
    """Undo `move_open_items` when the merge itself then failed. Best effort."""
    clean = [str(i) for i in ids if i]
    try:
        for chunk in _chunks(clean):
            sb.table(TABLE).update({"project_id": source_id}).eq("project_id", target_id).in_("id", chunk).execute()
    except Exception:  # noqa: BLE001
        logger.exception("rfp files: moving items back to %s failed", source_id)


def resolve(sb, project_id: str, item_id: str, actor_id: str | None) -> dict | None:
    """Mark handled. None when the item is not this project's or is already
    resolved (the route's 404)."""
    rows = _rows(
        sb.table(TABLE).update({"resolved_at": _now_iso(), "resolved_by": actor_id, "resolved_how": HOW_MANUAL})
        .eq("id", item_id).eq("project_id", project_id).is_("resolved_at", "null").execute()
    )
    if not rows:
        return None
    row = rows[0]
    _audit_quietly(actor_id, AUDIT_RESOLVE, project_id,
                   {"how": HOW_MANUAL, "item_id": item_id, "kind": row.get("kind"), "name": row.get("name")})
    return row


def reopen(sb, project_id: str, item_id: str, actor_id: str | None) -> dict | None:
    """Any resolved item back to open (the three resolution columns
    cleared). None when the item is not this project's or is not resolved."""
    rows = _rows(
        sb.table(TABLE).update({"resolved_at": None, "resolved_by": None, "resolved_how": None})
        .eq("id", item_id).eq("project_id", project_id).not_.is_("resolved_at", "null").execute()
    )
    if not rows:
        return None
    row = rows[0]
    _audit_quietly(actor_id, AUDIT_REOPEN, project_id,
                   {"item_id": item_id, "kind": row.get("kind"), "name": row.get("name")})
    return row


def dismiss_all(sb, project_id: str, actor_id: str | None) -> dict | None:
    """"Dismiss all": stamp who and when on the record and resolve every
    open item with `how = dismiss_all`. None when the project has no created
    record; IssueConflict only when nothing is open. A second dismissal
    (items a later sync added after the first) re-stamps and resolves the
    new ones; the earlier `dismiss_all` rows stay resolved (review finding
    12). Returns the updated record."""
    record = _record(sb, project_id)
    if record is None:
        return None
    open_ids = [str(i["id"]) for i in _open_items(sb, project_id) if i.get("id")]
    if not open_ids:
        raise IssueConflict(MSG_NOTHING_OPEN)
    rows = _rows(
        sb.table(CREATED_TABLE)
        .update({"files_issue_dismissed_at": _now_iso(), "files_issue_dismissed_by": actor_id})
        .eq("project_id", project_id).execute()
    )
    if not rows:
        return None
    resolved = _resolve_ids(sb, project_id, open_ids, actor_id, HOW_DISMISS_ALL)
    _audit_quietly(actor_id, AUDIT_DISMISS, project_id, {"items": len(resolved)})
    return rows[0]


def undismiss(sb, project_id: str, actor_id: str | None) -> dict | None:
    """Undo "Dismiss all": clear the stamp and reopen exactly the items the
    dismissal resolved. None when the project has no created record;
    IssueConflict when it is not dismissed."""
    record = _record(sb, project_id)
    if record is None:
        return None
    rows = _rows(
        sb.table(CREATED_TABLE)
        .update({"files_issue_dismissed_at": None, "files_issue_dismissed_by": None})
        .eq("project_id", project_id).not_.is_("files_issue_dismissed_at", "null").execute()
    )
    if not rows:
        raise IssueConflict(MSG_NOT_DISMISSED)
    reopened = _rows(
        sb.table(TABLE).update({"resolved_at": None, "resolved_by": None, "resolved_how": None})
        .eq("project_id", project_id).eq("resolved_how", HOW_DISMISS_ALL).execute()
    )
    _audit_quietly(actor_id, AUDIT_UNDISMISS, project_id, {"items": len(reopened)})
    return rows[0]


# ── Wire shapes (section 7) ──────────────────────────────────────────────────


def _person(user_id: Any, names: dict[str, str | None]) -> dict | None:
    if not user_id:
        return None
    return {"id": str(user_id), "name": names.get(str(user_id))}


def item_wire(row: dict, names: dict[str, str | None]) -> dict:
    resolved = None
    if row.get("resolved_at"):
        resolved = {"at": row.get("resolved_at"), "by": _person(row.get("resolved_by"), names),
                    "how": row.get("resolved_how")}
    email = row.get("email_ref") if isinstance(row.get("email_ref"), dict) else None
    return {
        "id": str(row.get("id")),
        "kind": row.get("kind"),
        "group": row.get("group"),
        "origin": row.get("origin"),
        "provider": row.get("provider"),
        "status": row.get("status"),
        "name": row.get("name"),
        # Re-vetted on the way out: only an https link on a recognised host
        # ever reaches the page.
        "url": safe_url(row.get("url")),
        "reason": row.get("reason"),
        "email": {k: email.get(k) for k in ("from", "from_address", "subject", "received_at", "mailbox")}
        if email else None,
        "resolved": resolved,
    }


def _sort_key(row: dict) -> tuple:
    return (_GROUP_ORDER.get(str(row.get("group")), 9), normalise_name(row.get("name")), str(row.get("id")))


def issue_state(record: dict | None, open_count: int) -> str:
    """`open` whenever any item is open (a sync can add items after a
    dismissal, review finding 12); else `dismissed` when stamped; else
    `resolved`."""
    if open_count:
        return STATE_OPEN
    if (record or {}).get("files_issue_dismissed_at"):
        return STATE_DISMISSED
    return STATE_RESOLVED


def _job_files(sb, job_id: str) -> list[dict]:
    """One job's files in upload order, paged by PostgREST's row cap so a
    large job is never cut."""
    out: list[dict] = []
    start = 0
    while True:
        page = _rows(
            sb.table(FILES_TABLE).select("id, job_id, filename, status, error, created_at").eq("job_id", job_id)
            .order("created_at").order("id").range(start, start + _PAGE_ROWS - 1).execute()
        )
        out.extend(page)
        if len(page) < _PAGE_ROWS:
            return out
        start += _PAGE_ROWS


def _drop_summary(sb, project_id: str) -> dict | None:
    """The newest drop-zone splitter job of the project that holds at least
    one file, with its files' statuses aggregated (the page polls while it
    is processing). A job without files is never shown: it could only read
    `processing` forever (review finding 5). Jobs are read newest first and
    each one's files on their own (paged), so the newest job's files are
    never the ones a row cap cuts (review N3); usually one files read."""
    from app.services.rfp_split import SOURCE_PROJECT

    jobs = _rows(
        sb.table(JOBS_TABLE).select("id, status, created_at").eq("project_id", project_id)
        .eq("source", SOURCE_PROJECT).order("created_at", desc=True).limit(_DROP_JOBS_SCAN).execute()
    )
    job: dict | None = None
    files: list[dict] = []
    for candidate in jobs:
        files = _job_files(sb, str(candidate["id"]))
        if files:
            job = candidate
            break
    if job is None:
        return None
    failed = [f for f in files if f.get("status") == "failed"]
    return {
        "job_id": str(job["id"]),
        "status": job.get("status"),
        "files_total": len(files),
        "files_done": sum(1 for f in files if f.get("status") == "done"),
        "files_failed": len(failed),
        "failed": [{"filename": f.get("filename"), "error": f.get("error")} for f in failed[:_DROP_FAILED_MAX]],
        "created_at": job.get("created_at"),
    }


def _package_sent(project_id: str) -> bool:
    from app.routers.files import handoff_locked

    try:
        return bool(handoff_locked(project_id))
    except Exception:  # noqa: BLE001 - the drop route checks again
        logger.exception("rfp files: hand-off lock read failed for %s", project_id)
        return False


def issue_for_project(sb, project_id: str, *, record: dict | None = None,
                      names: dict[str, str | None] | None = None,
                      settings: Settings | None = None) -> dict | None:
    """The issue (section 7), or None when the project has no items at all.
    `record` is the created record when the caller holds one with the
    `files_issue_*` columns; `names` the profiles map `{id: full_name}` the
    caller already loaded (the few names needed are read otherwise)."""
    from app.services import rfp_file_drop

    items = _items(sb, project_id)
    if not items:
        return None
    if record is None or "files_issue_dismissed_at" not in record:
        record = _record(sb, project_id) or {}
    s = settings or get_settings()
    wanted = {r.get("resolved_by") for r in items} | {record.get("files_issue_dismissed_by")}
    wanted = {str(w) for w in wanted if w}
    names = dict(names or {})
    missing = wanted - set(names)
    if missing:
        names.update(_names(sb, missing))
    open_rows = sorted([r for r in items if not r.get("resolved_at")], key=_sort_key)
    done_rows = sorted(
        [r for r in items if r.get("resolved_at")],
        key=lambda r: (str(r.get("resolved_at") or ""), str(r.get("id"))), reverse=True,
    )
    dismissed = None
    if record.get("files_issue_dismissed_at"):
        dismissed = {"at": record.get("files_issue_dismissed_at"),
                     "by": _person(record.get("files_issue_dismissed_by"), names)}
    available, why = rfp_file_drop.splitter_state(s)
    return {
        "state": issue_state(record, len(open_rows)),
        "total_failure": total_failure_live(record),
        "counts": {
            "open": len(open_rows),
            "resolved": len(done_rows),
            "not_downloaded": sum(1 for r in open_rows if r.get("group") == GROUP_NOT_DOWNLOADED),
            "refused": sum(1 for r in open_rows if r.get("group") == GROUP_REFUSED),
        },
        "items": [item_wire(r, names) for r in open_rows + done_rows],
        "dismissed": dismissed,
        "drop": _drop_summary(sb, project_id),
        "splitter_available": available,
        "splitter_unavailable_reason": why,
        "package_sent": _package_sent(project_id),
        "page_cap": int(s.bid_split_max_pages_per_file),
    }


def flags_for_records(sb, records: list[dict]) -> dict[str, dict | None]:
    """`{project_id: {state, open, total} | None}` for the Created from RFP
    Ingestion page: one read of the page's items (chunked by 200 ids, each
    chunk paged by 1000 rows so PostgREST's row cap never truncates it,
    review finding 11), grouped by project. None for a project without
    items."""
    ids = sorted({str(r["project_id"]) for r in records if r.get("project_id")})
    counts: dict[str, list[int]] = {}
    for chunk in _chunks(ids):
        start = 0
        while True:
            page = _rows(
                sb.table(TABLE).select("id, project_id, resolved_at").in_("project_id", chunk)
                .order("id").range(start, start + _PAGE_ROWS - 1).execute()
            )
            for row in page:
                entry = counts.setdefault(str(row.get("project_id")), [0, 0])
                entry[1] += 1
                if not row.get("resolved_at"):
                    entry[0] += 1
            if len(page) < _PAGE_ROWS:
                break
            start += _PAGE_ROWS
    out: dict[str, dict | None] = {}
    for rec in records:
        pid = str(rec.get("project_id") or "")
        if pid not in counts:
            out[pid] = None
            continue
        open_count, total = counts[pid]
        out[pid] = {"state": issue_state(rec, open_count), "open": open_count, "total": total}
    return out

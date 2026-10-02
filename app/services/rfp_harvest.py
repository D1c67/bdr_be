"""RFP Ingestion: project data harvest (docs/RFP_HARVEST.md).

After the match step finds no existing project, the `harvest` pipeline step
enqueues one `rfp_harvest` queue job per email row and the row waits. The job
(`execute`) goes to the invitation platform, pulls the project facts and the
bidding documents, and parks them: facts on `rfp_harvests`, documents in an
Ingestion Sandbox run (`source_kind = rfp_email`). One `rfp_harvests` row per
platform object (`method` + `external_key`), reused by every later email
about the same object.

Four harvesters live behind `harvester_for`: Procore (RFP_HARVEST.md),
PipelineSuite (RFP_PIPELINESUITE.md, 2026-09-16: the GC's own plan room at
`<gc>.pipelinesuite.com`, logged into with the Project ID and Security Key
the invitation email carries, one stored session per portal host), SmartBid
(RFP_SMARTBID.md, 2026-10-01: ConstructConnect's platform, many GCs, every
invitation from the platform's own address; the email's View the Project
link carries a per-recipient, per-project passport key that buys a bearer
token for one harvest, nothing secret stored, the agreement gate read and
never accepted, the bid question never answered) and the email harvester
(RFP_HARVEST.md 2.5, 2026-09-16: the `organic`, `general` and `nonorganic`
methods, whose "platform" is the email itself: its attachments and its
cloud-share links, body in `rfp_email_harvest.py`, the pure policy and
trigger in `rfp_email_files.py`). The seam is where the next platforms
plug in.
BuildingConnected and NGEM (2026-09-15) and PlanHub (2026-09-16) are not
invitation methods: their mail is sanitized out at listing time. `gc_portal`
(2026-09-16) is the method for a GC that invites through a genuinely bespoke
portal; every such portal is different, so the scraper is chosen by the
sender's domain through `GC_PORTAL_SCRAPERS` (doc 2.3). The registry is empty
until the first scraper lands, and until then a gc_portal row drains to done
unharvested exactly like organic. The pure parts (`normalize_facts`,
`classify_manifest`, `html_to_text`, the `pipelinesuite_*` and `smartbid_*`
builders) have no I/O so the captured payloads can be tested exhaustively.

Failure policy (doc 2.2): a harvest never blocks the pipeline forever.
Unavailable (no credentials, logins locked, an interstitial) parks the row
without spending anything; transient trouble goes through the queue's
ladder; a permanent problem marks the harvest `failed` and moves the email
on with `harvest_id` set so the outcome is visible. The pipeline exit is
`create` (docs/RFP_CREATE.md 3) for success and failure alike; the create
step decides whether a project is made.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import os
import re
import tempfile
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import nh3

from app.core.config import Settings, get_settings
from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.services import (
    cloud_folders,
    graph_inbox,
    llm_queue,
    pipelinesuite_client as psc,
    procore_client as pc,
    rfp_email_files as ef,
    rfp_ingest,
    rfp_test,
    smartbid_client as sbc,
)
from app.services.notifications import notify_role
from app.services.rfp_email_auth import (
    METHOD_GC_PORTAL,
    METHOD_PIPELINESUITE,
    METHOD_PROCORE,
    METHOD_SMARTBID,
    address_domain,
    domain_covered_by,
)

logger = logging.getLogger(__name__)

TABLE = "rfp_harvests"
SESSIONS_TABLE = "rfp_harvest_sessions"
EMAILS_TABLE = "rfp_emails"

JOB_TYPE = "rfp_harvest"
MODEL_LABEL = "procore"
NOTIFY_LOGIN_FAILED = "rfp_harvest.login_failed"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"
HARVEST_STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_COMPLETE, STATUS_FAILED)

# rfp_emails statuses a manual run accepts (the pipeline runs on `harvest`).
MANUAL_STATUSES = ("done", "merged", "duplicate", "harvest")

FILE_ACCEPTED = "accepted"
FILE_REJECTED = "rejected"
FILE_TOO_LARGE = "too_large"
FILE_DOWNLOAD_FAILED = "download_failed"
FILE_SKIPPED_CAP = "skipped_cap"
# Two more, from the email harvester (doc 2.5): settled before the download
# loop runs, so it skips them.
FILE_REUSED = ef.FILE_REUSED
FILE_EXPANDED = ef.FILE_EXPANDED
# And one from SmartBid (RFP_SMARTBID.md 4): a plan-room file behind an
# agreement or a prequalification, never downloaded.
FILE_SKIPPED = "skipped"
_SETTLED_BEFORE_DOWNLOAD = (FILE_REUSED, FILE_EXPANDED, FILE_SKIPPED)
HARVESTER_EMAIL = ef.HARVESTER_EMAIL

KIND_DRAWING = "drawing"
KIND_SPECIFICATION = "specification"
KIND_OTHER = "other"

_ERROR_MAX_CHARS = 500
_TEXT_MAX_CHARS = 20_000
_SHORT_MAX_CHARS = 300
_NAME_MAX_CHARS = 200
_PATH_MAX_CHARS = 400
_RAW_MAX_CHARS = 200_000
_MAX_MEMBERS = 50
_MAX_FORM_ITEMS = 200
_DOWNLOAD_ATTEMPTS = 3
_TOUCH_EVERY_SECONDS = 60.0

_MSG_NO_LINK = "The email carries no usable Procore bid link."
_MSG_NO_EMAIL_FILES = "The email carries no attachments or share links to harvest."
_MSG_NO_HARVESTER = "No harvester exists for this invitation method."
_MSG_NOT_CONFIGURED = "Procore credentials are not configured."
_MSG_NO_GC_PORTAL_SCRAPER = "No scraper exists yet for this GC's portal ({domain})."
_MSG_GC_PORTAL_NOT_WIRED = (
    "The {scraper} scraper is registered for {domain} but nothing runs it yet."
)
_MSG_INTERRUPTED = "The harvest was interrupted; it will be retried."
_MSG_CLAIMED = "Another harvest of the same bid is running; waiting for it."
_MSG_TOO_MANY_FILES = "The bid package holds more files than the harvest accepts ({n} of {cap})."
_MSG_TOO_MANY_BYTES = "The bid package is larger than the harvest accepts ({mb} MB of {cap} MB)."
_MSG_NO_FILES = "The bid package has no documents to download."
_MSG_MANIFEST_SHAPE = "Procore answered with a document list the harvest does not understand."
_MSG_NO_DOWNLOAD_LINK = "The platform gave no download link for this file."

# PipelineSuite (docs/RFP_PIPELINESUITE.md section 4).
_MSG_NO_PS_REFERENCE = "The email carries no PipelineSuite Project ID and Security Key."
_MSG_PS_PAGE = "The portal answered with a page the harvest does not understand."
_MSG_PS_TOO_MANY_FILES = "The project holds more files than the harvest accepts ({n} of {cap})."
_MSG_PS_TOO_MANY_BYTES = "The project files are larger than the harvest accepts ({mb} MB of {cap} MB)."
_MSG_PS_NO_FILES = "The project has no files to download."
_MSG_PS_LOCKED = psc.LOCKED_MESSAGE
_MSG_PROCORE_LOCKED = "Procore logins are locked after repeated failures."
_PS_RAW_FILES_HEAD = 50

# SmartBid (docs/RFP_SMARTBID.md section 4). The caps and the empty-project
# sentences are PipelineSuite's (both speak of "the project").
_MSG_NO_SB_REFERENCE = "The email carries no SmartBid project link."
_MSG_SB_LOCKED = sbc.LOCKED_MESSAGE
_MSG_SB_RESTRICTED = "SmartBid requires an agreement for this file"
_SB_RAW_FILES_HEAD = 50
# BidProject keys `raw` never keeps: the passport key again, and the
# description (it is `description_text`).
_SB_RAW_DROP_KEYS = frozenset({"PassportKey", "ProjectDescription"})
_SB_SECRET_KEY_RE = re.compile(r"passport|token|secret|password", re.IGNORECASE)

# The four client exception families _harvest_files and the job map the same
# way (the email harvester's session raises the cloud_folders pair).
_TRANSIENT_ERRORS = (
    pc.ProcoreTransient, psc.PipelineSuiteTransient, sbc.SmartBidTransient,
    cloud_folders.CloudTransient,
)
_FORBIDDEN_ERRORS = (
    pc.ProcoreForbidden, psc.PipelineSuiteForbidden, sbc.SmartBidForbidden,
    cloud_folders.CloudForbidden,
)
# (unavailable, forbidden, base) per platform, for the job's failure mapping.
_PROCORE_ERRORS = (pc.ProcoreUnavailable, pc.ProcoreForbidden, pc.ProcoreError)
_PIPELINESUITE_ERRORS = (
    psc.PipelineSuiteUnavailable, psc.PipelineSuiteForbidden, psc.PipelineSuiteError
)
_SMARTBID_ERRORS = (sbc.SmartBidUnavailable, sbc.SmartBidForbidden, sbc.SmartBidError)
_EMAIL_ERRORS = (cloud_folders.CloudUnavailable, cloud_folders.CloudForbidden, cloud_folders.CloudError)


# ── Errors ───────────────────────────────────────────────────────────────────


class RfpHarvestTransient(RuntimeError):
    """Worth retrying: network, a 5xx, storage. The queue's ladder applies."""

    llm_error_kind = "infrastructure"


class RfpHarvestPermanent(ValueError):
    """A permanent, app-authored refusal. `http_status` lets the router map it."""

    llm_error_kind = "bad_input"

    def __init__(self, message: str, *, http_status: int = 409) -> None:
        super().__init__(message)
        self.http_status = http_status


class _ClaimLost(Exception):
    """The harvest row's claim_token changed under us."""


# ── Small helpers ────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _cap(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def _error(exc: BaseException) -> str:
    return (str(exc) or type(exc).__name__)[:_ERROR_MAX_CHARS]


_BLOCK_BREAK_RE = re.compile(r"<\s*(br|/p|/div|/li|/tr|/h[1-6])\s*/?\s*>", re.IGNORECASE)
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def html_to_text(value: Any, *, limit: int = _TEXT_MAX_CHARS) -> str | None:
    """Visible text of a rich-text value: block ends become newlines, every
    tag is dropped (nh3, no tags allowed, script/style contents removed),
    entities resolved, whitespace tidied per line, capped."""
    if value is None:
        return None
    text = str(value)
    if not text.strip():
        return None
    text = _BLOCK_BREAK_RE.sub("\n", text)
    text = nh3.clean(text, tags=set(), attributes={}, strip_comments=True)
    text = html_lib.unescape(text).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out = _MULTI_BLANK_RE.sub("\n\n", "\n".join(lines)).strip()
    return out[:limit] or None


def _address_text(value: Any) -> str | None:
    text = html_to_text(value, limit=_SHORT_MAX_CHARS * 2)
    if not text:
        return None
    return ", ".join(part for part in (p.strip() for p in text.splitlines()) if part)[:_SHORT_MAX_CHARS]


def _person(entry: Any) -> dict | None:
    if not isinstance(entry, dict):
        return None
    first = _cap(entry.get("first") or entry.get("first_name"), 80) or ""
    last = _cap(entry.get("last") or entry.get("last_name"), 80) or ""
    name = " ".join(p for p in (first, last) if p) or None
    email = _cap(entry.get("email") or entry.get("email_address"), 200)
    if email:
        email = email.lower()
    if not name and not email:
        return None
    return {"name": name, "email": email}


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _iso_or_none(value: Any) -> str | None:
    """An ISO instant for a timestamp, or the bare `YYYY-MM-DD` for a
    date-only platform field (Procore sends award and walk-through dates
    without a time; turning them into UTC midnight would shift the day)."""
    if isinstance(value, str) and _DATE_ONLY_RE.match(value.strip()):
        return value.strip()
    dt = _parse_ts(value)
    return _iso(dt) if dt else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ── Pure: facts ──────────────────────────────────────────────────────────────


def _form_items(section: Any) -> list[dict]:
    items = section.get("bid_form_items") if isinstance(section, dict) else None
    out: list[dict] = []
    for item in (items or [])[:_MAX_FORM_ITEMS]:
        if not isinstance(item, dict):
            continue
        entry = {
            "description": html_to_text(
                item.get("description") or item.get("title") or item.get("name"),
                limit=_SHORT_MAX_CHARS * 2,
            ),
        }
        for key in ("unit", "unit_of_measure", "quantity", "position", "response_type"):
            if item.get(key) not in (None, ""):
                entry[key] = _cap(item.get(key), 80)
        out.append(entry)
    return out


def _form_sections(sections: Any) -> list[dict]:
    out: list[dict] = []
    for section in (sections or [])[:_MAX_FORM_ITEMS]:
        if not isinstance(section, dict):
            continue
        out.append(
            {
                "title": _cap(section.get("title"), _NAME_MAX_CHARS),
                "items": _form_items(section),
            }
        )
    return out


def _kind_for(row_type: str | None) -> str:
    text = (row_type or "").lower()
    if "drawing" in text:
        return KIND_DRAWING
    if "specification" in text:
        return KIND_SPECIFICATION
    return KIND_OTHER


def _discipline_for(kind: str, file_path: str) -> str | None:
    parts = [p for p in file_path.split("/") if p]
    if len(parts) < 2:
        return None
    if kind == KIND_DRAWING:
        # Bid_Drawings/Current/<Discipline>/<file>
        if len(parts) >= 4 and parts[1].lower() == "current":
            return parts[2][:80]
        return parts[-2][:80] if len(parts) >= 3 else None
    if kind == KIND_SPECIFICATION:
        # Specifications/<Division>/<file>
        return parts[1][:80] if len(parts) >= 3 else None
    return parts[0][:80]


def classify_manifest(docs: Any) -> list[dict]:
    """The documents manifest as harvest file entries (no URLs). Raises
    RfpHarvestPermanent when the payload is not the shape the bid sheet
    returns. The signed URL of each entry is returned separately by
    `manifest_urls`, never stored."""
    if not isinstance(docs, dict) or not isinstance(docs.get("files"), list):
        raise RfpHarvestPermanent(_MSG_MANIFEST_SHAPE)
    entries: list[dict] = []
    for row in docs["files"]:
        if not isinstance(row, dict):
            continue
        file_path = _cap(row.get("file_path"), _PATH_MAX_CHARS)
        if not file_path:
            continue
        kind = _kind_for(row.get("type") if isinstance(row.get("type"), str) else None)
        drawing = row.get("drawing") if isinstance(row.get("drawing"), dict) else {}
        entries.append(
            {
                "file_path": file_path,
                "size": max(0, _int_or_none(row.get("size")) or 0),
                "kind": kind,
                "discipline": _discipline_for(kind, file_path),
                "drawing_title": _cap(drawing.get("title"), _NAME_MAX_CHARS),
                "revision": _cap(drawing.get("revision"), 40),
                "sandbox_file_id": None,
                "status": None,
                "error": None,
            }
        )
    return entries


def manifest_urls(docs: Any) -> list[str | None]:
    """The signed download URL per manifest row, aligned with
    `classify_manifest` (rows without a file_path are skipped the same way)."""
    out: list[str | None] = []
    for row in (docs or {}).get("files") or []:
        if not isinstance(row, dict) or not _cap(row.get("file_path"), _PATH_MAX_CHARS):
            continue
        url = row.get("s3_source")
        out.append(url if isinstance(url, str) else None)
    return out


def documents_summary(entries: list[dict]) -> dict:
    return {
        "count": len(entries),
        "bytes": sum(int(e.get("size") or 0) for e in entries),
        "kinds": dict(Counter(e["kind"] for e in entries)),
        "disciplines": dict(Counter(e["discipline"] for e in entries if e.get("discipline"))),
    }


_RAW_DROP_KEYS = frozenset(
    {
        "s3_source", "png_s3_source", "thumbnail_url", "project_logo_url", "project_image_url",
        "streaming_url", "attachments_zip_streaming_url", "bid_docs_manifest", "links",
        "legacy_links", "mailto", "cc_mailto", "recipient_list", "recipient_ids",
        "recipient_list_with_email_and_number", "numbers", "company_phone", "mobile_phone",
        "business_phone", "fax_number", "contact", "point_of_contact_login_id",
        "distribution_member_ids", "nda_attachments", "avatar_url",
    }
)
_SIGNED_URL_RE = re.compile(r"(?:[?&](?:sig|X-Amz-Signature|Signature)=)", re.IGNORECASE)


def _trim_raw(value: Any, depth: int = 0) -> Any:
    """The platform payload without signed URLs, phone numbers, the
    recipient list and link tables. Bounded depth; strings capped."""
    if depth > 8:
        return None
    if isinstance(value, dict):
        return {
            k: _trim_raw(v, depth + 1)
            for k, v in value.items()
            if isinstance(k, str) and k not in _RAW_DROP_KEYS
        }
    if isinstance(value, list):
        return [_trim_raw(v, depth + 1) for v in value[:500]]
    if isinstance(value, str):
        if _SIGNED_URL_RE.search(value):
            return None
        return value[:_TEXT_MAX_CHARS]
    return value


def normalize_facts(
    ref: pc.ProcoreRef, bid_package: Any, bid: Any, bid_form: Any, entries: list[dict]
) -> dict:
    """The doc 4 `data` document from the three Procore payloads plus the
    classified manifest. Every string capped; HTML reduced to text."""
    bp = bid_package if isinstance(bid_package, dict) else {}
    bd = bid if isinstance(bid, dict) else {}
    bf = bid_form if isinstance(bid_form, dict) else {}
    requester = bd.get("bid_requester") if isinstance(bd.get("bid_requester"), dict) else {}
    project = bd.get("project") if isinstance(bd.get("project"), dict) else {}
    poc = _person(bp.get("point_of_contact")) or _person(requester)
    members = [
        p for p in (_person(m) for m in (bp.get("distribution_members") or [])[:_MAX_MEMBERS]) if p
    ]
    recipients = sorted(
        {
            p["email"]
            for p in (_person(m) for m in (bd.get("recipient_list") or [])[:_MAX_MEMBERS])
            if p and p.get("email")
        }
    )
    gc_name = _cap(requester.get("company"), _NAME_MAX_CHARS)
    return {
        "platform": pc.PROVIDER,
        "company_id": ref.company_id,
        "project_id": str(bp.get("project_id") or ref.project_id or "") or None,
        "bid_package_id": str(bp.get("id") or ref.package_id or "") or None,
        "bid_id": ref.bid_id,
        "bid_form_id": str(bd.get("bid_form_id") or bf.get("id") or "") or None,
        "project_name": _cap(bp.get("project_name") or project.get("name"), _NAME_MAX_CHARS),
        "project_address": _address_text(bp.get("project_location") or project.get("address")),
        "project_latitude": bp.get("project_latitude")
        if isinstance(bp.get("project_latitude"), (int, float)) else None,
        "project_longitude": bp.get("project_longitude")
        if isinstance(bp.get("project_longitude"), (int, float)) else None,
        "bid_package_title": _cap(bp.get("title") or bd.get("bid_package_title"), _NAME_MAX_CHARS),
        "bid_package_number": _int_or_none(bp.get("number")),
        "bid_due_at": _iso_or_none(bp.get("bid_due_date") or bd.get("due_date")),
        "accept_post_due_submissions": bool(bp.get("accept_post_due_submissions")),
        "anticipated_award_date": _iso_or_none(bp.get("anticipated_award_date")),
        "pre_bid_walk_through_date": _iso_or_none(bp.get("pre_bid_walk_through_date")),
        "pre_bid_walk_through_notes": html_to_text(bp.get("pre_bid_walk_through_notes")),
        "pre_bid_meeting_date": _iso_or_none(bp.get("pre_bid_meeting_date")),
        "pre_bid_meeting_location": html_to_text(
            bp.get("pre_bid_meeting_location"), limit=_SHORT_MAX_CHARS * 2
        ),
        "pre_bid_meeting_online_link": _cap(bp.get("pre_bid_meeting_online_link"), _SHORT_MAX_CHARS),
        "pre_bid_meeting_notes": html_to_text(bp.get("pre_bid_meeting_notes")),
        "pre_bid_rfi_deadline_date": _iso_or_none(bp.get("pre_bid_rfi_deadline_date")),
        "public_bid_opening_date": _iso_or_none(bp.get("public_bid_opening_details_date")),
        "public_bid_opening_location": html_to_text(
            bp.get("public_bid_opening_details_location"), limit=_SHORT_MAX_CHARS * 2
        ),
        "gc": {
            "name": gc_name,
            "address": _address_text(requester.get("vendor_address") or requester.get("company_address")),
            "phone": _cap(requester.get("business_phone") or requester.get("company_phone"), 60),
            "website": _cap(requester.get("company_website"), _SHORT_MAX_CHARS),
        },
        "point_of_contact": {
            **(poc or {"name": None, "email": None}),
            "phone": _cap(requester.get("business_phone") or requester.get("mobile_phone"), 60),
        },
        "distribution_members": members,
        "invited_recipients": recipients,
        "invitation_last_sent_at": _iso_or_none(bd.get("invitation_last_sent_at")),
        "bid_form": {
            "title": _cap(bf.get("title") or bd.get("bid_form_title"), _NAME_MAX_CHARS),
            "base_bid_sections": _form_sections(bf.get("base_bid")),
            "alternates": _form_sections(bf.get("alternates")),
        },
        "accounting_method": _cap(bp.get("accounting_method"), 40),
        "lump_sum_bidding": bool(bp.get("lump_sum_bidding")),
        "require_nda": bool(bp.get("require_nda") or bd.get("require_nda")),
        "blind_bidding": bool(bp.get("blind_bidding")),
        "documents": documents_summary(entries),
    }


def build_raw(bid_package: Any, bid: Any, bid_form: Any, docs: Any) -> dict:
    """The trimmed payloads, bounded: the manifest rows are dropped from
    `raw` (they live in `files`) and the whole thing is capped by size."""
    docs_head = dict(docs) if isinstance(docs, dict) else {}
    docs_head.pop("files", None)
    raw = {
        "bid_package": _trim_raw(bid_package),
        "bid": _trim_raw(bid),
        "bid_form": _trim_raw(bid_form),
        "documents": _trim_raw(docs_head),
    }
    if len(json.dumps(raw, default=str)) > _RAW_MAX_CHARS:
        raw = {"bid_package": _trim_raw(bid_package), "bid": _trim_raw(bid)}
    return raw


# ── Pure: PipelineSuite facts (RFP_PIPELINESUITE.md section 4) ───────────────

_PS_BANNER_RE = re.compile(r"^\W*click\b.*\b(yes|no|unsure)\b", re.IGNORECASE)
_PS_RFI_CONTACT_RE = re.compile(
    r"contact\s+(?P<name>[A-Za-z][A-Za-z .'-]{1,80}?)\s+(?:with|for)\s+(?:any\s+)?rfi",
    re.IGNORECASE,
)
# Bounded quantifiers keep search() linear on long '@'-free word runs
# (the unbounded form was quadratic on harvested scope text: ReDoS).
_PS_EMAIL_RE = re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}")


def pipelinesuite_files(page: psc.ProjectPage) -> tuple[list[dict], list[str | None]]:
    """The harvest file entries (no URLs; sizes KB x 1024, kind from the
    name, discipline always None) and the aligned in-memory download URL
    list, in page order (folders walked depth-first)."""
    entries: list[dict] = []
    urls: list[str | None] = []
    for row in page.files:
        name = _cap(row.get("name"), _NAME_MAX_CHARS) or "document"
        folder = _cap(row.get("folder"), _PATH_MAX_CHARS - len(name) - 1) or ""
        file_path = f"{folder}/{name}" if folder else name
        size_kb = row.get("size_kb")
        entries.append(
            {
                "file_path": file_path[:_PATH_MAX_CHARS],
                "size": max(0, int(size_kb or 0)) * 1024,
                "kind": psc.classify_name(name),
                "discipline": None,
                "file_id": _cap(row.get("file_id"), 40),
                "uploaded_on": _cap(row.get("uploaded_on"), 40),
                "sandbox_file_id": None,
                "status": None,
                "error": None,
            }
        )
        url = row.get("url")
        urls.append(url if isinstance(url, str) and url else None)
    return entries, urls


def pipelinesuite_description(scope: str | None) -> str | None:
    """The scope as the description: the leading CLICK YES / NO / UNSURE
    banner line(s) dropped, blank runs squeezed, capped."""
    if not scope:
        return None
    lines = [line for line in str(scope)[: _TEXT_MAX_CHARS * 2].splitlines() if not _PS_BANNER_RE.match(line.strip())]
    text = _MULTI_BLANK_RE.sub("\n\n", "\n".join(line.rstrip() for line in lines)).strip()
    return text[:_TEXT_MAX_CHARS] or None


def pipelinesuite_instructions(info: dict) -> str | None:
    """`other_info` and `plans` when present, joined by a blank line."""
    parts = [str(info.get(k)).strip() for k in ("other_info", "plans") if info.get(k)]
    text = "\n\n".join(p for p in parts if p)
    return text[:_TEXT_MAX_CHARS] or None


def _ps_address(info: dict) -> str | None:
    """address, city, "state zip" joined by ", "; the location appended in
    parentheses when it says something the address does not."""
    address = _cap(info.get("address"), _SHORT_MAX_CHARS)
    city = _cap(info.get("city"), 80)
    state_zip = " ".join(p for p in (_cap(info.get("state"), 40), _cap(info.get("zip"), 20)) if p)
    joined = ", ".join(p for p in (address, city, state_zip) if p)
    location = _cap(info.get("location"), _SHORT_MAX_CHARS)
    if location:
        known = joined.lower()
        adds = not any(part.strip() and part.strip().lower() in known for part in location.split(","))
        if not joined:
            joined = location
        elif adds:
            joined = f"{joined} ({location})"
    return joined[:_SHORT_MAX_CHARS] or None


def _ps_point_of_contact(page: psc.ProjectPage) -> dict | None:
    """The first Project Contact; else the RFI contact the scope names when
    an email address appears there; else None."""
    if page.contacts:
        first = page.contacts[0]
        return {
            "name": _cap(first.get("name"), _NAME_MAX_CHARS),
            "email": _cap(first.get("email"), 200),
            "phone": _cap(first.get("phone"), 60),
        }
    scope = (page.info.get("scope") or "")[:_TEXT_MAX_CHARS]
    email_match = _PS_EMAIL_RE.search(scope)
    if not email_match:
        return None
    name = None
    named = _PS_RFI_CONTACT_RE.search(scope)
    if named:
        raw = " ".join(named.group("name").split())
        name = raw.title() if raw.isupper() else raw
    return {"name": _cap(name, _NAME_MAX_CHARS), "email": email_match.group(0).lower(), "phone": None}


def normalize_pipelinesuite_facts(
    ref: psc.PipelineSuiteRef, page: psc.ProjectPage, entries: list[dict], tracking: dict | None
) -> dict:
    """The section 4 `data` document for a PipelineSuite harvest. Never the
    key, never the confirmation form's `cne` / `c` tokens, no URLs."""
    info = page.info
    contacts = [
        {
            "company": _cap(c.get("company"), _NAME_MAX_CHARS),
            "name": _cap(c.get("name"), _NAME_MAX_CHARS),
            "title": _cap(c.get("title"), _NAME_MAX_CHARS),
            "phone": _cap(c.get("phone"), 60),
            "extension": _cap(c.get("extension"), 20),
            "fax": _cap(c.get("fax"), 60),
            "email": _cap(c.get("email"), 200),
        }
        for c in page.contacts[:_MAX_MEMBERS]
    ]
    first_phone = next((c["phone"] for c in contacts if c.get("phone")), None)
    folders = sorted({e["file_path"].rsplit("/", 1)[0] for e in entries if "/" in e["file_path"]})
    summary = documents_summary(entries)
    summary.pop("disciplines", None)
    summary["folders"] = folders
    return {
        "platform": psc.PROVIDER,
        "portal_host": ref.host,
        "portal_label": ref.label,
        "project_id": ref.project_id,
        "project_number": _cap(info.get("project_number"), _NAME_MAX_CHARS),
        "project_name": _cap(info.get("project_name") or page.title, _NAME_MAX_CHARS),
        "project_address": _ps_address(info),
        "location": _cap(info.get("location"), _SHORT_MAX_CHARS),
        "bid_due_at": psc.bid_due_at(info),
        "bid_date_text": _cap(info.get("bid_date"), 80),
        "bid_time_text": _cap(info.get("bid_time"), 40),
        "gc": {
            "name": _cap(page.gc_name, _NAME_MAX_CHARS),
            "address": None,
            "phone": first_phone,
            "website": None,
        },
        "point_of_contact": _ps_point_of_contact(page),
        "contacts": contacts,
        "invited_name": _cap(page.invited_name, _NAME_MAX_CHARS),
        "trades": [
            {"code": _cap(t.get("code"), 40), "name": _cap(t.get("name"), _NAME_MAX_CHARS)}
            for t in page.trades[:_MAX_MEMBERS]
        ],
        "notices": [
            {
                "title": _cap(n.get("title"), _NAME_MAX_CHARS),
                "created_by": _cap(n.get("created_by"), _NAME_MAX_CHARS),
                "created_on": _cap(n.get("created_on"), 40),
            }
            for n in page.notices[:_MAX_MEMBERS]
        ],
        "other_info": _cap(info.get("other_info"), _TEXT_MAX_CHARS),
        "plans": _cap(info.get("plans"), _TEXT_MAX_CHARS),
        "response_recorded": bool(page.response_recorded),
        "tracking": tracking,
        "documents": summary,
    }


def build_pipelinesuite_raw(page: psc.ProjectPage) -> dict:
    """`{project_info, trades, notices, contacts, files_head}`: the page as
    parsed, no URL, no key, no form token."""
    head = [
        {
            "file_id": f.get("file_id"),
            "name": f.get("name"),
            "folder": f.get("folder"),
            "size_kb": f.get("size_kb"),
            "uploaded_on": f.get("uploaded_on"),
        }
        for f in page.files[:_PS_RAW_FILES_HEAD]
    ]
    raw = {
        "project_info": {k: (v[:_TEXT_MAX_CHARS] if isinstance(v, str) else v) for k, v in page.info.items()},
        "trades": [dict(t) for t in page.trades[:_MAX_MEMBERS]],
        "notices": [dict(n) for n in page.notices[:_MAX_MEMBERS]],
        "contacts": [dict(c) for c in page.contacts[:_MAX_MEMBERS]],
        "files_head": head,
        "title": page.title,
        "gc_name": page.gc_name,
        "invited_name": page.invited_name,
    }
    if len(json.dumps(raw, default=str)) > _RAW_MAX_CHARS:
        raw.pop("files_head", None)
    return raw


# ── Pure: SmartBid facts (RFP_SMARTBID.md section 4) ─────────────────────────


def _sb_kind(name: str, folder: str) -> str:
    """The file name's kind, else its folder path's: SmartBid GCs often keep
    the meaning in the folder ("Shell Bid Set/E-2026-09-01_..._REV_1.pdf",
    "Plans/Bid Drawings/...") and give the file a sheet code for a name."""
    kind = sbc.classify_name(name)
    if kind == psc.KIND_OTHER and folder:
        # The folder text carries the file's extension, so a spreadsheet
        # under "Plans/" stays other.
        return sbc.classify_name(folder + os.path.splitext(name)[1])
    return kind


def smartbid_files(project: sbc.SmartBidProject) -> tuple[list[dict], list[dict | None]]:
    """The harvest file entries (no URLs; sizes KB x 1024, kind from the
    name or else its folder, discipline always None; a restricted entry
    already `skipped`) and
    the aligned in-memory locator list: the parsed plan-room entry the
    client's `download` takes (its Href never leaves memory), None for a
    restricted entry. Plan-room order (folders walked depth-first)."""
    entries: list[dict] = []
    locators: list[dict | None] = []
    for row in project.files:
        name = _cap(row.get("name"), _NAME_MAX_CHARS) or "document"
        folder = _cap(row.get("folder"), _PATH_MAX_CHARS - len(name) - 1) or ""
        file_path = f"{folder}/{name}" if folder else name
        restricted = bool(row.get("restricted"))
        entries.append(
            {
                "file_path": file_path[:_PATH_MAX_CHARS],
                "size": max(0, int(row.get("size_kb") or 0)) * 1024,
                "kind": _sb_kind(name, folder),
                "discipline": None,
                "file_id": _cap(row.get("file_id"), 40),
                "uploaded_on": _cap(row.get("uploaded_on"), 40),
                "sandbox_file_id": None,
                "status": FILE_SKIPPED if restricted else None,
                "error": _MSG_SB_RESTRICTED if restricted else None,
            }
        )
        locators.append(None if restricted else dict(row))
    return entries, locators


def _sb_address(project: sbc.SmartBidProject) -> str | None:
    """address1, address2, city, "state zip" joined by ", "."""
    state_zip = " ".join(p for p in (_cap(project.state, 40), _cap(project.zip, 20)) if p)
    parts = (
        _cap(project.address1, _SHORT_MAX_CHARS), _cap(project.address2, _SHORT_MAX_CHARS),
        _cap(project.city, 80), state_zip,
    )
    return ", ".join(p for p in parts if p)[:_SHORT_MAX_CHARS] or None


def normalize_smartbid_facts(
    ref: sbc.SmartBidRef, project: sbc.SmartBidProject, entries: list[dict], tracking: dict | None
) -> dict:
    """The section 4 `data` document for a SmartBid harvest. Never the
    passport key, never a token, no URLs."""
    folders = sorted({e["file_path"].rsplit("/", 1)[0] for e in entries if "/" in e["file_path"]})
    summary = documents_summary(entries)
    summary.pop("disciplines", None)
    summary["folders"] = folders
    summary["restricted"] = sum(1 for e in entries if e.get("status") == FILE_SKIPPED)
    due_at = sbc.bid_due_at(project)
    _, zone_assumed = sbc.bid_due_zone(project.time_zone_short)
    manager = _cap(project.manager, _NAME_MAX_CHARS)
    phone = _cap(project.phone, 60)
    pre_bid = project.pre_bid if isinstance(project.pre_bid, dict) else None
    return {
        "platform": sbc.PROVIDER,
        "bid_project_id": project.bid_project_id or int(ref.bid_project_id),
        "system_id": project.system_id,
        "project_name": _cap(project.title, _NAME_MAX_CHARS),
        "project_address": _sb_address(project),
        "bid_due_at": due_at,
        "bid_due_text": _cap(project.bid_due_text, 80),
        "bid_due_tz": _cap(project.time_zone_short, 10),
        "bid_due_tz_assumed": bool(due_at and zone_assumed),
        "gc": {
            "name": _cap(project.gc_name, _NAME_MAX_CHARS),
            "address": None,
            "phone": phone,
            "fax": _cap(project.fax, 60),
            "website": None,
        },
        "point_of_contact": (
            {"name": manager, "email": None, "phone": phone} if (manager or phone) else None
        ),
        "owner": _cap(project.owner, _NAME_MAX_CHARS),
        "architect": _cap(project.architect, _NAME_MAX_CHARS),
        "project_status": _cap(project.project_status, 80),
        "past_due": bool(project.past_due),
        "allow_late_proposal": bool(project.allow_late_proposal),
        "pre_bid": (
            {
                "date": _cap(pre_bid.get("date"), 80),
                "time_zone": _cap(pre_bid.get("time_zone"), 10),
                "mandatory": bool(pre_bid.get("mandatory")),
            }
            if pre_bid else None
        ),
        "invitations": [
            {
                "code": _cap(i.get("code"), 40),
                "name": _cap(i.get("name"), _NAME_MAX_CHARS),
                "status": _cap(i.get("status"), 40),
            }
            for i in project.invitations[:_MAX_MEMBERS]
        ],
        "response_recorded": bool(project.response_recorded),
        "tracking": tracking,
        "documents": summary,
    }


def build_smartbid_raw(payload: Any, project: sbc.SmartBidProject) -> dict:
    """`{bid_project, invitations, files_head}`: the BidProject without the
    passport key (or any key-, token- or password-named field) and without
    the description, the parsed invitations, the first plan-room entries
    without their links. No key, no token, no URL a file could be fetched by."""
    bp = payload.get("BidProject") if isinstance(payload, dict) else None
    bid_project = {
        k: v for k, v in (bp if isinstance(bp, dict) else {}).items()
        if isinstance(k, str) and k not in _SB_RAW_DROP_KEYS and not _SB_SECRET_KEY_RE.search(k)
    }
    head = [
        {
            "file_id": f.get("file_id"),
            "name": f.get("name"),
            "folder": f.get("folder"),
            "size_kb": f.get("size_kb"),
            "uploaded_on": f.get("uploaded_on"),
            "version": f.get("version"),
            "restricted": bool(f.get("restricted")),
        }
        for f in project.files[:_SB_RAW_FILES_HEAD]
    ]
    raw = {
        "bid_project": _trim_raw(bid_project),
        "invitations": [dict(i) for i in project.invitations[:_MAX_MEMBERS]],
        "files_head": head,
    }
    if len(json.dumps(raw, default=str)) > _RAW_MAX_CHARS:
        raw.pop("files_head", None)
    return raw


# ── GC portal scrapers (doc 2.3) ─────────────────────────────────────────────
# A `gc_portal` invitation comes from a GC that runs its own bidding portal.
# Every portal is different, so the scraper is chosen by the sender's domain:
# `{"gc.example": "gc_example"}` names the scraper for mail from gc.example
# and its subdomains (label boundary, like authorized-sender domain rules).
# The key is the From domain, not the rule's, so a row whose method was
# corrected by hand still resolves. Empty until the first scraper lands.
GC_PORTAL_SCRAPERS: dict[str, str] = {}


def gc_portal_scraper_for(row: dict) -> str | None:
    """The registered scraper for this email's sender domain, or None."""
    domain = address_domain(row.get("from_address"))
    if not domain:
        return None
    for rule_domain, scraper in GC_PORTAL_SCRAPERS.items():
        if scraper and domain_covered_by(domain, rule_domain):
            return scraper
    return None


# ── Harvester registry ───────────────────────────────────────────────────────


PlatformRef = pc.ProcoreRef | psc.PipelineSuiteRef | sbc.SmartBidRef
# Every reference the row helpers read `external_key` / `external_url` off.
AnyRef = PlatformRef | ef.EmailRef


def harvester_for(row: dict, settings: Settings | None = None) -> str | None:
    """The harvester key for this email, or None: the method has none, the
    slice is off, the harvester's credentials are empty (Procore) or its
    flag is off (PipelineSuite, SmartBid, the email harvester), or
    (gc_portal) no scraper is registered for the sender's domain."""
    s = settings or get_settings()
    if not (s.rfp_ingest_enabled and s.rfp_harvest_enabled):
        return None
    method = row.get("invitation_method")
    if method == METHOD_PROCORE and s.procore_configured:
        return METHOD_PROCORE
    if method == METHOD_PIPELINESUITE and s.pipelinesuite_enabled:
        return METHOD_PIPELINESUITE
    if method == METHOD_SMARTBID and s.smartbid_enabled:
        return METHOD_SMARTBID
    if method == METHOD_GC_PORTAL and gc_portal_scraper_for(row) is not None:
        return METHOD_GC_PORTAL
    if method in ef.EMAIL_METHODS and s.rfp_harvest_email_enabled:
        return HARVESTER_EMAIL
    return None


def platform_reference(method: str | None, body_text: str | None) -> PlatformRef | None:
    """The platform object the email names, parsed from its text body: a
    ProcoreRef, a PipelineSuiteRef or a SmartBidRef (all answer to
    `external_key` and `external_url`). None for every other method."""
    if method == METHOD_PROCORE:
        return pc.parse_reference(body_text)
    if method == METHOD_PIPELINESUITE:
        return psc.parse_reference(body_text)
    if method == METHOD_SMARTBID:
        return sbc.parse_reference(body_text)
    return None


def reference_for(row: dict) -> AnyRef | None:
    """What this email names for its harvester, from the row: the platform
    object parsed off the text body (Procore, PipelineSuite, SmartBid), or (the email
    harvester) the email itself when its stored attachment listing or its
    body carries anything to harvest (`rfp_email_files.email_reference`).
    None for every other method, and the reason the match exit, the step
    and the job drain a row to done."""
    method = row.get("invitation_method")
    if method in ef.EMAIL_METHODS:
        return ef.email_reference(row)
    return platform_reference(method, row.get("body_text"))


def session_provider_for(row: dict, settings: Settings | None = None) -> str | None:
    """The rfp_harvest_sessions provider this email's harvest logs in
    through: `procore`, `pipelinesuite:<host>` from the parsed reference, or
    `smartbid` when the email carries a SmartBid project link. None when the
    method has no login session or the reference is missing."""
    method = row.get("invitation_method")
    if method == METHOD_PROCORE:
        return pc.PROVIDER
    if method == METHOD_PIPELINESUITE:
        ref = psc.parse_reference(row.get("body_text"))
        return ref.session_provider if ref is not None else None
    if method == METHOD_SMARTBID:
        sb_ref = sbc.parse_reference(row.get("body_text"))
        return sb_ref.session_provider if sb_ref is not None else None
    return None


def can_harvest(row: dict, settings: Settings | None = None) -> tuple[bool, str | None]:
    """(possible, reason) for the router and the step: a harvester exists and
    the email carries a platform link (or, PipelineSuite, the portal host,
    Project ID and Security Key; SmartBid, the View the Project link with
    its passport key)."""
    s = settings or get_settings()
    method = row.get("invitation_method")
    if method == METHOD_GC_PORTAL:
        if gc_portal_scraper_for(row) is None:
            domain = address_domain(row.get("from_address")) or "unknown domain"
            return False, _MSG_NO_GC_PORTAL_SCRAPER.format(domain=domain)
        if not (s.rfp_ingest_enabled and s.rfp_harvest_enabled):
            return False, _MSG_NO_HARVESTER
        return True, None
    if method == METHOD_PIPELINESUITE:
        if not (s.rfp_ingest_enabled and s.rfp_harvest_enabled and s.pipelinesuite_enabled):
            return False, _MSG_NO_HARVESTER
        if reference_for(row) is None:
            return False, _MSG_NO_PS_REFERENCE
        return True, None
    if method == METHOD_SMARTBID:
        if not (s.rfp_ingest_enabled and s.rfp_harvest_enabled and s.smartbid_enabled):
            return False, _MSG_NO_HARVESTER
        if reference_for(row) is None:
            return False, _MSG_NO_SB_REFERENCE
        return True, None
    if method in ef.EMAIL_METHODS:
        if not (s.rfp_ingest_enabled and s.rfp_harvest_enabled and s.rfp_harvest_email_enabled):
            return False, _MSG_NO_HARVESTER
        if reference_for(row) is None:
            return False, _MSG_NO_EMAIL_FILES
        return True, None
    if method != METHOD_PROCORE:
        return False, _MSG_NO_HARVESTER
    if not (s.rfp_ingest_enabled and s.rfp_harvest_enabled and s.procore_configured):
        return False, _MSG_NOT_CONFIGURED
    if reference_for(row) is None:
        return False, _MSG_NO_LINK
    return True, None


# ── Session store (rfp_harvest_sessions) ─────────────────────────────────────


def _is_pipelinesuite_provider(provider: str) -> bool:
    return provider.startswith(psc.SESSION_PROVIDER_PREFIX)


class _SessionStore:
    """One rfp_harvest_sessions row: the shared Procore session (provider
    `procore`, RFP_HARVEST.md 3.2), one PipelineSuite portal's session
    (provider `pipelinesuite:<host>`, RFP_PIPELINESUITE.md 3.2) or the
    SmartBid login bookkeeping (provider `smartbid`, RFP_SMARTBID.md 3.2:
    no cookies, nothing secret). The failure cap and the lock length come
    from the matching settings block."""

    def __init__(self, settings: Settings, provider: str = pc.PROVIDER) -> None:
        self.s = settings
        self.provider = provider
        self._last_touch = 0.0

    @property
    def max_failures(self) -> int:
        if _is_pipelinesuite_provider(self.provider):
            return self.s.pipelinesuite_login_max_failures
        if self.provider == sbc.SESSION_PROVIDER:
            return self.s.smartbid_login_max_failures
        return self.s.procore_login_max_failures

    @property
    def lock_seconds(self) -> int:
        if _is_pipelinesuite_provider(self.provider):
            return self.s.pipelinesuite_login_lock_seconds
        if self.provider == sbc.SESSION_PROVIDER:
            return self.s.smartbid_login_lock_seconds
        return self.s.procore_login_lock_seconds

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

    def record_login(self, *, ok: bool, error: str | None) -> dict:
        state = self.load() or {}
        now = _now()
        row: dict = {"provider": self.provider, "last_login_attempt_at": _iso(now)}
        if ok:
            row.update(login_failures=0, locked_until=None, last_error=None)
        else:
            failures = int(state.get("login_failures") or 0) + 1
            row.update(login_failures=failures, last_error=(error or "")[:_ERROR_MAX_CHARS])
            if failures >= self.max_failures:
                row["locked_until"] = _iso(now + timedelta(seconds=self.lock_seconds))
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
            logger.debug("rfp harvest: session touch failed", exc_info=True)


def _notify_lock(
    until: datetime, error: str, portal: str | None = None, *, platform: str | None = None
) -> None:
    """One bell to every IT Admin when logins lock; deduped while an unread
    one exists. `portal` is the PipelineSuite host (the Procore session
    passes none): the text names it. `platform="smartbid"` is the SmartBid
    sentence (its session passes no portal; `_notify_smartbid_lock`)."""
    sb = get_supabase()
    pending = (
        sb.table("notifications")
        .select("id")
        .eq("type", NOTIFY_LOGIN_FAILED)
        .is_("read_at", "null")
        .is_("dismissed_at", "null")
        .limit(1)
        .execute()
    ).data
    if pending:
        return
    when = until.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if platform == sbc.PROVIDER:
        message = (
            f"SmartBid logins failed repeatedly; SmartBid RFP harvests are paused until {when}. "
            "Each login uses the project link in the invitation email."
        )
    elif portal:
        message = (
            f"PipelineSuite login ({portal}) failed repeatedly; RFP harvests for that portal "
            f"are paused until {when}. The Project ID and Security Key come from the "
            "invitation email."
        )
    else:
        message = (
            f"Procore login failed repeatedly; RFP harvests are paused until {when}. "
            "Check PROCORE_LOGIN_EMAIL and PROCORE_LOGIN_PASSWORD."
        )
    metadata: dict = {"locked_until": _iso(until), "error": error[:_ERROR_MAX_CHARS]}
    if platform == sbc.PROVIDER:
        metadata["platform"] = sbc.PROVIDER
    elif portal:
        metadata["portal"] = portal
    notify_role(
        Role.IT_ADMIN,
        None,
        NOTIFY_LOGIN_FAILED,
        message,
        mirror_email=False,
        metadata=metadata,
    )


def procore_config(settings: Settings) -> pc.ProcoreConfig:
    return pc.ProcoreConfig(
        email=settings.procore_login_email.strip(),
        password=settings.procore_login_password,
        min_request_interval=settings.procore_min_request_interval_seconds,
        login_min_interval=settings.procore_login_min_interval_seconds,
        timeout=settings.procore_request_timeout_seconds,
    )


def open_session(settings: Settings | None = None) -> pc.ProcoreSession:
    s = settings or get_settings()
    return pc.ProcoreSession(procore_config(s), _SessionStore(s), on_lock=_notify_lock)


def pipelinesuite_config(settings: Settings, ref: psc.PipelineSuiteRef) -> psc.PipelineSuiteConfig:
    """The portal session config: the key's fingerprint as the account, never
    the key (the session takes the key from `ref` at login time)."""
    return psc.PipelineSuiteConfig(
        account=ref.fingerprint,
        min_request_interval=settings.pipelinesuite_min_request_interval_seconds,
        login_min_interval=settings.pipelinesuite_login_min_interval_seconds,
        timeout=settings.pipelinesuite_request_timeout_seconds,
    )


def open_pipelinesuite_session(
    settings: Settings | None, ref: psc.PipelineSuiteRef
) -> psc.PipelineSuiteSession:
    """One portal's session (store row `pipelinesuite:<host>`), the lock bell
    naming the portal."""
    s = settings or get_settings()
    return psc.PipelineSuiteSession(
        pipelinesuite_config(s, ref), _SessionStore(s, ref.session_provider), ref,
        on_lock=_notify_lock,
    )


def _notify_smartbid_lock(until: datetime, error: str, _portal: str | None = None) -> None:
    """The SmartBid session's `on_lock`: the shared bell, SmartBid's text."""
    _notify_lock(until, error, platform=sbc.PROVIDER)


def smartbid_config(settings: Settings) -> sbc.SmartBidConfig:
    return sbc.SmartBidConfig(
        min_request_interval=settings.smartbid_min_request_interval_seconds,
        login_min_interval=settings.smartbid_login_min_interval_seconds,
        timeout=settings.smartbid_request_timeout_seconds,
    )


def open_smartbid_session(settings: Settings | None, ref: sbc.SmartBidRef) -> sbc.SmartBidSession:
    """One harvest's SmartBid session (store row `smartbid`: bookkeeping
    only), the lock bell in SmartBid's words. The session takes the passport
    key from `ref` at login time and keeps the token in memory."""
    s = settings or get_settings()
    return sbc.SmartBidSession(
        smartbid_config(s), _SessionStore(s, ref.session_provider), ref,
        on_lock=_notify_smartbid_lock,
    )


def _locked_reason(provider: str) -> str:
    if _is_pipelinesuite_provider(provider):
        return _MSG_PS_LOCKED.format(host=provider[len(psc.SESSION_PROVIDER_PREFIX):])
    if provider == sbc.SESSION_PROVIDER:
        return _MSG_SB_LOCKED
    return _MSG_PROCORE_LOCKED


def availability(
    settings: Settings | None = None, provider: str = pc.PROVIDER
) -> tuple[bool, str | None, datetime | None]:
    """(usable, reason, locked_until) for one session provider without
    touching the platform: `procore` (the default, so older callers keep
    working), `pipelinesuite:<host>` or `smartbid`."""
    s = settings or get_settings()
    if provider == pc.PROVIDER:
        if not s.procore_configured:
            return False, _MSG_NOT_CONFIGURED, None
    elif _is_pipelinesuite_provider(provider):
        if not s.pipelinesuite_enabled:
            return False, _MSG_NO_HARVESTER, None
    elif provider == sbc.SESSION_PROVIDER:
        if not s.smartbid_enabled:
            return False, _MSG_NO_HARVESTER, None
    else:
        return False, _MSG_NO_HARVESTER, None
    state = _SessionStore(s, provider).load()
    until = pc.lock_state(state, _now())
    if until:
        return False, _locked_reason(provider), until
    return True, None, None


def availability_for(
    row: dict, settings: Settings | None = None
) -> tuple[bool, str | None, datetime | None]:
    """`availability` for this email's own session provider: the router's
    lock check (503 rfp_harvest_locked) and the step's park use it. A method
    with no login session (gc_portal) is always usable here; a PipelineSuite
    or SmartBid row without a reference is not (the sentence names what is
    missing)."""
    s = settings or get_settings()
    method = row.get("invitation_method")
    if method == METHOD_PROCORE:
        return availability(s, pc.PROVIDER)
    if method in (METHOD_PIPELINESUITE, METHOD_SMARTBID):
        provider = session_provider_for(row, s)
        if provider is None:
            return False, _no_reference_message(method), None
        return availability(s, provider)
    return True, None, None


def _session_rows() -> list[dict]:
    try:
        return get_supabase().table(SESSIONS_TABLE).select("*").execute().data or []
    except Exception:  # noqa: BLE001 - the settings tab degrades to "nothing stored"
        logger.debug("rfp harvest: session rows failed", exc_info=True)
        return []


def _portal_status(row: dict, now: datetime) -> dict:
    """One PipelineSuite portal for the settings tab: the host, the key
    fingerprint as `account`, never the cookies."""
    until = pc.lock_state(row, now)
    return {
        "host": (row.get("provider") or "")[len(psc.SESSION_PROVIDER_PREFIX):],
        "account": row.get("account"),
        "logged_in_at": row.get("logged_in_at"),
        "last_used_at": row.get("last_used_at"),
        "last_login_attempt_at": row.get("last_login_attempt_at"),
        "login_failures": int(row.get("login_failures") or 0),
        "locked_until": _iso(until) if until else None,
        "last_error": row.get("last_error"),
    }


def _smartbid_status(row: dict, settings: Settings, now: datetime) -> dict:
    """The SmartBid block for the settings tab: login bookkeeping only (no
    account, no cookies; there is no key or token stored to show)."""
    until = pc.lock_state(row, now)
    return {
        "enabled": bool(
            settings.rfp_ingest_enabled and settings.rfp_harvest_enabled and settings.smartbid_enabled
        ),
        "logged_in_at": row.get("logged_in_at"),
        "last_used_at": row.get("last_used_at"),
        "last_login_attempt_at": row.get("last_login_attempt_at"),
        "login_failures": int(row.get("login_failures") or 0),
        "locked_until": _iso(until) if until else None,
        "last_error": row.get("last_error"),
    }


def session_status(settings: Settings | None = None) -> dict:
    """For the settings tab: never the cookies, never the password, never a
    Security Key (the PipelineSuite `account` is its fingerprint), never a
    SmartBid passport key or token (none is stored)."""
    s = settings or get_settings()
    rows = _session_rows()
    now = _now()
    state = next((r for r in rows if r.get("provider") == pc.PROVIDER), None) or {}
    smartbid_row = next((r for r in rows if r.get("provider") == sbc.SESSION_PROVIDER), None) or {}
    portals = sorted(
        (
            _portal_status(r, now)
            for r in rows
            if _is_pipelinesuite_provider(str(r.get("provider") or ""))
        ),
        key=lambda p: p["host"],
    )
    active = 0
    try:
        jobs = (
            get_supabase()
            .table("llm_jobs")
            .select("id", count="exact")
            .eq("job_type", JOB_TYPE)
            .in_("status", ["queued", "running"])
            .execute()
        )
        active = jobs.count if jobs.count is not None else len(jobs.data or [])
    except Exception:  # noqa: BLE001
        logger.debug("rfp harvest: active job count failed", exc_info=True)
    until = pc.lock_state(state, now)
    return {
        "enabled": bool(s.rfp_ingest_enabled and s.rfp_harvest_enabled),
        "configured": s.procore_configured,
        "account": s.procore_login_email.strip() or None,
        "logged_in_at": state.get("logged_in_at"),
        "last_used_at": state.get("last_used_at"),
        "last_login_attempt_at": state.get("last_login_attempt_at"),
        "login_failures": int(state.get("login_failures") or 0),
        "locked_until": _iso(until) if until else None,
        "last_error": state.get("last_error"),
        "active_jobs": active,
        "pipelinesuite": {
            "enabled": bool(
                s.rfp_ingest_enabled and s.rfp_harvest_enabled and s.pipelinesuite_enabled
            ),
            "portals": portals,
        },
        "smartbid": _smartbid_status(smartbid_row, s, now),
    }


# ── Queue adapter ────────────────────────────────────────────────────────────


def enqueue(
    email_id: str, *, created_by: str | None, force: bool = False, settings: Settings | None = None,
    test_session_id: str | None = None,
) -> dict:
    """One job per email row; `raise_on_active` so the router can answer 409.
    Payload carries `force` (a lease requeue keeps refreshing) and, for a
    test row, the session tag (docs/RFP_TESTING.md 4.4)."""
    s = settings or get_settings()
    payload = {"email_id": email_id, "force": bool(force)}
    if test_session_id:
        payload["test_session_id"] = test_session_id
    return llm_queue.enqueue(
        JOB_TYPE,
        target_id=email_id,
        payload=payload,
        created_by=created_by,
        priority=s.rfp_harvest_queue_priority,
        settings=s,
        raise_on_active=True,
    )


def active_job(email_id: str) -> dict | None:
    return llm_queue.active_job(JOB_TYPE, email_id)


def current_status(email_id: str) -> str | None:
    """For the queue: the harvest row behind the email, every terminal
    harvest status as 'done' (so requeue_terminal refuses; the detail
    screen's Harvest again is the retry path)."""
    email = _load_email(get_supabase(), email_id, "id, harvest_id")
    if email is None:
        return None
    if not email.get("harvest_id"):
        return STATUS_PENDING
    row = _load_harvest(get_supabase(), email["harvest_id"])
    if row is None:
        return STATUS_PENDING
    return "done" if row.get("status") in (STATUS_COMPLETE, STATUS_FAILED) else row.get("status")


def mark_from_queue(email_id: str, status: str, error: str | None) -> None:
    """The queue's domain marks after a job attempt ends: 'pending' when a
    retry is scheduled (nothing to do; the harvest row is already back at
    pending), 'failed' when the ladder is exhausted (the permanent-failure
    writes, fenced so a job that finished normally is not overwritten)."""
    if status != STATUS_FAILED:
        return
    sb = get_supabase()
    email = _load_email(
        sb, email_id, "id, status, harvest_id, invitation_method, body_text, attachments_meta"
    )
    if email is None:
        return
    message = (error or _MSG_INTERRUPTED)[:_ERROR_MAX_CHARS]
    harvest_id = email.get("harvest_id")
    if not harvest_id:
        # Pipeline mode links the email only when the harvest completes, so
        # a job that died on the ladder has to reach the platform object's
        # row the same way the detail screen does: by external_key.
        method = email.get("invitation_method")
        ref = reference_for(email)
        row = _find_harvest(sb, method, ref.external_key) if ref is not None else None
        harvest_id = row["id"] if row else None
    if harvest_id:
        sb.table(TABLE).update(
            {"status": STATUS_FAILED, "last_error": message, "finished_at": _iso(_now()),
             "claim_token": None}
        ).eq("id", harvest_id).in_("status", [STATUS_PENDING, STATUS_RUNNING]).execute()
    if email.get("status") == "harvest":
        _finish_email(sb, email_id, harvest_id, error=message)


def error_message(exc: Exception, _label: str) -> str:
    """The sentence the queue stores for a failed harvest job: the service's
    own exceptions carry user sentences; anything else is app-authored."""
    if isinstance(
        exc,
        (
            RfpHarvestTransient, RfpHarvestPermanent, pc.ProcoreError, psc.PipelineSuiteError,
            sbc.SmartBidError, cloud_folders.CloudError,
        ),
    ):
        return str(exc) or _MSG_INTERRUPTED
    return _MSG_INTERRUPTED


# ── Row helpers ──────────────────────────────────────────────────────────────

_EMAIL_SELECT = (
    "id, status, invitation_method, from_address, body_text, harvest_id, harvested_at, "
    "flag_reason, attempts, last_error, next_attempt_at, sibling_of_email_id, primary_mailbox, "
    "attachments_meta, subject, "
    # The test bench's tag and the unwrap record (docs/RFP_TESTING.md 4.4, 5.2).
    "test_session_id, forward_meta"
)


def _load_email(sb, email_id: str, columns: str = _EMAIL_SELECT) -> dict | None:
    rows = sb.table(EMAILS_TABLE).select(columns).eq("id", email_id).limit(1).execute().data or []
    return rows[0] if rows else None


def _load_harvest(sb, harvest_id: str) -> dict | None:
    rows = sb.table(TABLE).select("*").eq("id", harvest_id).limit(1).execute().data or []
    return rows[0] if rows else None


def _find_harvest(sb, method: str, key: str) -> dict | None:
    rows = (
        sb.table(TABLE).select("*").eq("method", method).eq("external_key", key).limit(1).execute()
    ).data or []
    return rows[0] if rows else None


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc)
    return "23505" in text or "duplicate key" in text


def _find_or_create_harvest(
    sb, email_id: str, method: str, ref: AnyRef, *, test_session_id: str | None = None
) -> dict:
    row = _find_harvest(sb, method, ref.external_key)
    if row:
        return row
    new_row = {
        "rfp_email_id": email_id,
        "method": method,
        "external_key": ref.external_key,
        "external_url": ref.external_url,
        "status": STATUS_PENDING,
    }
    if test_session_id:
        new_row["test_session_id"] = test_session_id
    try:
        return (
            sb.table(TABLE)
            .insert(new_row)
            .execute()
        ).data[0]
    except Exception as exc:  # noqa: BLE001 - only the unique race is expected
        if not _is_unique_violation(exc):
            raise
        row = _find_harvest(sb, method, ref.external_key)
        if row is None:
            raise
        return row


def _reusable(row: dict, settings: Settings) -> bool:
    if row.get("status") != STATUS_COMPLETE:
        return False
    finished = _parse_ts(row.get("finished_at"))
    if finished is None:
        return False
    return _now() - finished <= timedelta(days=settings.rfp_harvest_reuse_days)


def stale_claim_filter(settings: Settings, now: datetime) -> str:
    """The PostgREST `or` filter a harvest claim CASes through: a row at
    pending, failed or complete, OR one still `running` whose `started_at`
    is older than the queue lease. A worker that died mid-harvest leaves its
    row `running` under its claim token; once the queue lease it held has
    expired nothing can still be writing under that token (every write is
    fenced on it), so the next attempt takes the row over instead of parking
    behind the dead claim forever. Shared with the NGEM harvest."""
    cutoff = _iso(now - timedelta(seconds=settings.llm_queue_lease_seconds))
    return (
        f"status.in.({STATUS_PENDING},{STATUS_FAILED},{STATUS_COMPLETE}),"
        f"and(status.eq.{STATUS_RUNNING},started_at.lt.{cutoff})"
    )


def _claim_harvest(sb, harvest_id: str, token: str, attempts: int, settings: Settings) -> bool:
    now = _now()
    rows = (
        sb.table(TABLE)
        .update(
            {
                "status": STATUS_RUNNING,
                "claim_token": token,
                "attempts": attempts + 1,
                "started_at": _iso(now),
                "finished_at": None,
                "last_error": None,
            }
        )
        .eq("id", harvest_id)
        .or_(stale_claim_filter(settings, now))
        .execute()
    ).data or []
    return bool(rows)


def carry_over_accepted(entries: list[dict], prior: Any, *, key_fields: tuple[str, ...]) -> int:
    """Resume: the entries an interrupted attempt already put into the
    sandbox run (status accepted with a sandbox_file_id) keep their file id
    and status on the fresh entry list, matched by `key_fields` (the name and
    the declared size), so `_harvest_files` skips them instead of downloading
    them again. Only meaningful when the run they went into is reused.
    Returns how many were carried over."""
    done: dict[tuple, dict] = {}
    for old in prior or []:
        if not isinstance(old, dict):
            continue
        if old.get("status") != FILE_ACCEPTED or not old.get("sandbox_file_id"):
            continue
        done[tuple(old.get(k) for k in key_fields)] = old
    carried = 0
    for entry in entries:
        old = done.get(tuple(entry.get(k) for k in key_fields))
        if old is None:
            continue
        entry["sandbox_file_id"] = old["sandbox_file_id"]
        entry["status"] = FILE_ACCEPTED
        entry["error"] = None
        carried += 1
    return carried


def _update_claimed(sb, harvest_id: str, token: str, fields: dict) -> None:
    rows = (
        sb.table(TABLE)
        .update(fields)
        .eq("id", harvest_id)
        .eq("claim_token", token)
        .eq("status", STATUS_RUNNING)
        .execute()
    ).data or []
    if not rows:
        raise _ClaimLost()


def _finish_email(sb, email_id: str, harvest_id: str | None, *, error: str | None) -> bool:
    """Pipeline mode: harvest -> split, the link and the stamp. Success and
    permanent failure alike land at `split` (docs/RFP_SPLIT.md 3: the split
    step stages the harvested documents into the Bid File Splitter, or
    falls straight through to `create` when the flags are off or nothing
    was harvested; a failed harvest still creates, flagged "missing
    files"; with automatic creation off the create step drains the row to
    `done`). `error` keeps the failure sentence on the row (the card shows
    the harvest's own)."""
    fields = {
        "status": "split",
        "harvest_id": harvest_id,
        "harvested_at": _iso(_now()),
        "attempts": 0,
        "last_error": error,
        "next_attempt_at": None,
        "updated_at": _iso(_now()),
    }
    rows = (
        sb.table(EMAILS_TABLE).update(fields).eq("id", email_id).eq("status", "harvest").execute()
    ).data or []
    return bool(rows)


def _link_email(sb, email: dict, harvest_id: str) -> None:
    """Manual mode: the link and the stamp only, status untouched (fenced on
    the status we read, so a row that moved is left alone)."""
    sb.table(EMAILS_TABLE).update(
        {"harvest_id": harvest_id, "harvested_at": _iso(_now()), "updated_at": _iso(_now())}
    ).eq("id", email["id"]).eq("status", email["status"]).execute()


def _park_email(sb, email: dict, *, seconds: float, error: str | None) -> None:
    """Pipeline mode wait: next_attempt_at pushed, no attempt spent."""
    if email.get("status") != "harvest":
        return
    sb.table(EMAILS_TABLE).update(
        {
            "next_attempt_at": _iso(_now() + timedelta(seconds=max(5.0, seconds))),
            "last_error": error,
            "updated_at": _iso(_now()),
        }
    ).eq("id", email["id"]).eq("status", "harvest").execute()


def _renew() -> None:
    if not llm_queue.renew_lease():
        raise RfpHarvestTransient(_MSG_INTERRUPTED)


# ── The job ──────────────────────────────────────────────────────────────────


def _scratch_dir(settings: Settings) -> Path:
    base = settings.rfp_ingest_scratch_dir or tempfile.gettempdir()
    root = Path(base) / "rfp-harvest"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="rfp-harvest-", dir=root))


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _download_one(session: Any, url: Any, dest: Path, *, max_bytes: int) -> bytes:
    """Up to _DOWNLOAD_ATTEMPTS paced tries on transient trouble; the bytes
    are read back once and the scratch file removed. `session` is any
    platform session exposing `download(locator, dest, max_bytes)` and
    raising one of the client families; `url` is that session's locator (a
    URL, or for SmartBid the parsed plan-room entry)."""
    last: Exception | None = None
    for attempt in range(_DOWNLOAD_ATTEMPTS):
        _unlink_quietly(dest)
        try:
            session.download(url, dest, max_bytes=max_bytes)
            data = dest.read_bytes()
            _unlink_quietly(dest)
            return data
        except _TRANSIENT_ERRORS as exc:
            last = exc
            if attempt + 1 < _DOWNLOAD_ATTEMPTS:
                time.sleep(2.0 * (attempt + 1))
    _unlink_quietly(dest)
    raise last or RfpHarvestTransient("The file host did not answer.")


def _existing_run(sb, run_id: str | None) -> dict | None:
    if not run_id:
        return None
    rows = (
        sb.table(rfp_ingest.RUNS_TABLE).select("id, status").eq("id", run_id).limit(1).execute()
    ).data or []
    return rows[0] if rows else None


def _harvest_files(
    sb,
    session: Any,
    settings: Settings,
    harvest: dict,
    token: str,
    email_id: str,
    entries: list[dict],
    urls: list[Any],
    *,
    prior_files: Any = None,
) -> tuple[str | None, int, int]:
    """Download every manifest row into one sandbox run. Returns (run_id,
    accepted, bytes). Raises _ClaimLost and RfpHarvestTransient; per-file
    trouble is recorded on the entry. Provider-neutral: `session` is any
    platform session exposing `provider` (the sandbox source kind) and
    `download(locator, dest, max_bytes)` raising the Procore, PipelineSuite,
    SmartBid or cloud_folders Transient / Forbidden pair; `urls` holds the
    aligned locators (URLs, or SmartBid's parsed plan-room entries), in
    memory only. The filename handed to the sandbox is the entry's basename,
    extension kept. `prior_files` is the harvest row's file list from before
    this attempt (the facts write replaces it), read only when the run it
    went into is reused. Entries settled before the loop (`reused`: an
    earlier harvest's file, its sandbox_file_id already set; `expanded`: an
    opened zip whose members follow it; `skipped`: a SmartBid file behind an
    agreement) are skipped and never counted as accepted."""
    if not entries:
        return None, 0, 0
    run = _existing_run(sb, harvest.get("sandbox_run_id"))
    if run is not None and run.get("status") in ("staging", "pending"):
        # An interrupted attempt's run: the files it already accepted stay
        # accepted (matched by path and size) and are not downloaded twice.
        carried = carry_over_accepted(entries, prior_files, key_fields=("file_path", "size"))
        if carried:
            logger.info(
                "rfp harvest: resuming %s on run %s past %s accepted file(s)",
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
        # The tag rides along only for a test session's harvest, so the
        # normal call is exactly what it was (docs/RFP_TESTING.md 4.4).
        tag = {"test_session_id": harvest["test_session_id"]} if harvest.get("test_session_id") else {}
        run = rfp_ingest.create_harvest_run(rfp_email_id=email_id, harvest_id=harvest["id"], **tag)
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
            if entry.get("status") in _SETTLED_BEFORE_DOWNLOAD:
                continue
            if not url:
                entry["status"] = FILE_DOWNLOAD_FAILED
                entry["error"] = _MSG_NO_DOWNLOAD_LINK
                continue
            if int(entry.get("size") or 0) > settings.rfp_ingest_max_file_bytes:
                entry["status"] = FILE_TOO_LARGE
                entry["error"] = "Larger than the sandbox accepts."
                continue
            dest = scratch / f"{index:04d}.bin"
            try:
                data = _download_one(session, url, dest, max_bytes=settings.rfp_ingest_max_file_bytes)
            except _FORBIDDEN_ERRORS as exc:
                entry["status"] = FILE_TOO_LARGE if "larger" in str(exc) else FILE_DOWNLOAD_FAILED
                entry["error"] = _error(exc)
                continue
            except _TRANSIENT_ERRORS as exc:
                entry["status"] = FILE_DOWNLOAD_FAILED
                entry["error"] = _error(exc)
                continue
            filename = entry["file_path"].rsplit("/", 1)[-1].strip() or "document.pdf"
            source = {
                "kind": session.provider,
                "file_path": entry["file_path"],
                "harvest_id": harvest["id"],
            }
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
                    raise RfpHarvestTransient(_error(exc)) from exc
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
            _record_file(sb, harvest, email_id, entry)
            # Progress lands after every file so a retry resumes past it.
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
            logger.warning("rfp harvest: could not delete the empty run %s", run_id)
        _update_claimed(sb, harvest["id"], token, {"sandbox_run_id": None})
        return None, 0, 0
    _renew()
    rfp_ingest.start_run(run_id)
    rfp_ingest.dispatch(run_id, created_by=None, background=None)
    return run_id, accepted, total


def _record_file(sb, harvest: dict, email_id: str, entry: dict) -> None:
    """Test rows: the `harvest.file` event per manifest row once its
    download settled (docs/RFP_TESTING.md 7.2). The sandbox verdict lands
    later, on the run's file row; what is known here is the outcome of the
    download and the sandbox's immediate answer."""
    if not rfp_test.session_for_email(harvest):
        return
    rfp_test.record(
        sb, session_id=harvest["test_session_id"], source=rfp_test.SOURCE_HARVEST, kind="file",
        level=rfp_test.LEVEL_INFO if entry.get("status") == FILE_ACCEPTED else rfp_test.LEVEL_WARN,
        title=f"File {entry.get('status')}: {entry.get('file_path')}",
        rfp_email_id=email_id, harvest_id=harvest.get("id"),
        detail={
            "name": entry.get("file_path"), "source": entry.get("origin") or entry.get("kind") or "platform",
            "provider": entry.get("provider"), "size": entry.get("size"), "status": entry.get("status"),
            "error": entry.get("error"), "sandbox_file_id": entry.get("sandbox_file_id"),
            "zip_of": entry.get("zip_of"), "link_key": entry.get("link_key"),
        },
    )


def _fetch_facts(session: pc.ProcoreSession, ref: pc.ProcoreRef) -> tuple[dict, dict, dict, dict, str]:
    package_id = session.resolve_bid_sheet(ref)
    referer = ref.bid_sheet_url
    bid_package = session.get_json(
        f"/rest/v1.0/companies/{ref.company_id}/bid_packages/{package_id}", referer=referer
    )
    bid = session.get_json(
        f"/rest/v1.0/companies/{ref.company_id}/bids/{ref.bid_id}",
        params={"view": "planroom_redesign"},
        referer=referer,
    )
    bid_form: dict = {}
    form_id = bid.get("bid_form_id") if isinstance(bid, dict) else None
    if form_id and re.fullmatch(r"\d{1,12}", str(form_id)):
        try:
            bid_form = session.get_json(
                f"/rest/v1.0/companies/{ref.company_id}/bid/{ref.bid_id}/bid_forms/{form_id}",
                referer=referer,
            )
        except pc.ProcoreForbidden:
            bid_form = {}
    docs = session.get_json(
        f"/rest/v1.0/companies/{ref.company_id}/planroom/bid_packages/{package_id}/documents",
        referer=referer,
    )
    return bid_package, bid, bid_form, docs, package_id


def _complete_harvest(
    sb, harvest: dict, token: str, entries: list[dict], run_id: str | None,
    accepted: int, total: int, *, no_files_message: str,
) -> None:
    _update_claimed(
        sb, harvest["id"], token,
        {
            "status": STATUS_COMPLETE,
            "sandbox_run_id": run_id,
            "files": entries,
            "files_accepted": accepted,
            "bytes_downloaded": total,
            "finished_at": _iso(_now()),
            "last_error": None if entries else no_files_message,
            "claim_token": None,
        },
    )


def _check_caps(
    settings: Settings, entries: list[dict], *, files_message: str, bytes_message: str
) -> None:
    """Over either cap is permanent; the facts are already written."""
    if len(entries) > settings.rfp_harvest_file_cap:
        raise RfpHarvestPermanent(
            files_message.format(n=len(entries), cap=settings.rfp_harvest_file_cap)
        )
    declared = sum(int(e.get("size") or 0) for e in entries)
    if declared > settings.rfp_harvest_max_total_bytes:
        raise RfpHarvestPermanent(
            bytes_message.format(
                mb=declared // (1024 * 1024),
                cap=settings.rfp_harvest_max_total_bytes // (1024 * 1024),
            )
        )


def _harvest_procore(
    sb, settings: Settings, email: dict, ref: pc.ProcoreRef, harvest: dict, token: str
) -> None:
    """The Procore body of the job (RFP_HARVEST.md 2.2 steps 5 to 7), run
    under the claim by `_run_claimed`."""
    with open_session(settings) as session:
        usable, reason, until = session.availability()
        if not usable:
            raise pc.ProcoreUnavailable(reason or _MSG_NOT_CONFIGURED, locked_until=until)
        _renew()
        bid_package, bid, bid_form, docs, package_id = _fetch_facts(session, ref)
        entries = classify_manifest(docs)
        urls = manifest_urls(docs)
        ref = pc.ProcoreRef(ref.company_id, ref.bid_id, package_id=package_id, project_id=ref.project_id)
        data = normalize_facts(ref, bid_package, bid, bid_form, entries)
        facts = {
            "external_url": ref.bid_sheet_url,
            "data": data,
            "raw": build_raw(bid_package, bid, bid_form, docs),
            "description_text": html_to_text(
                bid_package.get("bid_email_message") if isinstance(bid_package, dict) else None
            ),
            "instructions_text": html_to_text(
                bid_package.get("bid_web_message") if isinstance(bid_package, dict) else None
            ),
            "files": entries,
            "file_count": len(entries),
            "facts_at": _iso(_now()),
        }
        prior_files = list(harvest.get("files") or [])
        _update_claimed(sb, harvest["id"], token, facts)
        harvest.update(facts)
        _check_caps(
            settings, entries, files_message=_MSG_TOO_MANY_FILES, bytes_message=_MSG_TOO_MANY_BYTES
        )
        run_id, accepted, total = _harvest_files(
            sb, session, settings, harvest, token, email["id"], entries, urls,
            prior_files=prior_files,
        )
        _complete_harvest(
            sb, harvest, token, entries, run_id, accepted, total, no_files_message=_MSG_NO_FILES
        )


# ── PipelineSuite: the tracking pings (RFP_PIPELINESUITE.md 4, step 3) ───────


def _email_html(sb, email: dict) -> str | None:
    """The email's HTML body through Graph, from the sighting in its primary
    mailbox, else any sighting. None when no copy answers."""
    sightings = (
        sb.table("rfp_email_sightings")
        .select("mailbox, graph_message_id")
        .eq("rfp_email_id", email["id"])
        .order("created_at", desc=False)
        .execute()
    ).data or []
    # Compared case-insensitively: sightings written before 0134 normalized
    # them may carry the mailbox in whatever case Graph reported.
    primary = (email.get("primary_mailbox") or "").strip().lower()
    ordered = sorted(
        sightings,
        key=lambda s: 0 if (s.get("mailbox") or "").strip().lower() == primary else 1,
    )
    for sighting in ordered:
        try:
            full = graph_inbox.get_message(
                sighting["graph_message_id"],
                mailbox=sighting["mailbox"],
                select="id,body",
                body_type="html",
            )
        except Exception:  # noqa: BLE001 - the next sighting, then the text fallback
            logger.debug("rfp harvest: email html fetch failed", exc_info=True)
            continue
        body = ((full or {}).get("body") or {}).get("content") if isinstance(full, dict) else None
        if body:
            return str(body)
    return None


def _ping_once(
    session: Any, url: str, label: str, problems: list[str], ok: tuple[int, ...] = (200, 302)
) -> bool:
    """One tracker GET; True when it answered one of `ok` (200 or 302 by
    default). Never raises."""
    try:
        status = session.ping(url)
    except Exception as exc:  # noqa: BLE001 - a ping never fails the harvest
        logger.warning("rfp harvest: %s ping failed (%s)", label, type(exc).__name__)
        problems.append(f"{label}: {type(exc).__name__}")
        return False
    if status in ok:
        return True
    problems.append(f"{label}: no answer" if status is None else f"{label}: HTTP {status}")
    return False


def _ping_email_tracking(sb, email: dict, harvest: dict, settings: Settings, session: Any) -> dict | None:
    """Fire the email's own tracking once, the way a person reading it would:
    the SendGrid open pixel and the "View Files and Project Details" click
    link, each a single GET with no redirect followed (the Yes / No / Unsure
    links are never requested). Returns the `data.tracking` record, or None
    when nothing was pinged (the setting is off, or the harvest row already
    carries `tracking.pinged_at`). Nothing here raises."""
    try:
        if not settings.pipelinesuite_tracking_pings_enabled:
            return None
        data = harvest.get("data") if isinstance(harvest.get("data"), dict) else {}
        existing = data.get("tracking") if isinstance(data.get("tracking"), dict) else None
        if existing and existing.get("pinged_at"):
            return None
        problems: list[str] = []
        open_url: str | None = None
        click_url: str | None = None
        try:
            page_html = _email_html(sb, email)
        except Exception as exc:  # noqa: BLE001 - Graph trouble: the text fallback
            logger.warning("rfp harvest: email html lookup failed (%s)", type(exc).__name__)
            problems.append(f"email: {type(exc).__name__}")
            page_html = None
        if page_html:
            tracking = psc.parse_tracking(page_html)
            open_url, click_url = tracking.open_url, tracking.click_url
        if click_url is None:
            click_url = psc.parse_click_link(email.get("body_text"))
        if open_url is None and click_url is None:
            problems.append("no tracking links in the email")
        opened = _ping_once(session, open_url, "open", problems) if open_url else None
        clicked = _ping_once(session, click_url, "click", problems) if click_url else None
        return {
            "pinged_at": _iso(_now()),
            "opened": opened,
            "clicked": clicked,
            "error": ("; ".join(problems))[:_ERROR_MAX_CHARS] or None,
        }
    except Exception as exc:  # noqa: BLE001 - belt and braces: never out of the job
        logger.warning("rfp harvest: tracking pings failed (%s)", type(exc).__name__)
        return {
            "pinged_at": _iso(_now()),
            "opened": None,
            "clicked": None,
            "error": f"tracking: {type(exc).__name__}"[:_ERROR_MAX_CHARS],
        }


def _harvest_pipelinesuite(
    sb, settings: Settings, email: dict, ref: psc.PipelineSuiteRef, harvest: dict, token: str
) -> None:
    """The PipelineSuite body of the job (RFP_PIPELINESUITE.md section 4,
    steps 3 to 5): pings, availability, the project page (one login and one
    retry inside the client), facts first, caps, files, complete."""
    with open_pipelinesuite_session(settings, ref) as session:
        prior_data = harvest.get("data") if isinstance(harvest.get("data"), dict) else {}
        tracking = _ping_email_tracking(sb, email, harvest, settings, session)
        if tracking is not None:
            # Recorded before the portal is touched, so a parked or failed
            # run never pings again.
            merged = {**prior_data, "tracking": tracking}
            _update_claimed(sb, harvest["id"], token, {"data": merged})
            harvest["data"] = merged
        else:
            tracking = prior_data.get("tracking") if isinstance(prior_data.get("tracking"), dict) else None
        usable, reason, until = session.availability()
        if not usable:
            raise psc.PipelineSuiteUnavailable(reason or _MSG_NO_HARVESTER, locked_until=until)
        _renew()
        page = psc.parse_project_page(session.get_project_page())
        if not page.logged_in:
            raise psc.PipelineSuiteUnavailable(_MSG_PS_PAGE)
        entries, urls = pipelinesuite_files(page)
        data = normalize_pipelinesuite_facts(ref, page, entries, tracking)
        facts = {
            "external_url": ref.external_url,
            "data": data,
            "raw": build_pipelinesuite_raw(page),
            "description_text": pipelinesuite_description(page.info.get("scope")),
            "instructions_text": pipelinesuite_instructions(page.info),
            "files": entries,
            "file_count": len(entries),
            "facts_at": _iso(_now()),
        }
        prior_files = list(harvest.get("files") or [])
        _update_claimed(sb, harvest["id"], token, facts)
        harvest.update(facts)
        _check_caps(
            settings, entries,
            files_message=_MSG_PS_TOO_MANY_FILES, bytes_message=_MSG_PS_TOO_MANY_BYTES,
        )
        run_id, accepted, total = _harvest_files(
            sb, session, settings, harvest, token, email["id"], entries, urls,
            prior_files=prior_files,
        )
        _complete_harvest(
            sb, harvest, token, entries, run_id, accepted, total, no_files_message=_MSG_PS_NO_FILES
        )


# ── SmartBid: pings, downloads, the body (RFP_SMARTBID.md 4) ─────────────────


def _ping_smartbid_tracking(
    sb, email: dict, harvest: dict, settings: Settings, session: Any, ref: sbc.SmartBidRef
) -> dict | None:
    """Fire the email's own tracking once, the way a person reading it
    would: the SmartBid read receipt, the SendGrid open pixel and the "Click
    Here to View the Project" link (never an `iR` link), each a single GET
    with no redirect followed. When the email HTML cannot be had (or carries
    no View link) the click is the reference's own link and no pixel fires.
    Returns the `data.tracking` record (`opened`: either pixel answered 200;
    `clicked`: the link answered 200 or 302), or None when nothing was
    pinged (the setting is off, or the harvest row already carries
    `tracking.pinged_at`). Nothing here raises; no URL is recorded."""
    try:
        if not settings.smartbid_tracking_pings_enabled:
            return None
        data = harvest.get("data") if isinstance(harvest.get("data"), dict) else {}
        existing = data.get("tracking") if isinstance(data.get("tracking"), dict) else None
        if existing and existing.get("pinged_at"):
            return None
        problems: list[str] = []
        tracking = sbc.Tracking(None, None, None)
        try:
            page_html = _email_html(sb, email)
        except Exception as exc:  # noqa: BLE001 - Graph trouble: the reference's link
            logger.warning("rfp harvest: email html lookup failed (%s)", type(exc).__name__)
            problems.append(f"email: {type(exc).__name__}")
            page_html = None
        if page_html:
            tracking = sbc.parse_tracking(page_html)
        pixels = [
            (url, label)
            for url, label in ((tracking.read_receipt_url, "read receipt"), (tracking.open_url, "open"))
            if url
        ]
        click_url = tracking.click_url or ref.click_url
        opened: bool | None = None
        if pixels:
            results = [_ping_once(session, url, label, problems, ok=(200,)) for url, label in pixels]
            opened = any(results)
        clicked = _ping_once(session, click_url, "click", problems) if click_url else None
        return {
            "pinged_at": _iso(_now()),
            "opened": opened,
            "clicked": clicked,
            "error": ("; ".join(problems))[:_ERROR_MAX_CHARS] or None,
        }
    except Exception as exc:  # noqa: BLE001 - belt and braces: never out of the job
        logger.warning("rfp harvest: smartbid tracking pings failed (%s)", type(exc).__name__)
        return {
            "pinged_at": _iso(_now()),
            "opened": None,
            "clicked": None,
            "error": f"tracking: {type(exc).__name__}"[:_ERROR_MAX_CHARS],
        }


class _SmartBidDownloads:
    """What `_harvest_files` needs from a platform session (`provider` and
    `download(locator, dest, max_bytes)`) over one SmartBid session: the
    locator is the parsed plan-room entry (memory only), and a 401 on the
    security token or the direct-URL lookup logs in once more and retries
    that file once, at most."""

    provider = sbc.PROVIDER

    def __init__(self, session: Any) -> None:
        self.session = session

    def download(self, locator: Any, dest: Path, *, max_bytes: int) -> int:
        try:
            return self.session.download(locator, dest, max_bytes=max_bytes)
        except sbc.SmartBidSessionExpired:
            _unlink_quietly(dest)
            self.session.login()
            return self.session.download(locator, dest, max_bytes=max_bytes)


def _sb_with_relogin(session: Any, call: Callable[[], Any]) -> Any:
    """One project read; a 401 logs in once more and retries once (a second
    401 stays SmartBidSessionExpired: transient for the job)."""
    try:
        return call()
    except sbc.SmartBidSessionExpired:
        session.login()
        return call()


def _harvest_smartbid(
    sb, settings: Settings, email: dict, ref: sbc.SmartBidRef, harvest: dict, token: str
) -> None:
    """The SmartBid body of the job (RFP_SMARTBID.md section 4, steps 3 to
    5): pings, availability, the login, the agreement gate, the project
    (one re-login and retry on expiry), facts first, caps, files (restricted
    ones skipped), complete. The bearer token lives in the session only."""
    with open_smartbid_session(settings, ref) as session:
        prior_data = harvest.get("data") if isinstance(harvest.get("data"), dict) else {}
        tracking = _ping_smartbid_tracking(sb, email, harvest, settings, session, ref)
        if tracking is not None:
            # Recorded before the API is touched, so a parked or failed run
            # never pings again.
            merged = {**prior_data, "tracking": tracking}
            _update_claimed(sb, harvest["id"], token, {"data": merged})
            harvest["data"] = merged
        else:
            tracking = prior_data.get("tracking") if isinstance(prior_data.get("tracking"), dict) else None
        usable, reason, until = session.availability()
        if not usable:
            raise sbc.SmartBidUnavailable(reason or _MSG_SB_LOCKED, locked_until=until)
        _renew()
        session.login()
        _sb_with_relogin(session, session.gate)
        payload = _sb_with_relogin(session, session.get_project)
        project = sbc.parse_project(payload)
        entries, locators = smartbid_files(project)
        data = normalize_smartbid_facts(ref, project, entries, tracking)
        facts = {
            "external_url": ref.external_url,
            "data": data,
            "raw": build_smartbid_raw(payload, project),
            "description_text": html_to_text(project.description_html),
            "instructions_text": None,
            "files": entries,
            "file_count": len(entries),
            "facts_at": _iso(_now()),
        }
        prior_files = list(harvest.get("files") or [])
        _update_claimed(sb, harvest["id"], token, facts)
        harvest.update(facts)
        # The caps count what would be downloaded: restricted files never are.
        downloadable = [e for e in entries if e.get("status") != FILE_SKIPPED]
        _check_caps(
            settings, downloadable,
            files_message=_MSG_PS_TOO_MANY_FILES, bytes_message=_MSG_PS_TOO_MANY_BYTES,
        )
        run_id, accepted, total = None, 0, 0
        if downloadable:
            run_id, accepted, total = _harvest_files(
                sb, _SmartBidDownloads(session), settings, harvest, token, email["id"],
                entries, locators, prior_files=prior_files,
            )
        _complete_harvest(
            sb, harvest, token, entries, run_id, accepted, total, no_files_message=_MSG_PS_NO_FILES
        )


def _run_claimed(
    sb, settings: Settings, email: dict, harvest: dict, token: str, pipeline: bool,
    body: Callable[[], None], errors: tuple[type, type, type],
) -> None:
    """Run one platform body under the claim and map its failures (doc 2.2):
    `errors` = (unavailable, forbidden, base) for that platform's client."""
    unavailable, forbidden, base = errors
    email_id = email["id"]

    def outcome(kind: str, message: str | None, level: str = rfp_test.LEVEL_WARN) -> None:
        """Test rows: the harvest's exit as an event (docs/RFP_TESTING.md 7.2)."""
        if not rfp_test.session_for_email(email):
            return
        if kind == "finished":
            # The counts landed through `_update_claimed`, not on the dict.
            harvest.update(_load_harvest(sb, harvest["id"]) or {})
        rfp_test.record(
            sb, session_id=email["test_session_id"], source=rfp_test.SOURCE_HARVEST, kind=kind,
            level=level, title=f"Harvest {kind}" + (f": {message}" if message else ""),
            rfp_email_id=email_id, harvest_id=harvest.get("id"),
            detail={
                "status": harvest.get("status"), "error": message, "file_count": harvest.get("file_count"),
                "files_accepted": harvest.get("files_accepted"),
                "bytes_downloaded": harvest.get("bytes_downloaded"),
                "sandbox_run_id": harvest.get("sandbox_run_id"),
                "files": [
                    {"name": e.get("file_path"), "status": e.get("status"), "error": e.get("error")}
                    for e in (harvest.get("files") or []) if isinstance(e, dict)
                ][:200],
            },
        )

    try:
        body()
    except _ClaimLost:
        logger.warning("rfp harvest: claim lost on %s for email %s", harvest["id"], email_id)
        outcome("parked", _MSG_CLAIMED)
        if pipeline:
            _park_email(sb, email, seconds=settings.rfp_harvest_poll_seconds, error=_MSG_CLAIMED)
            return
        raise RfpHarvestTransient(_MSG_CLAIMED) from None
    except unavailable as exc:
        # Locked, unconfigured, an interstitial or an unexpected page:
        # nothing a retry fixes on its own. Pipeline rows wait (no attempt
        # spent); manual runs report it.
        outcome("parked" if pipeline else "failed", _error(exc))
        if pipeline:
            _release(sb, harvest["id"], token, STATUS_PENDING, _error(exc))
            wait = settings.rfp_harvest_poll_seconds
            if exc.locked_until:
                wait = max(wait, (exc.locked_until - _now()).total_seconds())
            _park_email(sb, email, seconds=wait, error=_error(exc))
            return
        _release(sb, harvest["id"], token, STATUS_FAILED, _error(exc))
        _link_email(sb, email, harvest["id"])
        raise RfpHarvestPermanent(_error(exc)) from exc
    except (forbidden, RfpHarvestPermanent) as exc:
        # Pipeline mode returns; manual mode raises from inside _permanent.
        outcome("failed", _error(exc), rfp_test.LEVEL_ERROR)
        _permanent(sb, email, harvest["id"], _error(exc), pipeline, token=token)
        return
    except (base, RfpHarvestTransient) as exc:
        # The client's transient, a session that stayed expired after the
        # one login, and the service's own transient: back to pending for
        # the queue's ladder; a manual run is linked so the card shows the row.
        outcome("retry", _error(exc))
        _release(sb, harvest["id"], token, STATUS_PENDING, _error(exc))
        if not pipeline:
            _link_email(sb, email, harvest["id"])
        raise RfpHarvestTransient(_error(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - storage, PostgREST: retry through the queue
        logger.exception("rfp harvest: unexpected failure on email %s", email_id)
        outcome("retry", _MSG_INTERRUPTED)
        _release(sb, harvest["id"], token, STATUS_PENDING, _MSG_INTERRUPTED)
        raise RfpHarvestTransient(_MSG_INTERRUPTED) from exc
    outcome("finished", None, rfp_test.LEVEL_INFO)
    _done(sb, email, harvest["id"], pipeline)


def execute(email_id: str, *, force: bool = False) -> None:
    """The rfp_harvest job (RFP_HARVEST.md 2.2; RFP_PIPELINESUITE.md 4;
    RFP_SMARTBID.md 4).
    Pipeline mode moves the email harvest -> done; manual mode (`force`, or a
    terminal row) only links it."""
    settings = get_settings()
    sb = get_supabase()
    email = _load_email(sb, email_id)
    if email is None:
        raise RfpHarvestPermanent("Email not found.", http_status=404)
    pipeline = email.get("status") == "harvest"
    if not pipeline and email.get("status") not in MANUAL_STATUSES:
        raise RfpHarvestPermanent("The email is not at a stage that can be harvested.")

    method = email.get("invitation_method")
    if harvester_for(email, settings) is None:
        if pipeline:
            _finish_email(sb, email_id, email.get("harvest_id"), error=None)
            return
        raise RfpHarvestPermanent(can_harvest(email, settings)[1] or _MSG_NO_HARVESTER)
    if method == METHOD_GC_PORTAL:
        # A scraper is registered for the sender but no runner is wired yet
        # (doc 2.3): fail the harvest visibly instead of walking a platform
        # path below. The scraper build replaces this branch with its call.
        _permanent(
            sb,
            email,
            None,
            _MSG_GC_PORTAL_NOT_WIRED.format(
                scraper=gc_portal_scraper_for(email),
                domain=address_domain(email.get("from_address")) or "unknown domain",
            ),
            pipeline,
        )
        return
    ref = reference_for(email)
    if ref is None:
        _permanent(sb, email, None, _no_reference_message(method), pipeline)
        return

    harvest = _find_or_create_harvest(
        sb, email_id, method, ref, test_session_id=email.get("test_session_id")
    )
    if rfp_test.session_for_email(email):
        # The row's tag rides in memory for the run and file events (a real
        # harvest row a test row reuses stays untagged in the table).
        harvest.setdefault("test_session_id", email["test_session_id"])
        harvester = harvester_for(email, settings)
        rfp_test.record(
            sb, session_id=email["test_session_id"], source=rfp_test.SOURCE_HARVEST, kind="started",
            title=f"Harvest started ({harvester}): {ref.external_key}",
            rfp_email_id=email_id, harvest_id=harvest.get("id"),
            detail={
                "harvester": harvester, "method": method, "platform_reference": ref.external_key,
                "external_url": ref.external_url, "force": bool(force), "pipeline": pipeline,
                "reusable": bool(_reusable(harvest, settings)),
                "credentials_present": bool(
                    settings.procore_configured if harvester == MODEL_LABEL else
                    getattr(ref, "security_key", None) if method == METHOD_PIPELINESUITE else
                    getattr(ref, "passport_key", None) if method == METHOD_SMARTBID else False
                ),
            },
        )
    if not force and _reusable(harvest, settings):
        if rfp_test.session_for_email(email):
            rfp_test.record(
                sb, session_id=email["test_session_id"], source=rfp_test.SOURCE_HARVEST, kind="finished",
                title="Harvest reused an earlier complete harvest", rfp_email_id=email_id,
                harvest_id=harvest.get("id"),
                detail={"reused": True, "file_count": harvest.get("file_count"),
                        "files_accepted": harvest.get("files_accepted")},
            )
        _done(sb, email, harvest["id"], pipeline)
        return

    token = uuid.uuid4().hex
    if not _claim_harvest(sb, harvest["id"], token, int(harvest.get("attempts") or 0), settings):
        logger.warning(
            "rfp harvest: lost the claim on %s for email %s (row %s since %s under another "
            "token); %s",
            harvest["id"], email_id, harvest.get("status"), harvest.get("started_at"),
            "parking the email" if pipeline else "reporting transient",
        )
        if pipeline:
            _park_email(sb, email, seconds=settings.rfp_harvest_poll_seconds, error=_MSG_CLAIMED)
            return
        raise RfpHarvestTransient(_MSG_CLAIMED)

    if method == METHOD_PIPELINESUITE:
        _run_claimed(
            sb, settings, email, harvest, token, pipeline,
            lambda: _harvest_pipelinesuite(sb, settings, email, ref, harvest, token),
            _PIPELINESUITE_ERRORS,
        )
        return
    if method == METHOD_SMARTBID:
        _run_claimed(
            sb, settings, email, harvest, token, pipeline,
            lambda: _harvest_smartbid(sb, settings, email, ref, harvest, token),
            _SMARTBID_ERRORS,
        )
        return
    if harvester_for(email, settings) == HARVESTER_EMAIL:
        # Imported here: the email body imports this module for the shared
        # machinery, so a top-level import would be a cycle.
        from app.services import rfp_email_harvest

        _run_claimed(
            sb, settings, email, harvest, token, pipeline,
            lambda: rfp_email_harvest.harvest(sb, settings, email, ref, harvest, token, force=force),
            _EMAIL_ERRORS,
        )
        return
    _run_claimed(
        sb, settings, email, harvest, token, pipeline,
        lambda: _harvest_procore(sb, settings, email, ref, harvest, token),
        _PROCORE_ERRORS,
    )


def _no_reference_message(method: str | None) -> str:
    if method == METHOD_PIPELINESUITE:
        return _MSG_NO_PS_REFERENCE
    if method == METHOD_SMARTBID:
        return _MSG_NO_SB_REFERENCE
    if method in ef.EMAIL_METHODS:
        return _MSG_NO_EMAIL_FILES
    return _MSG_NO_LINK


def _release(sb, harvest_id: str, token: str, status: str, error: str | None) -> None:
    try:
        sb.table(TABLE).update(
            {
                "status": status,
                "claim_token": None,
                "last_error": error,
                "finished_at": _iso(_now()) if status == STATUS_FAILED else None,
            }
        ).eq("id", harvest_id).eq("claim_token", token).execute()
    except Exception:  # noqa: BLE001 - the queue records the failure regardless
        logger.exception("rfp harvest: release failed for %s", harvest_id)


def _permanent(
    sb, email: dict, harvest_id: str | None, message: str, pipeline: bool, *, token: str | None = None
) -> None:
    if harvest_id:
        if token:
            _release(sb, harvest_id, token, STATUS_FAILED, message)
        else:
            sb.table(TABLE).update(
                {"status": STATUS_FAILED, "last_error": message, "finished_at": _iso(_now())}
            ).eq("id", harvest_id).execute()
    if pipeline:
        _finish_email(sb, email["id"], harvest_id, error=message)
    else:
        if harvest_id:
            _link_email(sb, email, harvest_id)
        raise RfpHarvestPermanent(message)


def _done(sb, email: dict, harvest_id: str, pipeline: bool) -> None:
    if pipeline:
        _finish_email(sb, email["id"], harvest_id, error=None)
    else:
        _link_email(sb, email, harvest_id)


# ── Pipeline step (called from rfp_email_ingest) ─────────────────────────────


def step(sb, row: dict, *, park: Callable[[float, str | None], None],
         finish: Callable[[], bool]) -> None:
    """The `harvest` sweep step: make sure a job exists, or move the row on
    when no harvester applies. `park(seconds, error)` pushes next_attempt_at
    without spending an attempt; `finish()` CASes the row off `harvest` (to
    `create` since docs/RFP_CREATE.md). Never calls the platform."""
    settings = get_settings()
    if harvester_for(row, settings) is None:
        finish()
        return
    if reference_for(row) is None:
        finish()
        return
    usable, reason, until = availability_for(row, settings)
    if not usable:
        wait = settings.rfp_harvest_poll_seconds
        if until:
            wait = max(wait, (until - _now()).total_seconds())
        park(wait, reason)
        return
    if active_job(row["id"]) is None:
        # The tag rides along only for a test row (docs/RFP_TESTING.md 4.4).
        tag = {"test_session_id": row["test_session_id"]} if rfp_test.session_for_email(row) else {}
        try:
            enqueue(row["id"], created_by=None, settings=settings, **tag)
        except llm_queue.JobAlreadyActive:
            pass
    park(settings.rfp_harvest_poll_seconds, None)


# ── Detail helpers for the router ────────────────────────────────────────────

_PUBLIC_HARVEST_COLUMNS = (
    "id, rfp_email_id, method, external_key, external_url, status, attempts, last_error, "
    "data, description_text, instructions_text, files, file_count, files_accepted, "
    "bytes_downloaded, sandbox_run_id, facts_at, started_at, finished_at, created_at, updated_at"
)


def harvest_for_email(sb, email: dict) -> dict | None:
    """The harvest row the detail screen shows: never raw, never claim_token.
    Falls back to the platform object's row when the email is not linked
    yet (a sibling, or a row still at harvest)."""
    harvest_id = email.get("harvest_id")
    row = None
    if harvest_id:
        rows = sb.table(TABLE).select(_PUBLIC_HARVEST_COLUMNS).eq("id", harvest_id).limit(1).execute().data
        row = rows[0] if rows else None
    if row is None:
        ref = reference_for(email)
        if ref is not None:
            rows = (
                sb.table(TABLE)
                .select(_PUBLIC_HARVEST_COLUMNS)
                .eq("method", email.get("invitation_method"))
                .eq("external_key", ref.external_key)
                .limit(1)
                .execute()
            ).data
            row = rows[0] if rows else None
    return row


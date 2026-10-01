"""Pure facts for the BuildingConnected Bid Board slice
(docs/RFP_BUILDINGCONNECTED.md sections 2, 3.3, 3.6 and 3.7; BUILD_CONTRACT
D17, D22, D28 and 3.2).

Everything here is a function of an opportunity dict (the API's list object,
`tests/fixtures_bc/opportunities.json`) or of an `rfp_portal_invitations`
row: no database, no network, no settings import. The scan
(`rfp_bc_portal`) calls `entry_status` and `invitation_fields` for every
pulled row and `tracked_changes` for a known one; project creation
(`rfp_create.facts_for_bc`) calls `project_facts` on the stored row.

Facts from the live board that shape the rules:

- `clientValues` mirrors the GC's values for the fields a subcontractor may
  edit locally; it is null on the five foreign rows (source MANUAL, EMAIL,
  DISCOVERED). `effective()` prefers a non-null clientValues entry and falls
  back to the top-level field.
- NDA-masked rows have `client.company.id` null, `updatedAt` null,
  `invitedAt` null, `location` null and no text.
- `projectInformation` is HTML (div, br, b, u, i, ul, li, a); the text
  conversion keeps the newlines, which is why these two fields never go
  through `rfp_create._text` (it flattens them).
- DATE columns (`issued_on`, `est_start_date`, `est_finish_date`) carry the
  Pacific calendar day; `bid_time_unknown` is true when the due instant is
  midnight Pacific.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

COMPANY_TZ = ZoneInfo("America/Los_Angeles")

PORTAL = "buildingconnected"
AGENCY_KEY = "bc"
DEEP_LINK = "https://app.buildingconnected.com/opportunities/{id}/info"

ENTRY_STATES = ("UNDECIDED", "WILL_SUBMIT", "SUBMITTED")
STATE_SUBMITTED = "SUBMITTED"
STATE_DECLINED = "DECLINED"
REQUEST_TYPE_BUDGET = "BUDGET"

STATUS_MATCH = "match"
STATUS_HISTORICAL = "historical"

AGENCY_NDA = "(NDA)"
AGENCY_NO_GC = "(No GC)"

BID_NOTES_MAX_CHARS = 400
TRUNCATED_SUFFIX = " [truncated]"

# The fields a known row is diffed on (design 3.6); the change log stores
# the invitation column names, not the API's. The GC pair (fix round 3):
# `gc_external_id` moving is the GC swapped on the board (the scan resets an
# unlinked row's resolution and asks again on a linked row's project);
# `gc_external_name` alone moving is the same company renamed (logged only).
TRACKED_FIELDS = (
    "close_at",
    "job_walk_at",
    "expected_start_at",
    "expected_finish_at",
    "title",
    "submission_state",
    "is_archived",
    "address",
    "gc_external_id",
    "gc_external_name",
)

# Every rfp_portal_invitations column the scan writes from the opportunity
# (contract section 2 item 1 plus the D22 mapping). The resolution columns
# (gc_id, gc_kind, gc_candidates, gc_confirmed_*, project_gc_id, sibling_of,
# ignore_source) and `status` are written by other steps and are NEVER here:
# an update from a later scan must not wipe them.
INVITATION_COLUMNS = frozenset(
    {
        "title",
        "agency",
        "agency_key",
        "bid_number",
        "bid_number_raw",
        "close_at",
        "issued_on",
        "view_url",
        "external_id",
        "external_url",
        "payload",
        "payload_hash",
        "bc_updated_at",
        "invited_at",
        "job_walk_at",
        "expected_start_at",
        "expected_finish_at",
        "rfis_due_at",
        "address",
        "trade_name",
        "submission_state",
        "workflow_bucket",
        "source",
        "request_type",
        "is_archived",
        "is_nda_required",
        "is_sealed",
        "gc_external_id",
        "gc_external_name",
        "lead",
    }
)

PROJECT_FACT_KEYS = frozenset(
    {
        "name",
        "actual_bid_at",
        "bid_time_unknown",
        "invitation_at",
        "job_walk_at",
        "est_start_date",
        "est_finish_date",
        "address",
        "bidding_url",
        "project_information",
        "trade_instructions",
        "bid_notes",
        "notes",
        "is_budgetary",
        "sender_display",
        "gc_external_name",
        "trade_name",
    }
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


# ── Small helpers ────────────────────────────────────────────────────────────


def parse_ts(s: Any) -> datetime | None:
    """An ISO instant (Z or offset) as an aware UTC datetime; None otherwise.
    A datetime passes through (naive means UTC)."""
    if isinstance(s, datetime):
        parsed = s
    elif isinstance(s, str) and s.strip():
        try:
            parsed = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def pacific_date(dt: Any) -> date | None:
    """The Pacific calendar day of an instant (a datetime or an ISO string)."""
    parsed = parse_ts(dt)
    return parsed.astimezone(COMPANY_TZ).date() if parsed else None


def is_midnight_pacific(dt: Any) -> bool:
    """True when the instant is exactly 00:00:00 Pacific: a date-only value
    the GC typed without a time (D17: bid_time_unknown)."""
    parsed = parse_ts(dt)
    if parsed is None:
        return False
    local = parsed.astimezone(COMPANY_TZ)
    return local.hour == 0 and local.minute == 0 and local.second == 0 and local.microsecond == 0


def deep_link(opportunity_id: Any) -> str:
    return DEEP_LINK.format(id=str(opportunity_id or "").strip())


def normalize_name(name: str | None) -> str:
    """A project name key for the sibling rules (D12): NFKC, lowercase,
    punctuation to spaces, whitespace collapsed. Nothing is dropped (the
    matcher's own normalizer strips reference numbers; this one must keep
    "Phase 2" apart from "Phase 3")."""
    if not isinstance(name, str):
        return ""
    base = unicodedata.normalize("NFKC", name).lower()
    return " ".join(_NON_ALNUM.sub(" ", base).split())


def _clean_line(value: Any) -> str | None:
    """A single-line string: trimmed, inner whitespace collapsed, None when
    empty. For names, addresses and trade names (never the two text fields)."""
    if value is None:
        return None
    out = " ".join(str(value).split())
    return out or None


def _strip_private(opp: dict) -> dict:
    """The opportunity without fixture-only keys (a leading underscore)."""
    return {k: v for k, v in opp.items() if not str(k).startswith("_")}


# ── HTML to text (D28) ───────────────────────────────────────────────────────

_BLOCK_TAGS = frozenset(
    {
        "div", "p", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr",
        "table", "blockquote", "section", "article", "header", "footer", "pre",
        "hr", "dd", "dt", "dl",
    }
)
_DROP_TAGS = frozenset({"script", "style", "head", "title", "noscript", "template"})
_VOID_BREAK_TAGS = frozenset({"br", "hr"})


class _TextExtractor(HTMLParser):
    """Block tags to newlines, `li` prefixed "- ", `a` kept as "text (href)"
    when the href differs from the text, script/style dropped, entities
    unescaped by the parser itself (convert_charrefs)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._drop_depth = 0
        self._at_line_start = True
        self._links: list[tuple[str | None, int]] = []   # (href, index of the text start)

    def _soft_break(self) -> None:
        """A block boundary: one newline unless the text is already at a
        line start (adjacent block tags never make a blank line; only a
        `<br>` between them does, which is how the board's editor writes
        a blank line)."""
        if not self._at_line_start:
            self.parts.append("\n")
            self._at_line_start = True

    def _hard_break(self) -> None:
        self.parts.append("\n")
        self._at_line_start = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _DROP_TAGS:
            self._drop_depth += 1
            return
        if self._drop_depth:
            return
        if tag in _VOID_BREAK_TAGS:
            self._hard_break()
            return
        if tag in _BLOCK_TAGS:
            self._soft_break()
            if tag == "li":
                self.parts.append("- ")
                self._at_line_start = False
            return
        if tag == "a":
            href = next((v for k, v in attrs if k.lower() == "href"), None)
            self._links.append((href.strip() if isinstance(href, str) else None, len(self.parts)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _VOID_BREAK_TAGS or tag in _BLOCK_TAGS:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _DROP_TAGS:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if self._drop_depth:
            return
        if tag in _BLOCK_TAGS:
            self._soft_break()
            return
        if tag == "a" and self._links:
            href, start = self._links.pop()
            text = " ".join("".join(self.parts[start:]).split())
            if href and _href_differs(href, text):
                self.parts.append(f" ({href})")
                self._at_line_start = False

    def handle_data(self, data: str) -> None:
        if self._drop_depth:
            return
        self.parts.append(data)
        if data.strip():
            self._at_line_start = False


def _href_differs(href: str, text: str) -> bool:
    if not text:
        return True
    a = href.strip().lower().rstrip("/")
    b = text.strip().lower().rstrip("/")
    if a == b:
        return False
    for scheme in ("https://", "http://", "mailto:", "tel:"):
        if a.startswith(scheme) and a[len(scheme):] == b:
            return False
    return True


def html_to_text(html: str | None, *, max_chars: int) -> str | None:
    """D28. None (or a string with nothing but markup and whitespace) is
    None. Whitespace is collapsed per line, blank lines are kept to one in a
    row, and the result is capped at `max_chars` INCLUDING the trailing
    " [truncated]" marker (the PATCH schema caps the project columns at the
    same number, so an untouched field must round-trip)."""
    if html is None:
        return None
    if not isinstance(html, str):
        html = str(html)
    if not html.strip():
        return None
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    raw = "".join(parser.parts)
    lines: list[str] = []
    blank_pending = False
    for line in raw.split("\n"):
        cleaned = " ".join(line.split())
        if not cleaned:
            blank_pending = bool(lines)
            continue
        if blank_pending:
            lines.append("")
            blank_pending = False
        lines.append(cleaned)
    text = "\n".join(lines).strip()
    if not text:
        return None
    return _cap_text(text, max_chars)


def _cap_text(text: str, max_chars: int) -> str:
    if max_chars is None or max_chars <= 0 or len(text) <= max_chars:
        return text
    keep = max(0, max_chars - len(TRUNCATED_SUFFIX))
    return text[:keep].rstrip() + TRUNCATED_SUFFIX


# ── Opportunity readers ──────────────────────────────────────────────────────


def effective(opp: dict, key: str):
    """`clientValues[key]` when clientValues is present and the value is not
    None, else the top-level field. Foreign rows (clientValues null) and
    masked rows (every clientValues entry null) fall back."""
    values = opp.get("clientValues")
    if isinstance(values, dict):
        value = values.get(key)
        if value is not None:
            return value
    return opp.get(key)


def _company(opp: dict) -> dict:
    client = opp.get("client")
    if not isinstance(client, dict):
        return {}
    company = client.get("company")
    return company if isinstance(company, dict) else {}


def _lead(opp: dict) -> dict | None:
    client = opp.get("client")
    if not isinstance(client, dict):
        return None
    lead = client.get("lead")
    if not isinstance(lead, dict):
        return None
    email = _clean_line(lead.get("email"))
    out = {
        "first_name": _clean_line(lead.get("firstName")),
        "last_name": _clean_line(lead.get("lastName")),
        "email": email.lower() if email else None,
        "phone": _clean_line(lead.get("phoneNumber")),
    }
    return out if any(out.values()) else None


def lead_name(lead: dict | None) -> str | None:
    """"First Last" from a stored lead, or None."""
    if not isinstance(lead, dict):
        return None
    parts = (_clean_line(lead.get("first_name")), _clean_line(lead.get("last_name")))
    return " ".join(p for p in parts if p) or None


def lead_view(lead: dict | None) -> dict | None:
    """The stored lead as the invitation detail carries it (BUILD_CONTRACT
    section 4): `{name, email, phone}`, the same shape the project's
    gc-confirm view uses, so the tab reads `lead.name`."""
    if not isinstance(lead, dict):
        return None
    return {
        "name": lead_name(lead),
        "email": _clean_line(lead.get("email")),
        "phone": _clean_line(lead.get("phone")),
    }


def _location_complete(opp: dict) -> str | None:
    location = effective(opp, "location")
    if not isinstance(location, dict):
        return None
    return _clean_line(location.get("complete"))


def _gc_external_id(opp: dict) -> str | None:
    return _clean_line(_company(opp).get("id"))


def _gc_external_name(opp: dict) -> str | None:
    return _clean_line(_company(opp).get("name"))


def is_masked(opp: dict) -> bool:
    """The NDA shape: `isNdaRequired` with no `client.company.id`."""
    return bool(opp.get("isNdaRequired")) and _gc_external_id(opp) is None


def _due_at(opp: dict) -> datetime | None:
    return parse_ts(effective(opp, "dueAt"))


def _title(opp: dict) -> str | None:
    return _clean_line(effective(opp, "name")) or _clean_line(opp.get("name"))


def entry_status(opp: dict, now: datetime) -> str:
    """Design 3.3: `match` when not archived, submissionState in
    ENTRY_STATES and (no due date or due >= now); else `historical`."""
    if opp.get("isArchived"):
        return STATUS_HISTORICAL
    if opp.get("submissionState") not in ENTRY_STATES:
        return STATUS_HISTORICAL
    due = _due_at(opp)
    if due is not None and due < parse_ts(now):
        return STATUS_HISTORICAL
    return STATUS_MATCH


def payload_hash(opp: dict) -> str:
    """sha256 of the canonical JSON (sorted keys, compact separators) of the
    opportunity without fixture-only keys; equal payloads skip the update."""
    canonical = json.dumps(_strip_private(opp), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _agency(opp: dict) -> str:
    name = _gc_external_name(opp)
    if name:
        return name
    return AGENCY_NDA if opp.get("isNdaRequired") else AGENCY_NO_GC


def invitation_fields(opp: dict, now: datetime, *, text_max_chars: int) -> dict:
    """Every rfp_portal_invitations column the scan writes for this
    opportunity (INVITATION_COLUMNS). Never `status`: `entry_status` decides
    that on insert and the sweep owns it afterwards. `text_max_chars` is
    accepted for signature parity with the other builders; the text fields
    live in `payload` and are converted at project-creation time
    (`project_facts`), never stored twice."""
    opportunity_id = str(opp.get("id") or "").strip()
    invited = parse_ts(opp.get("invitedAt")) or parse_ts(opp.get("createdAt"))
    link = deep_link(opportunity_id)
    payload = _strip_private(opp)
    fields = {
        "title": _title(opp) or opportunity_id,
        "agency": _agency(opp),
        "agency_key": AGENCY_KEY,
        "bid_number": opportunity_id,
        "bid_number_raw": opportunity_id,
        "close_at": _iso(_due_at(opp)),
        "issued_on": (pacific_date(invited).isoformat() if invited else None),
        "view_url": link,
        "external_id": opportunity_id,
        "external_url": link,
        "payload": payload,
        "payload_hash": payload_hash(opp),
        "bc_updated_at": _iso(parse_ts(opp.get("updatedAt"))),
        "invited_at": _iso(parse_ts(opp.get("invitedAt"))),
        "job_walk_at": _iso(parse_ts(effective(opp, "jobWalkAt"))),
        "expected_start_at": _iso(parse_ts(effective(opp, "expectedStartAt"))),
        "expected_finish_at": _iso(parse_ts(effective(opp, "expectedFinishAt"))),
        "rfis_due_at": _iso(parse_ts(effective(opp, "rfisDueAt"))),
        "address": _location_complete(opp),
        "trade_name": _clean_line(effective(opp, "tradeName")),
        "submission_state": _clean_line(opp.get("submissionState")),
        "workflow_bucket": _clean_line(opp.get("workflowBucket")),
        "source": _clean_line(opp.get("source")),
        "request_type": _clean_line(opp.get("requestType")),
        "is_archived": bool(opp.get("isArchived")),
        "is_nda_required": bool(opp.get("isNdaRequired")),
        "is_sealed": bool(opp.get("isSealedBidding")),
        "gc_external_id": _gc_external_id(opp),
        "gc_external_name": _gc_external_name(opp),
        "lead": _lead(opp),
    }
    assert set(fields) == INVITATION_COLUMNS
    return fields


def _comparable(field: str, value: Any) -> Any:
    if field in ("close_at", "job_walk_at", "expected_start_at", "expected_finish_at"):
        return _iso(parse_ts(value))
    if field == "is_archived":
        return bool(value)
    return _clean_line(value)


def tracked_changes(old_row: dict, opp: dict, *, text_max_chars: int) -> list[dict]:
    """[{field, old, new}] over TRACKED_FIELDS between the stored row and
    the opportunity as pulled now. Values are the stored spellings (ISO
    instants, single-line strings, booleans); the GC pair compares the
    platform's company id and name as `invitation_fields` writes them (so
    the stored row must carry both columns, or a missing one reads as a
    change)."""
    new = invitation_fields(opp, datetime.now(timezone.utc), text_max_chars=text_max_chars)
    changes: list[dict] = []
    for field in TRACKED_FIELDS:
        before = _comparable(field, (old_row or {}).get(field))
        after = _comparable(field, new.get(field))
        if before != after:
            changes.append({"field": field, "old": before, "new": after})
    return changes


# ── Project facts (D17, design 3.7) ──────────────────────────────────────────


def _payload(row: dict) -> dict:
    payload = (row or {}).get("payload")
    return payload if isinstance(payload, dict) else {}


def _sender_display(lead: dict | None) -> str | None:
    if not isinstance(lead, dict):
        return None
    email = _clean_line(lead.get("email"))
    if not email:
        return None
    name = lead_name(lead)
    return f"{name} <{email}>" if name else email


def _first_chars(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    return text if len(text) <= limit else text[:limit].rstrip()


def project_facts(row: dict, *, text_max_chars: int, notes_max_chars: int) -> dict:
    """The project columns an invitation row (with its payload) yields:
    PROJECT_FACT_KEYS exactly. Nulls stay null (no due date, no address, no
    text); DATE columns are Pacific calendar days as ISO strings."""
    row = row or {}
    payload = _payload(row)
    external_id = _clean_line(row.get("external_id")) or _clean_line(payload.get("id")) or ""
    link = _clean_line(row.get("external_url")) or deep_link(external_id)
    close_at = parse_ts(row.get("close_at"))
    invited = parse_ts(row.get("invited_at")) or parse_ts(payload.get("invitedAt")) or parse_ts(payload.get("createdAt"))
    project_information = html_to_text(effective(payload, "projectInformation"), max_chars=text_max_chars)
    trade_instructions = html_to_text(effective(payload, "tradeSpecificInstructions"), max_chars=text_max_chars)
    gc_name = _clean_line(row.get("gc_external_name"))
    trade_name = _clean_line(row.get("trade_name"))
    request_type = _clean_line(row.get("request_type")) or _clean_line(payload.get("requestType"))
    lead = row.get("lead") if isinstance(row.get("lead"), dict) else _lead(payload)
    invited_day = pacific_date(invited)
    notes = (
        f"Created from BuildingConnected: {gc_name or AGENCY_NDA}, "
        f"package {trade_name or '(no package)'}, "
        f"invited {invited_day.isoformat() if invited_day else '(unknown date)'}. {link}"
    )
    start = pacific_date(row.get("expected_start_at"))
    finish = pacific_date(row.get("expected_finish_at"))
    facts = {
        "name": _clean_line(row.get("title")),
        "actual_bid_at": _iso(close_at),
        "bid_time_unknown": is_midnight_pacific(close_at) if close_at else False,
        "invitation_at": _iso(invited),
        "job_walk_at": _iso(parse_ts(row.get("job_walk_at"))),
        "est_start_date": start.isoformat() if start else None,
        "est_finish_date": finish.isoformat() if finish else None,
        "address": _clean_line(row.get("address")),
        "bidding_url": link,
        "project_information": project_information,
        "trade_instructions": trade_instructions,
        "bid_notes": _first_chars(trade_instructions or project_information, BID_NOTES_MAX_CHARS),
        "notes": _first_chars(notes, notes_max_chars) if notes_max_chars else notes,
        "is_budgetary": request_type == REQUEST_TYPE_BUDGET,
        "sender_display": _sender_display(lead),
        "gc_external_name": gc_name,
        "trade_name": trade_name,
    }
    assert set(facts) == PROJECT_FACT_KEYS
    return facts

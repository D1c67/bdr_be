"""Plans notes for the assistant (docs/ASSISTANT_PHASE4_PLAN.md 3.3, 3.4.1).

Filled by WI-2. The public names and signatures below are binding (plan
sections 3 and 5): WI-9a's routes, WI-11's `plans_notes_sweep` rider and the
backfill script code against them.

`plans_reader_notes` (migration 0180 A1) holds one row per note-like item of
the latest output versions of an AI Estimating run, searched through the
RPC `search_plans_notes` (A2). The table is filled without editing the AE
writer (deviation D8): by `sync_run` from the backfill script, the lazy
repair inside the notes and brief routes, and the maintenance sweep.

Rules every implementation keeps:
- Upsert through PostgREST with
  `on_conflict="run_id,source_scope,category,source_version,source_key"` and
  `ignore_duplicates=True` (the plain constraint `plans_reader_notes_key_uq`),
  chunks of 500; `category=''` for main and brief rows.
- Lower versions of the same (run, scope, category) are removed from Python
  after the insert, never by SQL text in a migration.
- Bodies clipped to 4,000 characters, control characters stripped. No chat
  text is ever written and no pricing table is read.
- `PROJECT_CATEGORY_LABELS` and `REFUSED_PROJECT_CATEGORIES` are imported by
  name from `app.services.plans_reader_runs` (never edited).

Pure Python over a sync Supabase client: every function is plain `def` and
is called from plain `def` routes, the maintenance rider or the script.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from postgrest.types import CountMethod, ReturnMethod

logger = logging.getLogger(__name__)

TABLE = "plans_reader_notes"
OUTPUTS_TABLE = "plans_reader_outputs"
DELIVERABLES_TABLE = "plans_reader_deliverables"

# The plain unique constraint plans_reader_notes_key_uq of migration 0180 A1.
# A test pins this list against the constraint text.
ON_CONFLICT = "run_id,source_scope,category,source_version,source_key"

SCOPE_CATEGORY = "category"
SCOPE_MAIN = "main"
SCOPE_BRIEF = "brief_deliverable"

CHUNK = 500
PAGE = 1000
MAX_PAGES = 50
DEFAULT_MAX_ROWS = 5000

BODY_MAX = 4000
TITLE_MAX = 300
SHEET_NUMBER_MAX = 60
SHEET_TITLE_MAX = 300
QUOTE_MAX = 1000
SECTION_MAX = 120
SEVERITY_MAX = 40
KEY_MAX = 200
CATEGORY_MAX = 60

READABLE_EXTENSIONS = (".pdf", ".docx", ".xlsx")

# The brief kinds main writes unless a brief deliverable exists (its rows win).
BRIEF_KINDS: tuple[str, ...] = (
    "brief_summary", "system", "assumption", "exclusion", "risk", "rfi", "missing_document",
    "other_trade", "sheet_summary", "spec_highlight",
)
KINDS: frozenset[str] = frozenset(
    {
        "category_summary", "page_note", "scope", "spec", "schedule", "detail", "question",
        "material_note", "confidence_note", *BRIEF_KINDS,
    }
)

# Category output arrays flattened to notes (counts and runs are skipped:
# quantities are served by the output digest).
_CATEGORY_ARRAYS: tuple[tuple[str, str], ...] = (
    ("scope", "scope"),
    ("specs", "spec"),
    ("schedules", "schedule"),
    ("details", "detail"),
    ("questions", "question"),
)

# Brief list fields and their note kinds.
_BRIEF_LISTS: tuple[tuple[str, str], ...] = (
    ("systems", "system"),
    ("assumptions", "assumption"),
    ("exclusions", "exclusion"),
    ("risks", "risk"),
    ("rfis", "rfi"),
    ("missing_documents", "missing_document"),
    ("other_trades_affecting_electrical", "other_trade"),
    ("sheet_summaries", "sheet_summary"),
    ("spec_highlights", "spec_highlight"),
)

_COLUMNS = (
    "run_id", "source_scope", "category", "source_version", "source_key", "kind", "document_id", "page",
    "sheet_number", "sheet_title", "title", "body", "quote", "section", "severity",
)

# C0 and C1 controls except tab and newline; NUL is never accepted by Postgres.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_DASHES_RE = re.compile("[\u2013\u2014]")
_BLANKS_RE = re.compile(r"[ \t]+")


# -- Small helpers ------------------------------------------------------------


def _clean(value: Any, limit: int) -> str | None:
    """Text with controls stripped, dashes normalized, blanks squeezed, cut
    to `limit` characters; None when nothing is left."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return None
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    text = _DASHES_RE.sub("-", text)
    text = "\n".join(_BLANKS_RE.sub(" ", line).strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return None
    return text[:limit].rstrip() or None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n


def _page(value: Any) -> int | None:
    n = _int(value)
    return n if n is not None and n >= 1 else None


def _uuid(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _digest(item: Any) -> str:
    raw = json.dumps(item, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _v1_key(path: str, item: Any) -> str:
    """A v1 item's key: its path plus the first 16 hex of sha1(json)."""
    return f"{path}:{_digest(item)}"[:KEY_MAX]


def _ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _paged(query_factory) -> list[dict]:
    """Every row of a select, read in pages of PAGE (PostgREST caps a page)."""
    out: list[dict] = []
    for page in range(MAX_PAGES):
        start = page * PAGE
        chunk = query_factory().range(start, start + PAGE - 1).execute().data or []
        out.extend(chunk)
        if len(chunk) < PAGE:
            return out
    logger.warning("plans_reader_notes: read stopped after %d pages", MAX_PAGES)
    return out


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _lines(values: Any, limit: int = 40) -> list[str]:
    out = []
    for v in values if isinstance(values, list) else []:
        text = _clean(v, 600)
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _join(parts: list[str | None], sep: str = "\n") -> str:
    return sep.join(p for p in parts if p)


# -- Sources ------------------------------------------------------------------


@dataclass
class Source:
    """One latest-version source of notes: a category output, the main
    output or the brief deliverable."""

    scope: str
    category: str  # '' for main and brief rows
    version: int
    payload: Any

    @property
    def label(self) -> str:
        if self.scope == SCOPE_CATEGORY:
            return f"category:{self.category}"
        return self.scope


def _latest_output_ids(sb, run_id: str) -> dict[tuple[str, str], tuple[int, str]]:
    """{(scope, category or ''): (version, output row id)} of the highest
    version per scope and category."""
    rows = _paged(
        lambda: sb.table(OUTPUTS_TABLE).select("id, scope, category, version").eq("run_id", run_id).order("id")
    )
    best: dict[tuple[str, str], tuple[int, str]] = {}
    for r in rows:
        scope = str(r.get("scope") or "")
        if scope not in (SCOPE_CATEGORY, SCOPE_MAIN):
            continue
        category = "" if scope == SCOPE_MAIN else str(r.get("category") or "")
        if scope == SCOPE_CATEGORY and not category:
            continue
        version = _int(r.get("version")) or 0
        if version < 1:
            continue
        key = (scope, category)
        if key not in best or version > best[key][0]:
            best[key] = (version, str(r.get("id")))
    return best


def _latest_brief_id(sb, run_id: str) -> tuple[int, str] | None:
    rows = (
        sb.table(DELIVERABLES_TABLE).select("id, version").eq("run_id", run_id).eq("kind", "brief").execute().data
        or []
    )
    best: tuple[int, str] | None = None
    for r in rows:
        version = _int(r.get("version")) or 0
        if version >= 1 and (best is None or version > best[0]):
            best = (version, str(r.get("id")))
    return best


def _sources(sb, run_id: str) -> list[Source]:
    """The latest sources of one run, categories first (sorted), then main,
    then the brief deliverable."""
    out: list[Source] = []
    latest = _latest_output_ids(sb, run_id)
    for (scope, category), (version, row_id) in sorted(latest.items(), key=lambda kv: (kv[0][0] != SCOPE_CATEGORY, kv[0][1])):
        rows = sb.table(OUTPUTS_TABLE).select("output").eq("id", row_id).limit(1).execute().data or []
        payload = rows[0].get("output") if rows else None
        if isinstance(payload, dict):
            out.append(Source(scope, category[:CATEGORY_MAX], version, payload))
    brief = _latest_brief_id(sb, run_id)
    if brief is not None:
        rows = sb.table(DELIVERABLES_TABLE).select("spec_json").eq("id", brief[1]).limit(1).execute().data or []
        spec = rows[0].get("spec_json") if rows else None
        if isinstance(spec, dict):
            out.append(Source(SCOPE_BRIEF, "", brief[0], spec))
    return out


# -- Flattening ---------------------------------------------------------------


def _row(src: Source, run_id: str, key: str, kind: str, body: Any, **fields: Any) -> dict | None:
    text = _clean(body, BODY_MAX)
    if not text or kind not in KINDS:
        return None
    row = {
        "run_id": str(run_id),
        "source_scope": src.scope,
        "category": src.category,
        "source_version": int(src.version),
        "source_key": str(key)[:KEY_MAX],
        "kind": kind,
        "document_id": _uuid(fields.get("document_id")),
        "page": _page(fields.get("page")),
        "sheet_number": _clean(fields.get("sheet_number"), SHEET_NUMBER_MAX),
        "sheet_title": _clean(fields.get("sheet_title"), SHEET_TITLE_MAX),
        "title": _clean(fields.get("title"), TITLE_MAX),
        "body": text,
        "quote": _clean(fields.get("quote"), QUOTE_MAX),
        "section": _clean(fields.get("section"), SECTION_MAX),
        "severity": _clean(fields.get("severity"), SEVERITY_MAX),
    }
    return {c: row[c] for c in _COLUMNS}


def _schedule_body(item: dict) -> str:
    parts = [_clean(item.get("name"), 300), _clean(item.get("notes"), 1000)]
    columns = [str(c) for c in item.get("columns") or [] if c is not None]
    if columns:
        parts.append("Columns: " + " | ".join(columns))
    lines = []
    for r in item.get("rows") or []:
        if isinstance(r, list):
            cells = [str(c) for c in r if c is not None and str(c).strip()]
            if cells:
                lines.append(" | ".join(cells))
    parts.append("\n".join(lines))
    return _join(parts)[:BODY_MAX]


def _spec_body(item: dict) -> str:
    equals = [str(x) for x in item.get("approved_equals") or [] if x]
    return _join(
        [
            _clean(item.get("item"), 600),
            f"Manufacturer: {item['manufacturer']}" if item.get("manufacturer") else None,
            f"Basis of design: {item['basis_of_design']}" if item.get("basis_of_design") else None,
            f"Approved equals: {', '.join(equals)}" if equals else None,
            f"Substitutions: {item['substitution_rule']}" if item.get("substitution_rule") else None,
            f"Requirements: {item['vendor_or_organization_requirements']}"
            if item.get("vendor_or_organization_requirements") else None,
        ]
    )


def _item_fields(kind: str, item: dict) -> tuple[str, dict]:
    """(body, extra fields) of one category array item."""
    if kind == "scope":
        return str(item.get("statement") or ""), {
            "quote": item.get("quote"), "section": item.get("section"),
        }
    if kind == "spec":
        return _spec_body(item), {"title": item.get("item"), "section": item.get("section")}
    if kind == "schedule":
        return _schedule_body(item), {"title": item.get("name"), "section": item.get("schedule_type")}
    if kind == "detail":
        title = _join([_clean(item.get("detail_ref"), 100), _clean(item.get("title"), 200)], " ")
        return _join([_clean(item.get("title"), 300), _clean(item.get("electrical_relevance"), 3000)]), {"title": title}
    if kind == "question":
        why = _clean(item.get("why"), 1000)
        return _join([_clean(item.get("question"), 2000), f"Why: {why}" if why else None]), {}
    return "", {}


def _page_titles(output: dict) -> dict[tuple[str | None, int], tuple[str | None, str | None]]:
    """{(document_id, page): (sheet_number, title)} from the output's pages."""
    out: dict[tuple[str | None, int], tuple[str | None, str | None]] = {}
    for p in output.get("pages") or []:
        if not isinstance(p, dict):
            continue
        page = _page(p.get("page"))
        if page is None:
            continue
        out[(_uuid(p.get("document_id")), page)] = (p.get("sheet_number"), p.get("title"))
    return out


def _flatten_category(src: Source, run_id: str) -> list[dict]:
    from app.services import plans_reader_rows

    output = src.payload
    v2 = plans_reader_rows.is_v2(output)
    rows: list[dict | None] = []
    rows.append(_row(src, run_id, "summary", "category_summary", output.get("summary")))
    titles = _page_titles(output)
    for i, p in enumerate(output.get("pages") or []):
        if not isinstance(p, dict):
            continue
        for j, note in enumerate(p.get("notes") or []):
            rows.append(
                _row(
                    src, run_id, _v1_key(f"pages[{i}].notes[{j}]", note), "page_note", note,
                    document_id=p.get("document_id"), page=p.get("page"), sheet_number=p.get("sheet_number"),
                    sheet_title=p.get("title"),
                )
            )
    for array, kind in _CATEGORY_ARRAYS:
        for i, item in enumerate(output.get(array) or []):
            if not isinstance(item, dict):
                continue
            if v2 and item.get("status") == "superseded":
                continue
            sheet = item.get("sheet") if isinstance(item.get("sheet"), dict) else {}
            document_id = sheet.get("document_id") or item.get("document_id")
            page = sheet.get("page") or item.get("page")
            sheet_number = sheet.get("sheet_number") or item.get("sheet_number")
            row_id = item.get("row_id") if v2 else None
            key = str(row_id) if isinstance(row_id, str) and row_id else _v1_key(f"{array}[{i}]", item)
            body, extra = _item_fields(kind, item)
            known = titles.get((_uuid(document_id), _page(page) or 0), (None, None))
            rows.append(
                _row(
                    src, run_id, key, kind, body,
                    document_id=document_id, page=page, sheet_number=sheet_number or known[0],
                    sheet_title=known[1], **extra,
                )
            )
    return [r for r in rows if r is not None]


def _brief_of(payload: Any) -> dict | None:
    """The BRIEF_SPEC object of a brief deliverable (stored bare, or wrapped
    under `brief`)."""
    if not isinstance(payload, dict):
        return None
    if "project_summary" not in payload and isinstance(payload.get("brief"), dict):
        return payload["brief"]
    return payload


def _flatten_brief(src: Source, run_id: str, brief: dict) -> list[dict]:
    rows: list[dict | None] = []
    for field_name in ("project_summary", "electrical_scope_summary"):
        rows.append(
            _row(src, run_id, f"brief.{field_name}", "brief_summary", brief.get(field_name),
                 title=field_name.replace("_", " "))
        )
    for field_name, kind in _BRIEF_LISTS:
        for i, item in enumerate(brief.get(field_name) or []):
            key = f"brief.{field_name}[{i}]"
            if isinstance(item, str):
                rows.append(_row(src, run_id, key, kind, item))
                continue
            if not isinstance(item, dict):
                continue
            if kind == "system":
                body = _join(
                    [
                        f"{item.get('system') or 'System'}: in scope {item.get('in_scope') or 'unclear'}.",
                        _clean(item.get("notes"), 3000),
                    ],
                    " ",
                )
                rows.append(_row(src, run_id, key, kind, body, title=item.get("system")))
            elif kind == "risk":
                rows.append(
                    _row(src, run_id, key, kind, item.get("risk"), severity=item.get("severity"),
                         section=item.get("source"))
                )
            elif kind == "rfi":
                rows.append(_row(src, run_id, key, kind, item.get("question"), section=item.get("source")))
            elif kind == "sheet_summary":
                rows.append(
                    _row(src, run_id, key, kind, item.get("summary"), sheet_number=item.get("sheet_number"),
                         sheet_title=item.get("title"), title=item.get("title"))
                )
            elif kind == "spec_highlight":
                rows.append(
                    _row(src, run_id, key, kind, item.get("summary"), section=item.get("section"),
                         title=item.get("title"))
                )
    return [r for r in rows if r is not None]


def _flatten_main(src: Source, run_id: str, *, skip_brief: bool) -> list[dict]:
    output = src.payload
    rows: list[dict | None] = []
    if not skip_brief and isinstance(output.get("brief"), dict):
        rows.extend(_flatten_brief(src, run_id, output["brief"]))
    for i, item in enumerate(output.get("material_notes") or []):
        if not isinstance(item, dict):
            continue
        manufacturers = _lines(item.get("specified_manufacturers"))
        lead = _lines(item.get("lead_time_items"))
        body = _join(
            [
                *_lines(item.get("notes")),
                f"Specified manufacturers: {', '.join(manufacturers)}" if manufacturers else None,
                f"Lead time items: {', '.join(lead)}" if lead else None,
            ]
        )
        rows.append(_row(src, run_id, f"material_notes[{i}]", "material_note", body, title=item.get("material_category")))
    for i, note in enumerate(output.get("confidence_notes") or []):
        rows.append(_row(src, run_id, f"confidence_notes[{i}]", "confidence_note", note))
    return [r for r in rows if r is not None]


def flatten(src: Source, run_id: str, *, brief_deliverable_exists: bool = False) -> list[dict]:
    """The note rows of one source (deduplicated by source_key, first wins).
    A main source skips its brief kinds when a brief deliverable exists."""
    if src.scope == SCOPE_CATEGORY:
        rows = _flatten_category(src, run_id)
    elif src.scope == SCOPE_MAIN:
        rows = _flatten_main(src, run_id, skip_brief=brief_deliverable_exists)
    elif src.scope == SCOPE_BRIEF:
        brief = _brief_of(src.payload)
        rows = _flatten_brief(src, run_id, brief) if brief else []
    else:
        rows = []
    seen: set[str] = set()
    out = []
    for r in rows:
        if r["source_key"] in seen:
            continue
        seen.add(r["source_key"])
        out.append(r)
    return out


def plan_run(sb: Any, run_id: str) -> dict:
    """What `sync_run` would write, without writing: `{run_id, sources:
    [{label, version, rows}], rows}` (the backfill's dry run)."""
    sources = _sources(sb, str(run_id))
    has_brief = any(s.scope == SCOPE_BRIEF for s in sources)
    out = []
    total = 0
    for src in sources:
        n = len(flatten(src, str(run_id), brief_deliverable_exists=has_brief))
        total += n
        out.append({"label": src.label, "version": src.version, "rows": n})
    return {"run_id": str(run_id), "sources": out, "rows": total}


# -- Writing ------------------------------------------------------------------


def _existing_keys(sb, run_id: str, src: Source) -> set[str]:
    rows = _paged(
        lambda: sb.table(TABLE)
        .select("source_key")
        .eq("run_id", run_id)
        .eq("source_scope", src.scope)
        .eq("category", src.category)
        .eq("source_version", src.version)
        .order("source_key")
    )
    return {str(r.get("source_key")) for r in rows}


def _count(resp: Any) -> int:
    n = getattr(resp, "count", None)
    if isinstance(n, int):
        return n
    return len(getattr(resp, "data", None) or [])


def _remove_lower(sb, run_id: str, src: Source) -> int:
    """Delete rows of the same (run, scope, category) with a lower version."""
    resp = (
        sb.table(TABLE)
        .delete(count=CountMethod.exact, returning=ReturnMethod.minimal)
        .eq("run_id", run_id)
        .eq("source_scope", src.scope)
        .eq("category", src.category)
        .lt("source_version", src.version)
        .execute()
    )
    return _count(resp)


def _remove_main_brief(sb, run_id: str) -> int:
    """A brief deliverable suppresses main's brief kinds: delete them."""
    resp = (
        sb.table(TABLE)
        .delete(count=CountMethod.exact, returning=ReturnMethod.minimal)
        .eq("run_id", run_id)
        .eq("source_scope", SCOPE_MAIN)
        .eq("category", "")
        .in_("kind", list(BRIEF_KINDS))
        .execute()
    )
    return _count(resp)


def sync_run(sb: Any, run_id: str, *, max_rows: int | None = DEFAULT_MAX_ROWS) -> dict:
    """Write the notes of one run's latest outputs; idempotent.

    Sources: per (run, category) the highest `plans_reader_outputs` version
    with scope 'category', the highest scope 'main' row, and the highest
    `plans_reader_deliverables` row of kind 'brief' (`spec_json`). Flattening
    per 3.3 step 2 (v1 rows keyed `sha1(json)[:16]` with a path prefix, v2
    rows by their run-unique `row_id`, superseded rows skipped, `counts` and
    `runs` skipped; a brief deliverable writes `source_scope='brief_deliverable'`
    and suppresses main's brief kinds).

    Returns `{run_id, written, removed, versions}` plus `partial` (bool).
    `max_rows` bounds the rows written in one call (the lazy repair's cap):
    a source is written whole or not at all, the first source always, and a
    source left out sets `partial` (the sweep finishes it). None means no cap.
    """
    run_id = str(run_id)
    sources = _sources(sb, run_id)
    has_brief = any(s.scope == SCOPE_BRIEF for s in sources)
    written = removed = 0
    versions: dict[str, int] = {}
    partial = False
    for src in sources:
        rows = flatten(src, run_id, brief_deliverable_exists=has_brief)
        existing = _existing_keys(sb, run_id, src)
        missing = [r for r in rows if r["source_key"] not in existing]
        if missing and max_rows is not None and written > 0 and written + len(missing) > max_rows:
            partial = True
            continue
        for chunk in _chunks(missing, CHUNK):
            sb.table(TABLE).upsert(
                chunk, on_conflict=ON_CONFLICT, ignore_duplicates=True, returning=ReturnMethod.minimal
            ).execute()
        written += len(missing)
        versions[src.label] = src.version
        removed += _remove_lower(sb, run_id, src)
        if src.scope == SCOPE_MAIN and has_brief:
            removed += _remove_main_brief(sb, run_id)
    return {"run_id": run_id, "written": written, "removed": removed, "versions": versions, "partial": partial}


# -- Behind -------------------------------------------------------------------


def notes_behind(sb: Any, run_ids: list[str]) -> list[str]:
    """The run ids whose highest output version (category or main) or brief
    deliverable version is above the highest `source_version` in notes, or
    that have outputs and no notes.

    Compared per (scope, category), in the order given. A source whose
    flattening yields no rows (an empty output) keeps its run listed; the
    sweep's re-sync of it writes nothing and costs a few reads."""
    ids = list(dict.fromkeys(str(r) for r in run_ids or [] if r))
    if not ids:
        return []
    want: dict[str, dict[tuple[str, str], int]] = {}
    for chunk in _chunks(ids, 100):
        rows = _paged(
            lambda chunk=chunk: sb.table(OUTPUTS_TABLE)
            .select("run_id, scope, category, version")
            .in_("run_id", chunk)
            .order("id")
        )
        for r in rows:
            scope = str(r.get("scope") or "")
            if scope not in (SCOPE_CATEGORY, SCOPE_MAIN):
                continue
            category = "" if scope == SCOPE_MAIN else str(r.get("category") or "")[:CATEGORY_MAX]
            if scope == SCOPE_CATEGORY and not category:
                continue
            version = _int(r.get("version")) or 0
            slot = want.setdefault(str(r.get("run_id")), {})
            slot[(scope, category)] = max(slot.get((scope, category), 0), version)
        briefs = (
            sb.table(DELIVERABLES_TABLE).select("run_id, version").in_("run_id", chunk).eq("kind", "brief").execute().data
            or []
        )
        for r in briefs:
            version = _int(r.get("version")) or 0
            slot = want.setdefault(str(r.get("run_id")), {})
            slot[(SCOPE_BRIEF, "")] = max(slot.get((SCOPE_BRIEF, ""), 0), version)
    behind: list[str] = []
    for run_id in ids:
        for (scope, category), version in sorted((want.get(run_id) or {}).items()):
            if version < 1:
                continue
            have = (
                sb.table(TABLE)
                .select("source_version")
                .eq("run_id", run_id)
                .eq("source_scope", scope)
                .eq("category", category)
                .gte("source_version", version)
                .limit(1)
                .execute()
                .data
                or []
            )
            if not have:
                behind.append(run_id)
                break
    return behind


# -- Staleness ----------------------------------------------------------------


def _ext(filename: Any) -> str:
    name = str(filename or "").strip().lower()
    dot = name.rfind(".")
    return name[dot:] if dot >= 0 else ""


def stale_files(sb: Any, project_id: str, run: dict) -> list[dict]:
    """Project files added after the run (3.4.1, deviation D20).

    `project_files` of the project whose category is a key of
    `PROJECT_CATEGORY_LABELS`, whose extension is .pdf, .docx or .xlsx, whose
    id is not in the run's `plans_reader_files.source_project_file_id`, and
    whose `created_at` is after the latest `created_at` of the run's copied
    files (or `run.created_at` when none). Refused categories
    (`REFUSED_PROJECT_CATEGORIES`) are never listed. Each item is
    `{id, filename, category, created_at}`; `stale = bool(result)`.
    """
    from app.services.plans_reader_runs import PROJECT_CATEGORY_LABELS, REFUSED_PROJECT_CATEGORIES

    run_id = str(run.get("id") or "")
    copied = (
        sb.table("plans_reader_files").select("source_project_file_id, created_at").eq("run_id", run_id).execute().data
        or []
    ) if run_id else []
    copied_ids = {str(f["source_project_file_id"]) for f in copied if f.get("source_project_file_id")}
    stamps = [_ts(f.get("created_at")) for f in copied if f.get("source_project_file_id")]
    stamps = [t for t in stamps if t is not None]
    since = max(stamps) if stamps else _ts(run.get("created_at"))
    rows = _paged(
        lambda: sb.table("project_files")
        .select("id, filename, category, created_at")
        .eq("project_id", str(project_id))
        .order("created_at")
    )
    out: list[dict] = []
    for r in rows:
        category = str(r.get("category") or "")
        if category in REFUSED_PROJECT_CATEGORIES or category not in PROJECT_CATEGORY_LABELS:
            continue
        if _ext(r.get("filename")) not in READABLE_EXTENSIONS:
            continue
        if str(r.get("id")) in copied_ids:
            continue
        created = _ts(r.get("created_at"))
        if created is None or (since is not None and created <= since):
            continue
        out.append(
            {"id": str(r.get("id")), "filename": r.get("filename"), "category": category, "created_at": r.get("created_at")}
        )
    out.sort(key=lambda f: (_ts(f["created_at"]) or datetime.min.replace(tzinfo=timezone.utc), f["id"]))
    return out

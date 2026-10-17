"""Merge an RFP-created project into a project we are already bidding
(docs/PROJECT_MERGE.md).

RFP ingestion sometimes creates a project for an invitation that belongs to
a bid the team already has. A person with an admin role picks the project
they are bidding (the TARGET); the RFP-created one (the SOURCE) gives it its
GCs (with their bid dates and contacts), its RFP links, its email filing and
the documents the target does not already have, and is then deleted through
the existing archive (docs/PROJECT_DELETE.md) with the reason `merged`, so an
IT Admin can restore it.

Three steps, each a route on /projects:

- `merge_targets`: the Pick step's list. Empty search: the likely targets
  by name (`rfp_match.name_score` against the source's name). Typed search:
  number, name or a GC's name. `blocked` explains a source that cannot be
  merged at all (2.1) before anyone searches.
- `preview`: the "Are you sure?" step. Compares the documents by SHA-256
  (`file_hashes`, filled lazily within a time budget; the modal calls again
  until `ready`) and lists what moves and what stays.
- `merge`: decides the document list again from the stored hashes (never
  from the client), COPIES the moving objects under the target's prefix,
  then calls `merge_rfp_project` (migration 0149), which re-checks
  everything under a row lock and does the whole move and the archive in one
  transaction. A failed call removes the copies.

Errors come back as `project_merge:<code>` from the database (the archive's
own `project_delete:<code>` pass through); `ProjectMergeError` carries the
readable sentence and the HTTP status, and the routers answer with
`X-Error-Code: project_merge_<code>`.

Event-loop rule: everything here is sync (the Supabase SDK is sync); the
routers call it from plain `def` handlers.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from postgrest.exceptions import APIError
from storage3.exceptions import StorageApiError

from app.core.config import Settings, get_settings
from app.core.file_categories import DRAWING_CATEGORIES, FILE_NOTE_MAX_CHARS, INITIAL_CATEGORIES
from app.core.roles import ACTUAL_BID_VIEWER_ROLES, INTERNAL_ROLES, Role
from app.services import file_hashes, project_delete, storage
from app.services.notifications import audit, notify_role

logger = logging.getLogger(__name__)

AUDIT_MERGE = "project.merge"
# The bell row on the target (docs/PROJECT_MERGE.md 7.3).
NOTIFY_TYPE = "project_merged"

# Stages a project can never be bid from again (workflow.STAGES' terminal
# placeholders and the Go/No-Go kill).
CLOSED_STAGES = ("declined", "pm_only", "cp_only")

# The preview's and the merge's own hashing budget, checked between files.
HASH_BUDGET_S = 20.0
# Each file list in the preview is capped; the totals say how many in all.
LIST_CAP = 200

Q_MAX = 100
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
SUGGEST_LIMIT = 10
SUGGEST_FLOOR = 0.35
_SEARCH_CAP = 200
_GC_SEARCH_CAP = 50
_GC_PROJECT_CAP = 1000
_SUGGEST_CAP = 1000
_IN_CHUNK = 200

# Documents produced by the old project's own bid work: they stay with it.
WORK_CATEGORIES = frozenset({"quote", "rfq_split", "proposal"})

COMPANY_TZ = ZoneInfo("America/Los_Angeles")

# What counts as work already started on the source (2.1), in the order the
# sentence names it.
WORK_LABELS: dict[str, str] = {
    "rfq_sends": "RFQs sent to vendors",
    "quotes": "vendor quotes",
    "estimator_assignments": "an estimator assignment",
    "proposal_sends": "proposals",
    "external_submissions": "a recorded submission",
    "bid_outcomes": "a bid outcome",
}

PROJECT_SELECT = (
    "id, number, name, current_stage, abandoned_at, pm_stage, cp_enrolled_at, test_session_id, "
    "internal_bid_at, actual_bid_at"
)
FILE_SELECT = (
    "id, project_id, category, storage_path, preview_path, preview_status, filename, size_bytes, "
    "sha256, note, doc_type, created_at"
)
_RECORD_SELECT = (
    "project_id, harvest_id, files_status, files_claimed_at, split_status, split_job_id, "
    "split_resolution"
)

_MSG_WORK = (
    "Work has started on this project ({detail}). That work cannot move to another project: "
    "delete this project or bid it instead."
)

# code -> (sentence, HTTP status). `{detail}` is the database's detail text.
MESSAGES: dict[str, tuple[str, int]] = {
    "not_found": ("Project not found.", 404),
    "gone": (
        "This project no longer exists: it may have just been merged or deleted.", 409,
    ),
    "not_from_rfp": (
        "Only a project created by RFP ingestion can be merged into another project.", 409,
    ),
    "pm_enrolled": (
        "This project is in Project Management, so it cannot be merged into another project.",
        409,
    ),
    "cp_enrolled": (
        "This project is enrolled in Certified Payroll, so it cannot be merged into another "
        "project.", 409,
    ),
    "work_started": (_MSG_WORK, 409),
    "files_arriving": (
        "Documents are still being copied into this project. Merge it once they have arrived.",
        409,
    ),
    "split_running": (
        "The Bid File Splitter is still working on this project's documents. Merge it once "
        "the split has finished.", 409,
    ),
    "target_not_found": ("The project to merge into was not found.", 409),
    "same_project": ("A project cannot be merged into itself.", 409),
    "target_closed": (
        "That project is no longer being bid (abandoned, declined, its outcome recorded, or in "
        "Project Management), so nothing can be merged into it.", 409,
    ),
    "target_scope": (
        "That project belongs to a different test session, so these two cannot be merged.", 409,
    ),
    "source_name_mismatch": (
        "The old project name you typed does not match. Type it exactly as shown.", 422,
    ),
    "target_name_mismatch": (
        "The new project name you typed does not match. Type it exactly as shown.", 422,
    ),
    "files_not_ready": (
        "The documents are still being compared. Wait for the comparison to finish, then "
        "merge.", 409,
    ),
    "files_changed": (
        "The documents changed while you were confirming. Review the updated list, then merge "
        "again.", 409,
    ),
    "copy_failed": (
        "The documents could not be copied to the other project, so nothing was merged. Try "
        "again.", 409,
    ),
    "referenced": (
        "Other records still point at this project, so it cannot be merged ({detail}).", 409,
    ),
}
_GENERIC = ("The projects could not be merged ({code}).", 409)


class ProjectMergeError(Exception):
    """A user-facing failure: `.args[0]` is the sentence, `status_code` the
    HTTP status, `code` the machine code (also the X-Error-Code header)."""

    def __init__(self, message: str, status_code: int = 409, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


# ── Pure helpers ─────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _chunks(items: list, size: int = _IN_CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def pacific_date(value) -> str | None:
    """A timestamp as the company (Pacific) calendar date, ISO formatted:
    the database's `(ts at time zone 'America/Los_Angeles')::date`."""
    dt = _parse_ts(value)
    return dt.astimezone(COMPANY_TZ).date().isoformat() if dt else None


def _fmt_date(value) -> str | None:
    """An ISO date (or timestamp) as m/d/yyyy for a sentence."""
    if not value:
        return None
    try:
        d = date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    return f"{d.month}/{d.day}/{d.year}"


def confirm_text(project: dict) -> str:
    """What the person types for a project: the delete rule (trimmed name,
    else the trimmed number), which the database applies again."""
    return project_delete.confirm_text(project)[0]


def project_label(project: dict) -> str:
    """"26.9.7120 Cimarron Scoreboard" (number and name, either may be absent)."""
    parts = [str(project.get(k) or "").strip() for k in ("number", "name")]
    return " ".join(p for p in parts if p) or "a project"


def merge_note(source: dict, old_note: str | None = None) -> str:
    """The note a moved document carries (docs/PROJECT_MERGE.md 3.4):
    "Moved from NUMBER NAME (merged)", then the note it already had."""
    base = f"Moved from {project_label(source)} (merged)"
    old = (old_note or "").strip()
    text = f"{base}. {old}" if old else base
    return text[:FILE_NOTE_MAX_CHARS]


def landing(row: dict, locked: bool) -> tuple[str, str | None]:
    """(category, doc_type) a moved document gets on the target: what an
    internal upload of it to the target at its current stage would be
    (routers/files.py). Once the target's package has been sent
    (`handoff_locked`), the initial blocks are frozen and new material goes
    in as a revision (drawings and specifications, with their doc_type) or
    an additional file (the RFP document). Before that, an update category
    is refused, so a revision or additional file goes back to the block it
    stands for. Everything else keeps its category."""
    category = str(row.get("category") or "other")
    doc_type = row.get("doc_type")
    if locked:
        if category in DRAWING_CATEGORIES:
            return "revision", "drawing"
        if category == "specification":
            return "revision", "specification"
        if category in INITIAL_CATEGORIES:
            return "additional", None
        return category, doc_type if category in ("revision", "addendum") else None
    if category == "revision":
        if doc_type == "drawing":
            return "drawing", None
        if doc_type == "specification":
            return "specification", None
        return "other", None
    if category == "additional":
        return "other", None
    return category, doc_type if category == "addendum" else None


def work_sentence(keys) -> str:
    """The work_started sentence naming what was found, in WORK_LABELS order."""
    found = [WORK_LABELS[k] for k in WORK_LABELS if k in set(keys or [])]
    extra = [str(k) for k in (keys or []) if k not in WORK_LABELS]
    detail = ", ".join(found + extra) or "bid work"
    return _MSG_WORK.format(detail=detail)


def _error(code: str, detail: str | None = None) -> ProjectMergeError:
    if code == "work_started":
        keys = [k.strip() for k in (detail or "").split(",") if k.strip()]
        return ProjectMergeError(work_sentence(keys), 409, code)
    sentence, status_code = MESSAGES.get(code, _GENERIC)
    detail_text = (detail or "").strip() or "no detail"
    return ProjectMergeError(sentence.format(detail=detail_text, code=code), status_code, code)


def _blocked(code: str, detail: str | None = None) -> dict:
    return {"code": code, "message": str(_error(code, detail))}


def error_code_of(exc: Exception) -> str | None:
    """The `project_merge:<code>` (or the archive's `project_delete:<code>`)
    a database function raised, if any."""
    text = getattr(exc, "message", None) or str(exc)
    if not isinstance(text, str):
        return None
    for marker in ("project_merge:", "project_delete:"):
        if marker in text:
            return text.split(marker, 1)[1].split()[0].strip("'\",.}:;")
    return None


def _db_error(exc: Exception) -> ProjectMergeError:
    """Map an APIError from merge_rfp_project; anything else re-raises."""
    code = error_code_of(exc)
    if code is None:
        raise exc
    if code == "not_found":
        # The source vanished between the service's read and the lock: the
        # second of two merges of the same pair lands here.
        code = "gone"
    detail = getattr(exc, "details", None)
    return _error(code, detail if isinstance(detail, str) else None)


def target_block(source: dict, target: dict | None, *, has_outcome: bool) -> dict | None:
    """`{code, message}` when `target` cannot take the merge (2.2), else None."""
    if target is None:
        return _blocked("target_not_found")
    if str(target.get("id")) == str(source.get("id")):
        return _blocked("same_project")
    if (
        target.get("abandoned_at")
        or str(target.get("current_stage") or "") in CLOSED_STAGES
        or target.get("pm_stage") is not None
        or has_outcome
    ):
        return _blocked("target_closed")
    if (target.get("test_session_id") or None) != (source.get("test_session_id") or None):
        return _blocked("target_scope")
    return None


def search_pattern(q: str | None) -> str | None:
    """The search box as an ILIKE substring pattern, or None when blank:
    control characters dropped, trimmed, at most Q_MAX characters, the LIKE
    wildcards and the escape backslash-escaped (rfp_create._like_literal),
    and `*` as the one-character wildcard (PostgREST reads `*` as `%` and
    has no escape for it). It always travels as one filter param, never
    inside an or=(...) group, so commas and parentheses need no quoting."""
    text = "".join(ch for ch in (q or "") if ch >= " " and ch != "\x7f").strip()[:Q_MAX].strip()
    if not text:
        return None
    text = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{text.replace('*', '_')}%"


def visible_bid_at(project: dict, role: Role | str | None) -> str | None:
    """The bid date a role may see: the actual (to-GC) date for the
    ACTUAL_BID_VIEWER_ROLES (falling back to the internal one), the internal
    date for everyone else."""
    if role in ACTUAL_BID_VIEWER_ROLES:
        return project.get("actual_bid_at") or project.get("internal_bid_at")
    return project.get("internal_bid_at")


def nearest_first(rows: list[dict], role, now: datetime | None = None) -> list[dict]:
    """Sorted by the visible bid date's distance from now (no date last),
    then by name."""
    now = now or _now()

    def key(row):
        at = _parse_ts(visible_bid_at(row, role))
        gap = abs((at - now).total_seconds()) if at else float("inf")
        return (at is None, gap, (row.get("name") or "").lower(), str(row.get("id")))

    return sorted(rows, key=key)


def comparison_set(candidates: list[dict], targets: list[dict]) -> tuple[list[dict], list[dict]]:
    """The rows whose hash the comparison needs. Equal bytes have equal
    sizes, so a document whose recorded size no file on the other side has
    cannot be a duplicate and is never hashed; a row without a recorded size
    could match anything, so it (and every row facing it) is hashed."""
    if not candidates or not targets:
        return [], []
    c_sizes = {r.get("size_bytes") for r in candidates}
    t_sizes = {r.get("size_bytes") for r in targets}
    c_unknown = None in c_sizes
    t_unknown = None in t_sizes
    rel_c = [r for r in candidates if r.get("size_bytes") is None or t_unknown or r.get("size_bytes") in t_sizes]
    rel_t = [r for r in targets if r.get("size_bytes") is None or c_unknown or r.get("size_bytes") in c_sizes]
    return rel_c, rel_t


@dataclass
class FilePlan:
    """What happens to each source document. `moving` and `unreadable` (a
    subset of `moving`: its object could not be read, so it moves without a
    comparison) go to the target; `skipped` pairs a source row with the
    target row holding the same bytes; `kept` are the old project's work
    documents. `hashed` of `total` rows the comparison needs are done;
    `ready` once all are."""

    moving: list[dict] = field(default_factory=list)
    skipped: list[tuple[dict, dict]] = field(default_factory=list)
    kept: list[dict] = field(default_factory=list)
    unreadable: list[dict] = field(default_factory=list)
    hashed: int = 0
    total: int = 0

    @property
    def ready(self) -> bool:
        return self.hashed >= self.total


def classify(
    candidates: list[dict], kept: list[dict], targets: list[dict], relevant: list[dict],
    failed_ids: set[str],
) -> FilePlan:
    """The plan from rows whose `sha256` is filled as far as it got. A
    candidate is skipped when its hash equals any target file's hash (the
    oldest such target file is named); every other candidate moves."""
    plan = FilePlan(kept=list(kept))
    plan.total = len(relevant)
    plan.hashed = sum(
        1 for r in relevant if not file_hashes.needs_hash(r) or str(r.get("id")) in failed_ids
    )
    by_hash: dict[str, dict] = {}
    for t in sorted(targets, key=lambda r: (str(r.get("created_at") or ""), str(r.get("id")))):
        if not file_hashes.needs_hash(t):
            by_hash.setdefault(t["sha256"], t)
    for c in candidates:
        same = by_hash.get(c.get("sha256")) if not file_hashes.needs_hash(c) else None
        if same is not None:
            plan.skipped.append((c, same))
            continue
        plan.moving.append(c)
        if str(c.get("id")) in failed_ids:
            plan.unreadable.append(c)
    return plan


def gc_plan(source: dict, source_links: list[dict], target_gc_ids: set[str],
            names: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """(gcs moving [{gc_id, name, needs_by}], gcs already on the target
    [{gc_id, name}]), each by name. A moving link's due date is its own, else
    the source's bid date on the Pacific calendar (3.1)."""
    fallback = pacific_date(source.get("actual_bid_at"))
    moving, already = [], []
    for link in source_links:
        gc_id = str(link.get("gc_id"))
        name = names.get(gc_id)
        if gc_id in target_gc_ids:
            already.append({"gc_id": gc_id, "name": name})
        else:
            moving.append({"gc_id": gc_id, "name": name, "needs_by": link.get("needs_by") or fallback})
    order = lambda g: ((g.get("name") or "").lower(), g["gc_id"])  # noqa: E731
    return sorted(moving, key=order), sorted(already, key=order)


def notify_message(actor_name: str | None, source: dict, gcs_moved: list[dict], files_moved: int) -> str:
    """"Tom Moore merged 26.9.7120 Cimarron into this project: GC added:
    Acme Builders (due 10/15/2026)" (docs/PROJECT_MERGE.md 7.3)."""
    parts: list[str] = []
    if gcs_moved:
        named = []
        for g in gcs_moved:
            due = _fmt_date(g.get("needs_by"))
            label = g.get("name") or "a GC"
            named.append(f"{label} (due {due})" if due else label)
        parts.append(("GC added: " if len(named) == 1 else "GCs added: ") + ", ".join(named))
    if files_moved:
        parts.append(f"{files_moved} document{'s' if files_moved != 1 else ''} added")
    if not parts:
        parts.append("nothing new was added (its GCs and documents were already here)")
    who = (actor_name or "").strip() or "Someone"
    return f"{who} merged {project_label(source)} into this project: {'; '.join(parts)}"


# ── Reads ────────────────────────────────────────────────────────────────────


def _project(sb, project_id) -> dict | None:
    if not _is_uuid(project_id):
        return None
    rows = (
        sb.table("projects").select(PROJECT_SELECT).eq("id", str(project_id)).limit(1).execute()
    ).data or []
    return rows[0] if rows else None


def _record(sb, project_id: str) -> dict | None:
    rows = (
        sb.table("rfp_created_projects").select(_RECORD_SELECT).eq("project_id", project_id)
        .limit(1).execute()
    ).data or []
    return rows[0] if rows else None


def _exists(query) -> bool:
    return bool((query.limit(1).execute()).data)


def work_found(sb, project_id: str) -> list[str]:
    """The WORK_LABELS keys the project has rows for (2.1 work_started)."""
    found: list[str] = []
    rfq_ids = [
        r["id"] for r in (
            sb.table("rfqs").select("id").eq("project_id", project_id).execute()
        ).data or []
    ]
    for table in ("rfq_sends", "quotes"):
        if any(_exists(sb.table(table).select("id").in_("rfq_id", chunk)) for chunk in _chunks(rfq_ids)):
            found.append(table)
    for table in ("estimator_assignments", "proposal_sends", "external_submissions", "bid_outcomes"):
        if _exists(sb.table(table).select("id").eq("project_id", project_id)):
            found.append(table)
    return found


def files_arriving(record: dict, settings: Settings | None = None) -> bool:
    """The RFP creation step is still copying documents in: `pending`, or
    `running` under a live claim (a claim older than the queue lease is a
    dead worker's, rfp_create_files.claim_is_stale)."""
    status = record.get("files_status")
    if status == "pending":
        return True
    if status != "running":
        return False
    from app.services import rfp_create_files

    return not rfp_create_files.claim_is_stale(record, settings or get_settings())


def _split_running(sb, record: dict) -> bool:
    """The splitter's own "live right now" predicate (rfp_split.split_running).
    A failed check reads as running: the merge waits rather than racing a
    re-file."""
    try:
        from app.services import rfp_split

        return bool(rfp_split.split_running(sb, record))
    except Exception:  # noqa: BLE001
        logger.exception("project merge: split check failed for %s", record.get("project_id"))
        return True


def source_block(sb, project: dict, settings: Settings | None = None) -> dict | None:
    """`{code, message}` when the project the button is on cannot be merged
    (2.1), else None."""
    record = _record(sb, project["id"])
    if record is None:
        return _blocked("not_from_rfp")
    if project.get("pm_stage") is not None:
        return _blocked("pm_enrolled")
    if project.get("cp_enrolled_at") is not None:
        return _blocked("cp_enrolled")
    work = work_found(sb, project["id"])
    if work:
        return _blocked("work_started", ",".join(work))
    if files_arriving(record, settings):
        return _blocked("files_arriving")
    if _split_running(sb, record):
        return _blocked("split_running")
    return None


def _has_outcome(sb, project_ids) -> set[str]:
    out: set[str] = set()
    for chunk in _chunks(sorted({str(i) for i in project_ids if i})):
        for r in (sb.table("bid_outcomes").select("project_id").in_("project_id", chunk).execute()).data or []:
            out.add(str(r.get("project_id")))
    return out


def _resolve(sb, source_id: str, target_id) -> tuple[dict, dict | None, dict | None]:
    """(source, target, blocked): 404 ProjectMergeError for an unknown source;
    `blocked` is the first refusal, the source's (2.1) before the target's
    (2.2)."""
    source = _project(sb, source_id)
    if source is None:
        raise _error("not_found")
    if str(target_id) == str(source["id"]):
        return source, None, _blocked("same_project")
    target = _project(sb, target_id)
    blocked = source_block(sb, source)
    if blocked is None:
        has_outcome = bool(target) and str(target["id"]) in _has_outcome(sb, [target["id"]])
        blocked = target_block(source, target, has_outcome=has_outcome)
    return source, target, blocked


def _files(sb, project_id: str) -> list[dict]:
    return (
        sb.table("project_files").select(FILE_SELECT).eq("project_id", project_id)
        .order("created_at").execute()
    ).data or []


def _in_use(sb, rows: list[dict]) -> set[str]:
    """The source documents that stay with it: its work categories, and every
    row another row points at (`project_merge_file_refs`, any foreign key
    into project_files)."""
    kept = {str(r["id"]) for r in rows if r.get("category") in WORK_CATEGORIES}
    ids = [str(r["id"]) for r in rows if str(r["id"]) not in kept]
    for chunk in _chunks(ids):
        refs = sb.rpc("project_merge_file_refs", {"p_file_ids": chunk}).execute().data or []
        kept.update(str(x) for x in refs)
    return kept


def file_plan(sb, source_id: str, target_id: str, *, budget_s: float) -> FilePlan:
    """Load both sides, hash what the comparison still needs (within the
    budget) and classify."""
    src = _files(sb, source_id)
    tgt = _files(sb, target_id)
    kept_ids = _in_use(sb, src)
    kept = [r for r in src if str(r["id"]) in kept_ids]
    candidates = [r for r in src if str(r["id"]) not in kept_ids]
    rel_c, rel_t = comparison_set(candidates, tgt)
    relevant = rel_c + rel_t
    run = file_hashes.fill(sb, [r for r in relevant if file_hashes.needs_hash(r)], budget_s=budget_s)
    return classify(candidates, kept, tgt, relevant, run.failed)


def _gc_names(sb, gc_ids) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in _chunks(sorted({str(g) for g in gc_ids if g})):
        for r in (sb.table("general_contractors").select("id, name").in_("id", chunk).execute()).data or []:
            out[str(r["id"])] = r.get("name")
    return out


def _links(sb, project_ids) -> list[dict]:
    rows: list[dict] = []
    for chunk in _chunks(sorted({str(p) for p in project_ids if p})):
        rows.extend(
            (sb.table("project_gcs").select("id, project_id, gc_id, needs_by").in_("project_id", chunk)
             .execute()).data or []
        )
    return rows


def rfp_link_count(sb, project_id: str) -> int:
    """The RFP rows that point at the project and move with the merge (3.2):
    match records, emails, portal invitations and harvests."""
    total = len((sb.table("rfp_project_matches").select("id").eq("project_id", project_id).execute()).data or [])
    for table in ("rfp_emails", "rfp_portal_invitations"):
        total += len((
            sb.table(table).select("id").or_(
                f"created_project_id.eq.{project_id},match_project_id.eq.{project_id},"
                f"possible_rebid_project_id.eq.{project_id}"
            ).execute()
        ).data or [])
    total += len((sb.table("rfp_harvests").select("id").eq("project_id", project_id).execute()).data or [])
    return total


# ── Merge targets (the Pick step) ────────────────────────────────────────────


def _eligible(query, source: dict):
    """The 2.2 filters a projects query can carry (the outcome rule is a
    second read)."""
    query = (
        query.is_("abandoned_at", "null")
        .not_.in_("current_stage", list(CLOSED_STAGES))
        .is_("pm_stage", "null")
        .neq("id", source["id"])
    )
    session = source.get("test_session_id")
    return query.eq("test_session_id", session) if session else query.is_("test_session_id", "null")


_TARGET_SELECT = "id, number, name, current_stage, internal_bid_at, actual_bid_at, test_session_id"


def _search_rows(sb, source: dict, pattern: str) -> list[dict]:
    """Eligible projects whose number, name or a GC's name contains the text."""
    found: dict[str, dict] = {}
    for column in ("number", "name"):
        rows = (
            _eligible(sb.table("projects").select(_TARGET_SELECT), source).ilike(column, pattern)
            .limit(_SEARCH_CAP).execute()
        ).data or []
        for r in rows:
            found.setdefault(str(r["id"]), r)
    gc_ids = [
        str(r["id"]) for r in (
            sb.table("general_contractors").select("id").ilike("name", pattern).limit(_GC_SEARCH_CAP)
            .execute()
        ).data or []
    ]
    project_ids: list[str] = []
    for chunk in _chunks(gc_ids):
        for r in (sb.table("project_gcs").select("project_id").in_("gc_id", chunk).execute()).data or []:
            pid = str(r.get("project_id"))
            if pid not in found and pid not in project_ids:
                project_ids.append(pid)
    for chunk in _chunks(project_ids[:_GC_PROJECT_CAP]):
        rows = (
            _eligible(sb.table("projects").select(_TARGET_SELECT), source).in_("id", chunk).execute()
        ).data or []
        for r in rows:
            found.setdefault(str(r["id"]), r)
    return list(found.values())


def _suggest_rows(sb, source: dict, role, settings: Settings) -> list[tuple[float, dict]]:
    """Eligible projects bid inside the New Bid check's window, scored by
    name against the source's name, above SUGGEST_FLOOR, best first. The
    window reads the actual bid date only for a role that may see it."""
    from app.services import rfp_match

    lo = rfp_match.pg_ts(_now() - timedelta(days=int(settings.rfp_match_precreate_window_days)))
    query = _eligible(sb.table("projects").select(_TARGET_SELECT), source)
    if role in ACTUAL_BID_VIEWER_ROLES:
        query = query.or_(f"actual_bid_at.gte.{lo},internal_bid_at.gte.{lo}")
    else:
        query = query.gte("internal_bid_at", lo)
    rows = (query.limit(_SUGGEST_CAP).execute()).data or []
    scored = []
    for row in rows:
        ns = rfp_match.name_score(
            source.get("name") or "", row.get("name") or "", a_number=source.get("number"),
            b_number=row.get("number"), settings=settings,
        )
        if ns.score >= SUGGEST_FLOOR:
            scored.append((ns.score, row))
    scored.sort(key=lambda pair: (-pair[0], (pair[1].get("name") or "").lower(), str(pair[1]["id"])))
    return scored


def _target_items(sb, rows: list[dict], role) -> list[dict]:
    ids = [str(r["id"]) for r in rows]
    links = _links(sb, ids)
    names = _gc_names(sb, {link.get("gc_id") for link in links})
    gcs_by_project: dict[str, set[str]] = {}
    for link in links:
        name = names.get(str(link.get("gc_id")))
        if name:
            gcs_by_project.setdefault(str(link.get("project_id")), set()).add(name)
    created: set[str] = set()
    for chunk in _chunks(ids):
        for r in (sb.table("rfp_created_projects").select("project_id").in_("project_id", chunk).execute()).data or []:
            created.add(str(r.get("project_id")))
    return [
        {
            "id": r["id"],
            "number": r.get("number"),
            "name": r.get("name"),
            "stage": r.get("current_stage"),
            "bid_at": visible_bid_at(r, role),
            "gc_names": sorted(gcs_by_project.get(str(r["id"]), set()), key=str.lower),
            "rfp_created": str(r["id"]) in created,
        }
        for r in rows
    ]


def merge_targets(
    sb, source_id: str, *, q: str | None, limit: int | None, role,
    settings: Settings | None = None,
) -> dict:
    """`{items, blocked}` for the Pick step (docs/PROJECT_MERGE.md 2.2).
    A blocked source answers its refusal and no items. Empty `q`: up to
    SUGGEST_LIMIT likely targets by name score; otherwise up to `limit`
    matches, nearest bid date first."""
    settings = settings or get_settings()
    source = _project(sb, source_id)
    if source is None:
        raise _error("not_found")
    blocked = source_block(sb, source, settings)
    if blocked is not None:
        return {"items": [], "blocked": blocked}
    pattern = search_pattern(q)
    if pattern is None:
        scored = _suggest_rows(sb, source, role, settings)
        closed = _has_outcome(sb, [r["id"] for _, r in scored])
        rows = [r for _, r in scored if str(r["id"]) not in closed][:SUGGEST_LIMIT]
    else:
        cap = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
        rows = _search_rows(sb, source, pattern)
        closed = _has_outcome(sb, [r["id"] for r in rows])
        rows = nearest_first([r for r in rows if str(r["id"]) not in closed], role)[:cap]
    return {"items": _target_items(sb, rows, role), "blocked": None}


# ── Preview (the "Are you sure?" step) ───────────────────────────────────────


def _side(project: dict | None, *, with_stage: bool = False) -> dict | None:
    if project is None:
        return None
    out = {
        "id": project["id"],
        "number": project.get("number"),
        "name": project.get("name"),
        "confirm_text": confirm_text(project),
    }
    if with_stage:
        out["stage"] = project.get("current_stage")
    return out


def _file_item(row: dict) -> dict:
    return {"id": row["id"], "filename": row.get("filename"), "category": row.get("category")}


def preview(sb, source_id: str, target_id, *, budget_s: float = HASH_BUDGET_S) -> dict:
    """What the merge would do (docs/PROJECT_MERGE.md 5). Hashes documents
    within the budget first, so it may answer `ready: false` with progress
    (`hashed` of `total`); the modal calls again until `ready`. A `blocked`
    answer is final (nothing is hashed). File lists are capped at LIST_CAP,
    with totals. `files_skipped` holds the documents the target already has
    (`reason: duplicate`, `same_as_filename` set) and the old project's own
    work documents that stay with it (`reason: in_use`, no
    `same_as_filename`)."""
    source, target, blocked = _resolve(sb, source_id, target_id)
    out: dict[str, Any] = {
        "ready": False,
        "hashed": 0,
        "total": 0,
        "source": _side(source),
        "target": _side(target, with_stage=True),
        "gcs_moving": [],
        "gcs_already": [],
        "files_moving": [],
        "files_moving_total": 0,
        "files_skipped": [],
        "files_skipped_total": 0,
        "files_unreadable": [],
        "files_unreadable_total": 0,
        "rfp_links": 0,
        "blocked": blocked,
    }
    if blocked is not None:
        return out
    plan = file_plan(sb, source["id"], target["id"], budget_s=budget_s)
    source_links = _links(sb, [source["id"]])
    target_gc_ids = {str(link.get("gc_id")) for link in _links(sb, [target["id"]])}
    names = _gc_names(sb, {link.get("gc_id") for link in source_links})
    moving_gcs, already_gcs = gc_plan(source, source_links, target_gc_ids, names)
    skipped = [
        {**_file_item(c), "same_as_filename": same.get("filename"), "reason": "duplicate"}
        for c, same in plan.skipped
    ] + [{**_file_item(k), "same_as_filename": None, "reason": "in_use"} for k in plan.kept]
    out.update({
        "ready": plan.ready,
        "hashed": plan.hashed,
        "total": plan.total,
        "gcs_moving": moving_gcs,
        "gcs_already": already_gcs,
        "files_moving": [_file_item(r) for r in plan.moving[:LIST_CAP]],
        "files_moving_total": len(plan.moving),
        "files_skipped": skipped[:LIST_CAP],
        "files_skipped_total": len(skipped),
        "files_unreadable": [
            {"id": r["id"], "filename": r.get("filename")} for r in plan.unreadable[:LIST_CAP]
        ],
        "files_unreadable_total": len(plan.unreadable),
        "rfp_links": rfp_link_count(sb, source["id"]),
    })
    return out


# ── Merge ────────────────────────────────────────────────────────────────────


def _handoff_locked(project_id: str) -> bool:
    from app.routers.files import handoff_locked

    return bool(handoff_locked(project_id))


def _missing_object(exc: Exception) -> bool:
    return isinstance(exc, StorageApiError) and file_hashes.storage_error_missing(exc)


def remove_objects(paths: list[str]) -> None:
    """Best effort: a copy that outlives a failed merge is an orphan under the
    target's prefix, never a broken row."""
    for path in paths:
        try:
            storage.delete_file(path)
        except Exception:  # noqa: BLE001
            logger.exception("project merge: could not remove the copy %s", path)


def copy_documents(source: dict, target: dict, rows: list[dict], *, locked: bool) -> tuple[list[dict], list[str]]:
    """Copy each moving document's object (and its preview derivative) under
    the target's prefix, server side. Returns (the `p_files` payload, the
    paths written). An object storage says is missing moves without bytes
    (it cannot be read on either project); any other failure removes the
    copies made so far and raises copy_failed."""
    from app.services import office_preview

    payload: list[dict] = []
    copies: list[str] = []
    try:
        for row in rows:
            category, doc_type = landing(row, locked)
            path = storage.build_object_path(target["id"], category, row.get("filename") or "file")
            try:
                storage.copy_file(row.get("storage_path") or "", path)
                copies.append(path)
            except Exception as exc:  # noqa: BLE001
                if not _missing_object(exc):
                    raise
                logger.warning("project merge: %s has no stored object; it moves without bytes", row.get("id"))
            preview_path = None
            if row.get("preview_path"):
                candidate = office_preview.preview_object_path(target["id"], str(row["id"]))
                try:
                    storage.copy_file(row["preview_path"], candidate)
                    copies.append(candidate)
                    preview_path = candidate
                except Exception as exc:  # noqa: BLE001
                    if not _missing_object(exc):
                        raise
            payload.append({
                "file_id": row["id"],
                "storage_path": path,
                "preview_path": preview_path,
                "category": category,
                "doc_type": doc_type,
                "note": merge_note(source, row.get("note")),
            })
    except Exception as exc:  # noqa: BLE001
        logger.exception("project merge: copying documents to %s failed", target.get("id"))
        remove_objects(copies)
        raise _error("copy_failed") from exc
    return payload, copies


def _actor_name(sb, actor_id: str | None) -> str | None:
    if not actor_id:
        return None
    rows = (sb.table("profiles").select("id, full_name").eq("id", actor_id).limit(1).execute()).data or []
    return rows[0].get("full_name") if rows else None


def stage_owner_roles(stage: str | None) -> list[Role]:
    """The internal roles that own the target's current stage (never the
    external estimator); the Estimating Admin when the stage names none."""
    from app.services import workflow

    defn = workflow.STAGES.get(str(stage or ""))
    roles = [r for r in (defn.owner_roles if defn else ()) if r in INTERNAL_ROLES]
    return list(dict.fromkeys(roles)) or [Role.ESTIMATING_ADMIN]


def _notify(sb, source: dict, target: dict, result: dict, actor_id: str) -> None:
    """One bell row per owner of the target's current stage, the way the RFP
    matcher's merge notice is sent (bell only, no email mirror)."""
    try:
        message = notify_message(
            _actor_name(sb, actor_id), source, result.get("gcs_moved") or [],
            int(result.get("files_moved") or 0),
        )
        for role in stage_owner_roles(target.get("current_stage")):
            notify_role(
                role, target["id"], NOTIFY_TYPE, message, mirror_email=False,
                metadata={
                    "archive_id": result.get("archive_id"),
                    "source_project_id": source["id"],
                    "gcs_moved": len(result.get("gcs_moved") or []),
                    "files_moved": int(result.get("files_moved") or 0),
                },
            )
    except Exception:  # noqa: BLE001 - the merge stands; the bell is a courtesy
        logger.exception("project merge: notification failed for %s", target.get("id"))


def _after(
    sb, source: dict, target: dict, result: dict, payload: list[dict], actor_id: str, *,
    landed: list[tuple[str, str | None]] | None = None, no_drawings_before: bool = False,
) -> None:
    """Side effects once the transaction committed, each best effort: the
    archive's usual `project.delete` audit row (reason merged), the
    `project.merge` row on the target, the bell, the BuildingConnected
    files flag a moved drawing or specification satisfies (as an upload
    would), and the target's missing-files items a moved file of the same
    name resolves (docs/RFP_MISSING_FILES.md 4; `landed` is `(category,
    filename)` per moved document, `no_drawings_before` the target's state
    read before the transaction)."""
    try:
        audit(actor_id, project_delete.AUDIT_DELETE, "project", source["id"], {
            "archive_id": result.get("archive_id"),
            "reason": project_delete.REASON_MERGED,
            "note": None,
            "number": source.get("number"),
            "name": source.get("name"),
            "from_rfp": True,
            "row_total": result.get("row_total"),
            "merged_into": target["id"],
        })
    except Exception:  # noqa: BLE001
        logger.exception("project merge: delete audit failed for %s", source.get("id"))
    try:
        audit(actor_id, AUDIT_MERGE, "project", target["id"], {
            "archive_id": result.get("archive_id"),
            "source_project_id": source["id"],
            "source_number": source.get("number"),
            "source_name": source.get("name"),
            "gcs_moved": result.get("gcs_moved") or [],
            "gcs_already": result.get("gcs_already") or [],
            "files_moved": int(result.get("files_moved") or 0),
            "files_skipped": int(result.get("files_skipped") or 0),
            "rfp_links_moved": int(result.get("rfp_links_moved") or 0),
        })
    except Exception:  # noqa: BLE001
        logger.exception("project merge: audit failed for %s", target.get("id"))
    _notify(sb, source, target, result, actor_id)
    try:
        from app.services import files_needed

        for category in sorted({p["category"] for p in payload} & set(files_needed.CLEAR_CATEGORIES)):
            files_needed.clear_if_satisfied(sb, target["id"], category, actor_id)
    except Exception:  # noqa: BLE001
        logger.exception("project merge: files flag check failed for %s", target.get("id"))
    try:
        from app.services import files_needed, rfp_file_issues

        by_category: dict[str, list[str | None]] = {}
        for category, filename in landed or []:
            by_category.setdefault(category, []).append(filename)
        for category in sorted(by_category):
            rfp_file_issues.after_file_insert(
                sb, target["id"], category=category, filenames=by_category[category], actor_id=actor_id,
                first_file=no_drawings_before and category in files_needed.CLEAR_CATEGORIES, by_person=True,
            )
    except Exception:  # noqa: BLE001
        logger.exception("project merge: missing-files check failed for %s", target.get("id"))


def merge(
    sb, source_id: str, *, target_id, actor_id: str, confirm_source: str | None,
    confirm_target: str | None, budget_s: float = HASH_BUDGET_S,
) -> dict:
    """Merge the source into the target (docs/PROJECT_MERGE.md 4.3). The
    document list is decided here from the stored hashes (hashing what is
    still missing within the budget; `files_not_ready` when that is not
    enough); the objects are copied first, then one database transaction
    moves everything and archives the source. `files_changed` from the
    database (the target gained a document since) re-plans and retries once.
    Returns `{archive_id, source, target, gcs_moved, gcs_already,
    files_moved, files_skipped, rfp_links_moved}`."""
    source, target, blocked = _resolve(sb, source_id, target_id)
    if blocked is not None:
        raise ProjectMergeError(blocked["message"], 409, blocked["code"])
    if (confirm_source or "").strip() != confirm_text(source):
        raise _error("source_name_mismatch")
    if (confirm_target or "").strip() != confirm_text(target):
        raise _error("target_name_mismatch")
    locked = _handoff_locked(target["id"])
    # The missing-files first-file rule reads the target BEFORE the move
    # (docs/RFP_MISSING_FILES.md 4); best effort.
    from app.services import rfp_file_issues

    no_drawings_before = rfp_file_issues.no_drawings_yet(sb, target["id"])
    result: dict | None = None
    payload: list[dict] = []
    landed: list[tuple[str, str | None]] = []
    # The source's open missing-files items move to the target BEFORE the
    # transaction archives the source (its rows cascade with it), and move
    # back when the merge then fails (docs/RFP_MISSING_FILES.md 10). Best
    # effort both ways.
    moved_items: list[str] | None = None
    try:
        for attempt in (1, 2):
            plan = file_plan(sb, source["id"], target["id"], budget_s=budget_s)
            if not plan.ready:
                raise _error("files_not_ready")
            payload, copies = copy_documents(source, target, plan.moving, locked=locked)
            # One payload entry per moving row, in order (copy_documents).
            landed = [(p.get("category"), r.get("filename")) for p, r in zip(payload, plan.moving)]
            if moved_items is None:
                moved_items = rfp_file_issues.move_open_items(sb, source["id"], target["id"])
            try:
                result = sb.rpc("merge_rfp_project", {
                    "p_source": source["id"],
                    "p_target": target["id"],
                    "p_actor": actor_id,
                    "p_confirm_source": (confirm_source or "")[:project_delete.CONFIRM_MAX_CHARS],
                    "p_confirm_target": (confirm_target or "")[:project_delete.CONFIRM_MAX_CHARS],
                    "p_files": payload,
                }).execute().data or {}
            except APIError as exc:
                remove_objects(copies)
                err = _db_error(exc)
                if err.code == "files_changed" and attempt == 1:
                    logger.info("project merge: documents changed under %s; re-planning once", source["id"])
                    continue
                raise err from exc
            except Exception:
                remove_objects(copies)
                raise
            break
    except Exception:
        if moved_items:
            rfp_file_issues.move_items_back(sb, moved_items, target["id"], source["id"])
        raise
    result = result or {}
    _after(sb, source, target, result, payload, actor_id, landed=landed, no_drawings_before=no_drawings_before)
    return {
        "archive_id": result.get("archive_id"),
        "source": result.get("source") or {k: source.get(k) for k in ("id", "number", "name")},
        "target": result.get("target") or {k: target.get(k) for k in ("id", "number", "name")},
        "gcs_moved": result.get("gcs_moved") or [],
        "gcs_already": result.get("gcs_already") or [],
        "files_moved": int(result.get("files_moved") or 0),
        "files_skipped": int(result.get("files_skipped") or 0),
        "rfp_links_moved": int(result.get("rfp_links_moved") or 0),
    }

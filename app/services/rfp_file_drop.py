"""The missing-files drop zone on a project (docs/RFP_MISSING_FILES.md 5).

A person fetches what the harvest could not (from the share link, the
email, the portal) and drops it on the project page. One file per request
(the request-body cap is per request, the file cap per file); the browser
sends a drop's files in sequence, passing the `job_id` the first PDF's
answer returned so the batch shares one Bid File Splitter job.

Routing, per file (`route_for`, pure):

- a PDF (extension AND magic) goes to the splitter on the project's drop
  job (`bid_split_jobs.source = 'project'`, linked to the project from
  birth) when the splitter is on and configured, its model is serving, the
  page count is within the cap and the hand-off package has not sent; the
  segments are then filed into the project by trade by the splitter's own
  re-file (`rfp_split.resync_after_run` -> `promote_split_file`);
- any other PDF lands whole (`specification` when the name says so, else
  `drawing`) with a note saying why;
- a Word or Excel file (extension AND its container magic) lands whole
  (`specification` when the name says so, else `other`);
- once the package has sent, every whole file lands under `additional`
  with its note: the upload policy freezes the initial package blocks
  then, and post-hand-off material goes in as an update (build decision,
  section 13.1);
- anything else is a 415.

Whole files go through `routers/files.add_team_file` (the upload route's
policy and insert, as the person), splitter files through
`bid_split.check_upload`'s rules, `bid_split.stage_upload` and
`bid_split.dispatch` (the splitter route's own). Items the dropped name
matches are resolved before the file is stored, so a PDF headed for the
splitter (whose segments land minutes later under trade names) clears its
item at drop time; a store that then fails reopens them.

Sync throughout: the route runs `drop` in the threadpool.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.config import Settings, get_settings
from app.services import bid_split, llm, rfp_file_issues, rfp_split, storage
from app.services.notifications import audit

logger = logging.getLogger(__name__)

ROUTE_SPLITTER = "splitter"
ROUTE_WHOLE = "whole"

KIND_PDF = "pdf"
KIND_OFFICE = "office"

PDF_MAGIC = b"%PDF-"
OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")   # .doc / .xls (OLE compound file)
ZIP_MAGIC = b"PK\x03\x04"                         # .docx / .xlsx (OOXML container)
_OFFICE_MAGIC = {".doc": OLE_MAGIC, ".xls": OLE_MAGIC, ".docx": ZIP_MAGIC, ".xlsx": ZIP_MAGIC}

CATEGORY_DRAWING = "drawing"
CATEGORY_SPECIFICATION = "specification"
CATEGORY_OTHER = "other"
CATEGORY_ADDITIONAL = "additional"

# A drop job takes more files while it is processing or for this long after
# it settled; otherwise the next drop opens a new one.
DROP_JOB_REUSE_SECONDS = 600

MSG_UNSUPPORTED = "{name}: the drop zone accepts PDF, Word and Excel files"
NOTE_WHOLE_PDF = "Added whole from the missing-files drop zone: {why}."
NOTE_OFFICE = "Added from the missing-files drop zone. Check the category."
WHY_SPLITTER_OFF = "the Bid File Splitter is turned off on this server"
WHY_OVER_CAP = "the PDF has {pages} pages, over the splitter's limit of {cap} pages per file"
WHY_PACKAGE_SENT = "this project's package has already been sent, so it was added under Additional files"
MSG_SPLITTER_AWAY = "The Bid File Splitter cannot run right now because {why}."

# "The name says so" (section 5): spec / specification(s) / specs at the
# start of a word, "project manual", "division", or a leading CSI section
# number ("26 05 19"). Underscores, dots and dashes read as spaces first.
_SPEC_RE = re.compile(r"\bspec|\bproject manual\b|\bdivision\b|^\s*\d{2} \d{2} \d{2}\b")


class DropRefused(Exception):
    """The drop cannot take this file; `str(exc)` is the sentence and
    `status_code` the HTTP answer."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Route:
    kind: str                 # splitter | whole
    category: str | None      # the project_files category of a whole file
    note: str | None          # the project_files note of a whole file
    why: str | None = None    # why a PDF did not go to the splitter


def looks_like_spec(name: Any) -> bool:
    """Case-insensitive: `spec...` at the start of a word, `project manual`,
    `division`, or a leading `NN NN NN` section number."""
    if not isinstance(name, str):
        return False
    text = re.sub(r"[_.\-]+", " ", name.lower())
    return bool(_SPEC_RE.search(text))


def file_kind(filename: str | None, magic: bytes) -> str | None:
    """`pdf` or `office` when the extension AND the magic bytes agree, else
    None (the 415)."""
    ext = os.path.splitext((filename or "").lower())[1]
    head = bytes(magic or b"")
    if ext == ".pdf" and head.startswith(PDF_MAGIC):
        return KIND_PDF
    expected = _OFFICE_MAGIC.get(ext)
    if expected is not None and head.startswith(expected):
        return KIND_OFFICE
    return None


def route_for(
    filename: str, content_type: str | None, magic: bytes, pages: int | None, settings: Settings, *,
    splitter_available: bool, package_sent: bool, unavailable_why: str | None = None,
) -> Route:
    """Where one dropped file goes (pure). `content_type` is the client's
    declared type, never trusted (the extension and the magic decide); kept
    so the signature reads like the upload paths. `pages` is the PDF's page
    count when the caller computed it (only when the splitter could take
    it). Raises DropRefused (415) for anything that is not a PDF, Word or
    Excel file."""
    kind = file_kind(filename, magic)
    if kind is None:
        raise DropRefused(MSG_UNSUPPORTED.format(name=filename), 415)
    spec = looks_like_spec(filename)
    if kind == KIND_OFFICE:
        category = CATEGORY_ADDITIONAL if package_sent else (CATEGORY_SPECIFICATION if spec else CATEGORY_OTHER)
        return Route(ROUTE_WHOLE, category, NOTE_OFFICE)
    cap = int(settings.bid_split_max_pages_per_file)
    why: str | None = None
    if package_sent:
        why = WHY_PACKAGE_SENT
    elif not splitter_available:
        why = unavailable_why or WHY_SPLITTER_OFF
    elif pages is not None and pages > cap:
        why = WHY_OVER_CAP.format(pages=pages, cap=cap)
    if why is None:
        return Route(ROUTE_SPLITTER, None, None)
    if package_sent:
        category = CATEGORY_ADDITIONAL
    else:
        category = CATEGORY_SPECIFICATION if spec else CATEGORY_DRAWING
    return Route(ROUTE_WHOLE, category, NOTE_WHOLE_PDF.format(why=why), why)


def splitter_state(settings: Settings | None = None) -> tuple[bool, str | None]:
    """`(available, why not)`: the splitter is on (BID_FILE_SPLITTER_ENABLED),
    configured for `bid_split`, and its model is not away. The sentence is
    for the page ("PDFs will be filed whole because ...")."""
    s = settings or get_settings()
    if not getattr(s, "bid_file_splitter_enabled", False):
        return False, rfp_split.MSG_SPLIT_OFF
    try:
        configured = llm.is_configured("bid_split", s)
    except Exception:  # noqa: BLE001 - an unreadable route reads as unconfigured
        configured = False
    if not configured:
        return False, MSG_SPLITTER_AWAY.format(why=rfp_split._MODEL_AWAY_WHY["unconfigured"])
    away = rfp_split.model_away(s)
    if away:
        why = rfp_split._MODEL_AWAY_WHY.get(str(away[0]), "its AI model is offline")
        return False, MSG_SPLITTER_AWAY.format(why=why)
    return True, None


def _why_lowercase(sentence: str | None) -> str | None:
    """The page's sentence as the note's clause ("the Bid File Splitter
    cannot run right now because ...")."""
    if not sentence:
        return None
    text = sentence.strip().rstrip(".")
    return text[:1].lower() + text[1:]


# ── The drop job ─────────────────────────────────────────────────────────────


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _reusable(job: dict, project_id: str, now: datetime) -> bool:
    if str(job.get("project_id") or "") != project_id or job.get("source") != rfp_split.SOURCE_PROJECT:
        return False
    if job.get("status") == "processing":
        return True
    ended = _parse_ts(job.get("completed_at")) or _parse_ts(job.get("updated_at"))
    return ended is not None and now - ended <= timedelta(seconds=DROP_JOB_REUSE_SECONDS)


def drop_job(
    sb, project_id: str, user_id: str | None, job_id: str | None, settings: Settings,
) -> tuple[dict, bool]:
    """`(job, opened)`: the job this file joins, `job_id` when it is this
    project's drop job, still processing or settled within
    DROP_JOB_REUSE_SECONDS, and below the per-job file cap (`opened` False);
    otherwise a new `project` job linked to the project (`opened` True)."""
    if job_id:
        try:
            uuid.UUID(str(job_id))
            rows = sb.table("bid_split_jobs").select("*").eq("id", str(job_id)).limit(1).execute().data or []
        except (ValueError, AttributeError, TypeError):
            rows = []
        job = rows[0] if rows else None
        if job is not None and _reusable(job, project_id, datetime.now(timezone.utc)):
            files = sb.table("bid_split_files").select("id").eq("job_id", job["id"]).execute().data or []
            if len(files) < int(settings.bid_split_max_files_per_job):
                return job, False
    try:
        model = llm.active_model("bid_split", settings)
    except Exception:  # noqa: BLE001 - a label for history, never a gate
        model = None
    job = sb.table("bid_split_jobs").insert({
        "status": "processing",
        "file_count": 0,
        "model": model,
        "created_by": user_id,
        "source": rfp_split.SOURCE_PROJECT,
        "project_id": project_id,
    }).execute().data[0]
    return job, True


def _discard_empty_job(sb, job_id: str) -> None:
    """A job this request opened whose first file did not stage: gone again
    (its objects, then the row), so no empty job reads `processing` forever
    (review finding 5). Only a job that still holds no file. Best effort."""
    try:
        if sb.table("bid_split_files").select("id").eq("job_id", job_id).limit(1).execute().data:
            return
    except Exception:  # noqa: BLE001 - unsure: keep it
        logger.exception("rfp files: could not check drop job %s before discarding it", job_id)
        return
    try:
        storage.delete_bid_split_prefix(job_id)
    except Exception:  # noqa: BLE001
        logger.exception("rfp files: could not sweep the objects of drop job %s", job_id)
    try:
        sb.table("bid_split_jobs").delete().eq("id", job_id).execute()
    except Exception:  # noqa: BLE001
        logger.exception("rfp files: could not delete the empty drop job %s", job_id)


# ── The drop ─────────────────────────────────────────────────────────────────


def drop(
    sb, project_id: str, user, filename: str | None, content: bytes, *, job_id: str | None = None,
    background=None, settings: Settings | None = None,
) -> dict:
    """Route and store one dropped file (section 5). Returns `{route,
    job_id, split_file_id, project_file_id, category, resolved}`; raises
    DropRefused (415 / 413 / 409 / 400) or the upload policy's
    HTTPException. Audited `rfp_files.drop`."""
    from app.routers import files as files_router

    s = settings or get_settings()
    name = (filename or "").strip() or "upload"
    kind = file_kind(name, content[:8])
    if kind is None:
        raise DropRefused(MSG_UNSUPPORTED.format(name=name), 415)
    package_sent = bool(files_router.handoff_locked(project_id))
    available, why_not = splitter_state(s)
    pages: int | None = None
    if kind == KIND_PDF and available and not package_sent:
        # The splitter's own validation, never a copy (review finding 16):
        # extension and magic, must open, page cap. Over the cap is not a
        # refusal here: the PDF lands whole and `route_for` says why.
        try:
            pages = bid_split.check_upload(name, content, s)
        except bid_split.UploadRefused as exc:
            if exc.status_code == 413 and exc.pages is not None:
                pages = exc.pages
            else:
                raise DropRefused(str(exc), exc.status_code) from exc
    route = route_for(
        name, None, content[:8], pages, s, splitter_available=available, package_sent=package_sent,
        unavailable_why=_why_lowercase(why_not),
    )
    if route.kind == ROUTE_WHOLE:
        # The upload policy first (the lock): nothing is resolved for a file
        # that is then refused.
        files_router.check_upload_lock(project_id, route.category, route.note)
    resolved = rfp_file_issues.resolve_by_names(sb, project_id, [name], getattr(user, "id", None))
    out = {"route": route.kind, "job_id": None, "split_file_id": None, "project_file_id": None,
           "category": route.category}
    frow: dict | None = None
    try:
        if route.kind == ROUTE_SPLITTER:
            job, opened = drop_job(sb, project_id, user.id, job_id, s)
            try:
                frow = bid_split.stage_upload(sb, job, name, content, pages, user.id)
            except Exception as exc:
                if opened:
                    _discard_empty_job(sb, str(job["id"]))
                if isinstance(exc, bid_split.UploadRefused):
                    raise DropRefused(str(exc), exc.status_code) from exc
                raise
            out.update(job_id=str(job["id"]), split_file_id=str(frow["id"]))
        else:
            # `add_team_file` never raises once its row is in (everything
            # after the insert is best effort), so an exception here means
            # nothing was stored.
            row, more = files_router.add_team_file(
                project_id, route.category, name, content, user, note=route.note, background=background,
                audit_extra={"source": "rfp_files_drop"},
            )
            out["project_file_id"] = str(row["id"])
            resolved += [r for r in more if r not in resolved]
    except Exception:
        # Nothing was stored (a full job, storage trouble, a lock that moved
        # since the check, a failed insert): the up-front name matches go
        # back to open.
        rfp_file_issues.unresolve_name_matches(sb, project_id, resolved)
        raise
    if frow is not None:
        # Staged: the run is queued (or runs inline when the queue is off).
        bid_split.dispatch(background, str(frow["id"]), user.id)
    out["resolved"] = resolved
    try:
        audit(user.id, rfp_file_issues.AUDIT_DROP, rfp_file_issues.AUDIT_ENTITY, project_id, {
            "route": route.kind, "filename": name[:300], "job_id": out["job_id"],
            "project_file_id": out["project_file_id"], "resolved": resolved[:50],
        })
    except Exception:  # noqa: BLE001 - the file is in
        logger.exception("rfp files: drop audit failed for %s", project_id)
    return out

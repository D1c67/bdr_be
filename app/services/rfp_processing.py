"""The /rfp-processing page: every RFP intake row still in flight, classified
into lanes (docs/RFP_PROCESSING.md sections 2 and 3).

Pure where it can be: `classify_row` decides a row's lane and stuck reason
from the row alone, and `email_item` / `portal_item` / `summarize` /
`select_lane` shape and count already-loaded rows. The two readers
(`load_email_rows`, `load_portal_rows`) are the only database reads of the
page besides the reference lookups the router makes over the page's ids.

In flight, by construction small:

- email rows at a pending step or a human lane, plus `failed` rows updated
  inside the last FAILED_WINDOW_DAYS;
- portal invitations at match, harvest, split, create or review_match.

Finished rows never load. Each source is read in pages of READ_PAGE and
capped at READ_CAP rows (oldest received first, so the rows that have
waited longest are the ones kept); past the cap the counts under-report and
a warning is logged. The union, the lane filter, the sort and the slice
happen in Python.

Nothing here writes, and nothing here returns a mail body, a link, a
`view_url` or a `match_score`: names, subjects and errors are plain text.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from app.services import rfp_email_visibility as visibility
from app.services.rfp_email_ingest import STATUS_HUMAN, STATUS_PENDING
from app.services.rfp_match import parse_dt, pg_ts

logger = logging.getLogger(__name__)

SOURCE_EMAIL = "email"
SOURCE_PORTAL = "portal"

LANE_REVIEW_LLM = "review_llm"
LANE_UNAUTHORIZED = "flagged_unauthorized"
LANE_REVIEW_MATCH = "review_match"
LANE_PROCESSING = "processing"
LANE_STUCK = "stuck"
REVIEW_LANES = (LANE_REVIEW_LLM, LANE_UNAUTHORIZED, LANE_REVIEW_MATCH)
LANES = REVIEW_LANES + (LANE_PROCESSING, LANE_STUCK)
# The `lane` query values of GET /rfp-processing: every lane, `review` (the
# three review lanes together) and `all`.
LANE_FILTERS = ("all", "review") + LANES

STUCK_FAILED = "failed"
STUCK_RETRYING = "retrying"
STUCK_MODEL_WAIT = "model_wait"
STUCK_STALLED = "stalled"
STUCK_KINDS = (STUCK_FAILED, STUCK_RETRYING, STUCK_MODEL_WAIT, STUCK_STALLED)

# The sentence rfp_email_ingest writes into last_error when a later copy of a
# message waits at extract for an older copy to decide (the older wording is
# still on rows written before 2026-09-21). Our own text, never the mail's.
SIBLING_WAIT_PREFIXES = (
    "Waiting for another copy of this message",
    "Waiting for an earlier copy of this message",
)


def is_sibling_wait(last_error) -> bool:
    """True when `last_error` is the pipeline's own sibling wait sentence."""
    text = str(last_error or "").lstrip()
    return any(text.startswith(p) for p in SIBLING_WAIT_PREFIXES)


# The leader id the sibling wait sentence carries in parentheses
# (rfp_email_ingest._sibling_short_circuit writes "... message (<id>) to ...").
_SIBLING_LEADER_RE = re.compile(
    r"\(([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\)", re.IGNORECASE
)


def sibling_wait_leader_id(last_error) -> str | None:
    """The id of the copy a sibling-wait row is parked behind, lowercased, or
    None when `last_error` is not the wait sentence or names no id."""
    if not is_sibling_wait(last_error):
        return None
    m = _SIBLING_LEADER_RE.search(str(last_error))
    return m.group(1).lower() if m else None

EMAIL_FAILED = "failed"
# The portal's statuses (rfp_portal_ingest.STATUS_PENDING / STATUS_HUMAN),
# spelled out so this module stays importable without the portal service.
PORTAL_PENDING = ("match", "harvest", "split", "create")
PORTAL_HUMAN = ("review_match",)
PORTAL_TABLE = "rfp_portal_invitations"

# The automated steps in pipeline order (the summary's `steps`, the page's
# step strip). Every email pending status, which includes the portal ones.
STEPS = tuple(STATUS_PENDING)
# Steps whose work is a long-running job, judged on the slow threshold.
SLOW_STEPS = ("harvest", "split")
# A harvest job in one of these states is the row's progress: never stalled.
HARVEST_ACTIVE = ("pending", "running")

FAILED_WINDOW_DAYS = 14
READ_PAGE = 1000
READ_CAP = 2000
_IN_CHUNK = 200            # ids per `in_` filter (URL length), as in the ingest service
_ERROR_MAX_CHARS = 500     # the pipeline already caps last_error; kept as a backstop

# The columns classify_row reads, for the summary (which never builds items).
EMAIL_CLASSIFY_SELECT = (
    "id, status, attempts, next_attempt_at, last_error, updated_at, decided_at_step, "
    "harvest_id, received_at"
)
# The list: the queue's list columns plus what classify_row and the item need.
# Built from rfp_emails._LIST_SELECT by the router (see `email_list_select`).
_EMAIL_EXTRA_COLUMNS = (
    "attempts", "next_attempt_at", "last_error", "updated_at", "decided_at_step",
)
_PORTAL_SELECT = (
    "id, portal, title, status, flag_reason, decided_at_step, attempts, next_attempt_at, "
    "last_error, updated_at, created_at, first_seen_at, match_project_id, harvest_id, "
    "created_project_id"
)


def email_list_select(list_select: str) -> str:
    """`list_select` (the queue's `_LIST_SELECT`) plus the columns the page
    needs on top, without repeating one it already names."""
    cols = [c.strip() for c in list_select.split(",") if c.strip()]
    for extra in _EMAIL_EXTRA_COLUMNS:
        if extra not in cols:
            cols.append(extra)
    return ", ".join(cols)


# ── Classification (section 3, pure) ─────────────────────────────────────


def _pending_for(source: str) -> tuple[str, ...]:
    return PORTAL_PENDING if source == SOURCE_PORTAL else tuple(STATUS_PENDING)


def _human_for(source: str) -> tuple[str, ...]:
    return PORTAL_HUMAN if source == SOURCE_PORTAL else tuple(STATUS_HUMAN)


def _stuck(kind: str, step, row: dict, max_attempts: int) -> dict:
    return {
        "kind": kind,
        "step": step,
        "attempts": int(row.get("attempts") or 0),
        "max_attempts": int(max_attempts),
        "next_attempt_at": row.get("next_attempt_at"),
        "last_error": _cap_error(row.get("last_error")),
        # When the row last moved: the failure write for failed, the retry or
        # wait write for retrying and model_wait, the last progress for stalled.
        "since": row.get("updated_at"),
    }


def classify_row(
    row: dict,
    *,
    now: datetime,
    source: str,
    max_attempts: int,
    stall_minutes: int,
    slow_stall_minutes: int,
    harvest_status: str | None,
) -> tuple[str, dict | None]:
    """`(lane, stuck | None)` for one in-flight row (section 3), first rule
    that applies wins:

    1. a human lane (review_llm, flagged_unauthorized, review_match): that
       lane, never stuck (a person is the step);
    2. an email row at `failed`: stuck `failed`, step = decided_at_step;
    3. a pending row with attempts >= 1: stuck `retrying`;
    4. a pending row with attempts 0, last_error AND next_attempt_at set:
       stuck `model_wait` (the model is away; nothing was spent), unless the
       error is the sibling wait sentence (a later copy parked behind an
       older copy of the same message), which is `processing`;
    5. a pending row whose updated_at is older than the stall threshold
       (`slow_stall_minutes` for harvest and split, `stall_minutes` for the
       rest) with next_attempt_at null or past: stuck `stalled`, except a
       `harvest` row whose harvest record is pending or running (the job is
       the progress);
    6. otherwise `processing`.

    `harvest_status` is the linked rfp_harvests row's status, or None when
    the row has no harvest yet. A status this module does not know is
    `processing` (never hidden, never called stuck on a guess).
    """
    status = row.get("status")
    if status in _human_for(source):
        return status, None
    if source == SOURCE_EMAIL and status == EMAIL_FAILED:
        return LANE_STUCK, _stuck(STUCK_FAILED, row.get("decided_at_step"), row, max_attempts)
    if status not in _pending_for(source):
        return LANE_PROCESSING, None
    attempts = int(row.get("attempts") or 0)
    if attempts >= 1:
        return LANE_STUCK, _stuck(STUCK_RETRYING, status, row, max_attempts)
    next_at = parse_dt(row.get("next_attempt_at"))
    if is_sibling_wait(row.get("last_error")):
        # A later copy of a message parked at extract behind an older copy
        # (rfp_email_ingest sibling rule): it follows that copy's decision,
        # which is usually a person's in a review lane. Nothing is stuck.
        return LANE_PROCESSING, None
    if row.get("last_error") and row.get("next_attempt_at"):
        return LANE_STUCK, _stuck(STUCK_MODEL_WAIT, status, row, max_attempts)
    if status == "harvest" and harvest_status in HARVEST_ACTIVE:
        return LANE_PROCESSING, None
    minutes = slow_stall_minutes if status in SLOW_STEPS else stall_minutes
    moved = (
        parse_dt(row.get("updated_at"))
        or parse_dt(row.get("received_at"))
        or parse_dt(row.get("created_at"))
    )
    if (
        moved is not None
        and now - moved > timedelta(minutes=max(1, int(minutes)))
        and (next_at is None or next_at <= now)
    ):
        return LANE_STUCK, _stuck(STUCK_STALLED, status, row, max_attempts)
    return LANE_PROCESSING, None


def _cap_error(value) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:_ERROR_MAX_CHARS]


# ── Reads ────────────────────────────────────────────────────────────────


def email_in_flight_filter(now: datetime) -> str:
    """The PostgREST or-group of the email in-flight set: every pending and
    human status, or `failed` updated inside the window. The timestamp is a
    Z-suffixed literal (`rfp_match.pg_ts`): a '+' would URL-decode to a
    space."""
    live = ",".join(tuple(STATUS_PENDING) + tuple(STATUS_HUMAN))
    since = pg_ts(now - timedelta(days=FAILED_WINDOW_DAYS))
    return f"status.in.({live}),and(status.eq.{EMAIL_FAILED},updated_at.gte.{since})"


def _paged(query_factory, label: str) -> list[dict]:
    """Up to READ_CAP rows in READ_PAGE pages, oldest received first."""
    out: list[dict] = []
    offset = 0
    while offset < READ_CAP:
        size = min(READ_PAGE, READ_CAP - offset)
        page = query_factory().range(offset, offset + size - 1).execute().data or []
        out.extend(page)
        if len(page) < size:
            return out
        offset += size
    logger.warning(
        "rfp processing: %s in-flight read hit the %s row cap; counts past it are not shown",
        label, READ_CAP,
    )
    return out


def load_email_rows(sb, user, *, now: datetime, select: str) -> list[dict]:
    """Every in-flight email row this viewer can see, through
    `rfp_email_visibility.apply_scope`. A viewer whose scope is empty gets
    no rows and no query."""
    scope = visibility.visible_mailboxes(user)
    if scope is not None and not scope:
        return []
    flt = email_in_flight_filter(now)

    def query():
        q = sb.table("rfp_emails").select(select).or_(flt)
        return (
            visibility.apply_scope(q, user)
            .order("received_at", desc=False)
            .order("id", desc=False)
        )

    return _paged(query, "email")


def load_portal_rows(sb, *, portals=None) -> list[dict]:
    """Every in-flight portal invitation (no mailbox scope: invitations are
    not mailbox rows). The caller decides whether the viewer gets them.
    `portals`, when given, restricts the read to those portal keys (the
    served ones): an empty set reads nothing at all."""

    served = None if portals is None else sorted({str(p) for p in portals if p})
    if served is not None and not served:
        return []

    def query():
        q = (
            sb.table(PORTAL_TABLE)
            .select(_PORTAL_SELECT)
            .in_("status", list(PORTAL_PENDING + PORTAL_HUMAN))
        )
        if served is not None:
            q = q.in_("portal", served)
        return q.order("first_seen_at", desc=False).order("id", desc=False)

    return _paged(query, "portal")


def _chunks(items: list, size: int = _IN_CHUNK):
    for i in range(0, len(items), size):
        yield items[i: i + size]


def harvest_statuses(sb, ids, lookup) -> dict[str, str]:
    """`{harvest_id: status}` through `lookup` (the queue router's
    `_harvest_status_by_id`), chunked so a long id list never overflows the
    URL. No query when there is nothing to look up."""
    wanted = sorted({str(i) for i in ids if i})
    out: dict[str, str] = {}
    for chunk in _chunks(wanted):
        out.update(lookup(sb, chunk) or {})
    return out


def harvest_ids_to_check(rows: list[dict]) -> set[str]:
    """The harvest ids classify_row needs: rows at `harvest` with a link."""
    return {str(r["harvest_id"]) for r in rows if r.get("status") == "harvest" and r.get("harvest_id")}


# ── Classify every loaded row ────────────────────────────────────────────


def classify_all(
    rows: list[dict], *, source: str, now: datetime, settings, harvests: dict[str, str]
) -> list[tuple[dict, str, dict | None]]:
    """`[(row, lane, stuck)]` for one source's loaded rows."""
    max_attempts = int(getattr(settings, "rfp_email_ingestion_classify_max_attempts", 0) or 0)
    stall = int(getattr(settings, "rfp_processing_stall_minutes", 30) or 30)
    slow = int(getattr(settings, "rfp_processing_slow_stall_minutes", 360) or 360)
    out = []
    for row in rows:
        lane, stuck = classify_row(
            row,
            now=now,
            source=source,
            max_attempts=max_attempts,
            stall_minutes=stall,
            slow_stall_minutes=slow,
            harvest_status=harvests.get(str(row.get("harvest_id") or "")),
        )
        out.append((row, lane, stuck))
    return out


# ── Copies waiting on a review (section 3, rule 4) ───────────────────────


def review_waits(classified: list[tuple[str, dict, str, dict | None]]) -> dict[str, dict]:
    """`{row_id: {"id": leader_id, "lane": leader_lane}}` for every email row
    in the processing lane that is parked behind another copy of the same
    message (the sibling wait sentence) whose copy sits in a review lane: the
    row moves only once a person decides that copy.

    Read off the rows already loaded for this viewer, so no extra query runs
    and a leader outside the viewer's mailbox scope is never described (the
    row then stays a plain processing row)."""
    lanes = {
        str(row.get("id") or "").lower(): lane
        for source, row, lane, _ in classified
        if source == SOURCE_EMAIL
    }
    out: dict[str, dict] = {}
    for source, row, lane, _ in classified:
        if source != SOURCE_EMAIL or lane != LANE_PROCESSING:
            continue
        leader_id = sibling_wait_leader_id(row.get("last_error"))
        if not leader_id:
            continue
        leader_lane = lanes.get(leader_id)
        if leader_lane in REVIEW_LANES:
            out[str(row.get("id"))] = {"id": leader_id, "lane": leader_lane}
    return out


# ── Summary (section 2.1) ────────────────────────────────────────────────


def summarize(classified: list[tuple[str, dict, str, dict | None]], *, portal_served: bool,
              now: datetime) -> dict:
    """The summary over `[(source, row, lane, stuck)]`. `waiting_on_review`
    is the part of the processing lane that waits on a person reviewing
    another copy of the same message (`review_waits`)."""
    lanes = {lane: 0 for lane in LANES}
    steps = {step: 0 for step in STEPS}
    kinds = {kind: 0 for kind in STUCK_KINDS}
    for _source, row, lane, stuck in classified:
        lanes[lane] = lanes.get(lane, 0) + 1
        if lane == LANE_PROCESSING and row.get("status") in steps:
            steps[row["status"]] += 1
        if stuck is not None:
            kinds[stuck["kind"]] = kinds.get(stuck["kind"], 0) + 1
    lanes["review_total"] = sum(lanes[lane] for lane in REVIEW_LANES)
    lanes["total"] = len(classified)
    return {
        "lanes": lanes,
        "steps": steps,
        "stuck_kinds": kinds,
        "waiting_on_review": len(review_waits(classified)),
        "portal_served": bool(portal_served),
        "generated_at": now.isoformat(),
    }


# ── The list (section 2.2) ───────────────────────────────────────────────


def lane_matches(lane_filter: str, lane: str) -> bool:
    if lane_filter == "all":
        return True
    if lane_filter == "review":
        return lane in REVIEW_LANES
    return lane == lane_filter


def _group(lane: str) -> int:
    """Review rows first, then stuck, then processing."""
    if lane in REVIEW_LANES:
        return 0
    if lane == LANE_STUCK:
        return 1
    return 2


_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


def waited_since(source: str, row: dict) -> str | None:
    """The instant the row's wait is measured from: `received_at` for email,
    `first_seen_at` (the portal's "invited" instant) else `created_at` for
    portal invitations."""
    if source == SOURCE_PORTAL:
        return row.get("first_seen_at") or row.get("created_at")
    return row.get("received_at")


def select_lane(classified: list[tuple[str, dict, str, dict | None]], lane_filter: str):
    """The rows in `lane_filter`, sorted: review, stuck, processing; inside a
    group the OLDEST wait first (ties by source, then id, so paging is
    stable)."""
    hits = [c for c in classified if lane_matches(lane_filter, c[2])]
    hits.sort(key=lambda c: (
        _group(c[2]),
        parse_dt(waited_since(c[0], c[1])) or _FAR_FUTURE,
        c[0],
        str(c[1].get("id") or ""),
    ))
    return hits


def email_item(row: dict, lane: str, stuck: dict | None, *, projects: dict, gcs: dict,
               harvests: dict, waiting_on: dict | None = None) -> dict:
    """One email row in the section 2.2 shape. `waiting_on` is the row's
    `review_waits` entry: the copy in a review lane it is parked behind."""
    gc = gcs.get(row.get("resolved_gc_id")) if row.get("resolved_gc_id") else None
    return {
        "source": SOURCE_EMAIL,
        "id": row.get("id"),
        "status": row.get("status"),
        "lane": lane,
        "awaiting_review": lane in REVIEW_LANES,
        "stuck": stuck,
        "name": row.get("extracted_project_name"),
        "subject": row.get("subject"),
        "from_address": row.get("from_address"),
        "from_name": row.get("from_name"),
        "gc_name": (gc or {}).get("name") or row.get("extracted_gc_name"),
        "invitation_method": row.get("invitation_method"),
        "received_at": row.get("received_at"),
        "updated_at": row.get("updated_at"),
        "attempts": int(row.get("attempts") or 0),
        "next_attempt_at": row.get("next_attempt_at"),
        "last_error": _cap_error(row.get("last_error")),
        "harvest_status": _harvest_display(row, harvests),
        "match_project": projects.get(row.get("match_project_id")) if row.get("match_project_id") else None,
        "created_project_id": row.get("created_project_id"),
        "primary_mailbox": row.get("primary_mailbox"),
        "portal": None,
        # Additive to the section 2.2 shape: the model's verdict, for the
        # Detail column of a review_llm row. The same three list columns
        # GET /rfp-emails serves to the same roles; the reasoning is the
        # service's capped, schema-checked text.
        "llm_answer": row.get("llm_answer"),
        "llm_confidence": row.get("llm_confidence"),
        "llm_reasoning": row.get("llm_reasoning"),
        # Additive: the copy this row waits behind while a person reviews it,
        # `{id, lane}`, else null. Only ever a row this viewer can see.
        "waiting_on": waiting_on,
    }


def portal_item(row: dict, lane: str, stuck: dict | None, *, projects: dict,
                harvests: dict) -> dict:
    """One portal invitation in the section 2.2 shape."""
    return {
        "source": SOURCE_PORTAL,
        "id": row.get("id"),
        "status": row.get("status"),
        "lane": lane,
        "awaiting_review": lane in REVIEW_LANES,
        "stuck": stuck,
        "name": row.get("title"),
        "subject": None,
        "from_address": None,
        "from_name": None,
        "gc_name": None,
        "invitation_method": row.get("portal") or "ngem",
        "received_at": waited_since(SOURCE_PORTAL, row),
        "updated_at": row.get("updated_at"),
        "attempts": int(row.get("attempts") or 0),
        "next_attempt_at": row.get("next_attempt_at"),
        "last_error": _cap_error(row.get("last_error")),
        "harvest_status": _harvest_display(row, harvests),
        "match_project": projects.get(row.get("match_project_id")) if row.get("match_project_id") else None,
        "created_project_id": row.get("created_project_id"),
        "primary_mailbox": None,
        "portal": row.get("portal") or "ngem",
        "llm_answer": None,
        "llm_confidence": None,
        "llm_reasoning": None,
        "waiting_on": None,
    }


def _harvest_display(row: dict, harvests: dict) -> str | None:
    """The queue's convention: `pending` for a row at `harvest` that has no
    harvest record yet, else the record's status (null when unlinked)."""
    if row.get("status") == "harvest" and not row.get("harvest_id"):
        return "pending"
    if not row.get("harvest_id"):
        return None
    return harvests.get(str(row["harvest_id"]))

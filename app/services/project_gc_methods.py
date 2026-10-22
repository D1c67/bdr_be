"""How each GC on a project invited us (migration 0173).

The project header shows one pill per GC and method, and the GCs box lists
every method of every GC, so both read the per-GC `methods` list that
`proposal_send.project_gc_rows` adds to the GC list wire shape. A GC's
methods are the union, in this order, of:

1. `rfp_created`: the RFP that created the project (rfp_created_projects),
   credited to the GC that creation attached or inferred: the email's
   resolved GC, the portal invitation's GC, the likely GC, or (plan
   `created`, where the new GC's id is stored nowhere) the GC rows inserted
   in the minutes the creation ran;
2. `rfp_merge`: every live merge of an RFP onto the project for that GC
   (rfp_project_matches, kind `merged`, not unmerged), oldest first;
3. `portal`: the BuildingConnected (or other portal) invitations resolved to
   the project for that GC (rfp_portal_invitations at `exists`, or the one
   at `created` that made it);
4. `user`: the method a person set on the GCs box (project_gcs.
   invitation_method).

Each key appears once, carrying every source that produced it. A GC with
none of the above reads as `by_hand` (source `default`). The RFP reads ride
the master RFP switch (and each portal its own switch), the same way the
merge read in project_gc_rows does, so a deployment without those tables
never queries them; a failed read degrades to the user-set method rather
than failing the GC list every membership write returns.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

METHOD_BY_HAND = "by_hand"
METHOD_OTHER = "other"

SOURCE_RFP_CREATED = "rfp_created"
SOURCE_RFP_MERGE = "rfp_merge"
SOURCE_PORTAL = "portal"
SOURCE_USER = "user"
SOURCE_DEFAULT = "default"

# What a person may set on the GCs box: the email invitation methods
# (rfp_email_auth.INVITATION_METHODS, kept in step by a test), the two
# portals, and a catch-all. "By hand" is not a value: it is NULL, the
# default. Validated here, not by a DB CHECK (0173 only checks the shape),
# so a new method needs no migration.
USER_SETTABLE_METHODS: tuple[str, ...] = (
    "organic",
    "procore",
    "pipelinesuite",
    "smartbid",
    "gc_portal",
    "general",
    "nonorganic",
    "buildingconnected",
    "ngem",
    METHOD_OTHER,
)

# A portal row whose `invitation_method` is empty is NGEM (the first portal,
# from before the key was stored); the frontend labels it the same way.
_PORTAL_DEFAULT = "ngem"
_PORTAL_FLAGS = {"ngem": "rfp_ngem_enabled", "buildingconnected": "rfp_bc_enabled"}

# Plan `created`: the creation step inserts the GC right after its own
# rfp_created_projects row, so a GC on the project created inside this
# window is the one it made. Generous on purpose: a GC created that close to
# an RFP-made project by hand is vanishingly rare.
_CREATED_WINDOW_BEFORE = timedelta(seconds=60)
_CREATED_WINDOW_AFTER = timedelta(minutes=10)


def normalize_user_method(value: str | None) -> str | None:
    """The stored form of a user-set method: trimmed and lowercased, empty
    or `by_hand` = None (the default). Raises ValueError for a key outside
    USER_SETTABLE_METHODS."""
    if value is None:
        return None
    key = str(value).strip().lower()
    if not key or key == METHOD_BY_HAND:
        return None
    if key not in USER_SETTABLE_METHODS:
        raise ValueError(f"Unknown invitation method: {key}")
    return key


def _text(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def created_method(created: dict | None) -> str | None:
    """The method key of the RFP that created the project: the email's
    method, or the portal key (empty = NGEM)."""
    if not created:
        return None
    method = _text(created.get("invitation_method"))
    if created.get("source_kind") == "rfp_portal":
        return (method or _PORTAL_DEFAULT).lower()
    return method.lower() if method else None


def gcs_created_by_step(created: dict | None, gc_created_at: dict[str, str | None]) -> set[str]:
    """Plan `created`: the GCs whose directory row was inserted while the
    creation ran (see _CREATED_WINDOW_*). Empty for any other plan."""
    if not created or created.get("gc_plan") != "created":
        return set()
    anchor = _parse_ts(created.get("created_at"))
    if anchor is None:
        return set()
    out: set[str] = set()
    for gc_id, raw in gc_created_at.items():
        at = _parse_ts(raw)
        if at is not None and anchor - _CREATED_WINDOW_BEFORE <= at <= anchor + _CREATED_WINDOW_AFTER:
            out.add(gc_id)
    return out


def derive_methods(
    gc_ids: list[str],
    *,
    created: dict | None = None,
    created_gc_ids: set[str] | frozenset[str] = frozenset(),
    merges: list[dict] | tuple = (),
    portal_rows: list[dict] | tuple = (),
    user_set: dict[str, str | None] | None = None,
) -> dict[str, list[dict]]:
    """{gc_id: [{key, sources}]} for every GC id given (pure; see the module
    doc for the order). `merges` are live merged rfp_project_matches rows
    ({gc_id, invitation_method, decided_at}); `portal_rows` are
    {gc_id, portal}; `user_set` maps gc_id to the stored user method."""
    on_project = set(gc_ids)
    found: dict[str, dict[str, list[str]]] = {gid: {} for gid in gc_ids}

    def add(gc_id, key, source):
        key = _text(key)
        if not gc_id or gc_id not in on_project or not key:
            return
        sources = found[gc_id].setdefault(key.lower(), [])
        if source not in sources:
            sources.append(source)

    method = created_method(created)
    if method:
        for gc_id in sorted(created_gc_ids):
            add(gc_id, method, SOURCE_RFP_CREATED)
    for m in sorted(merges, key=lambda r: (str(r.get("decided_at") or ""), str(r.get("id") or ""))):
        add(m.get("gc_id"), m.get("invitation_method"), SOURCE_RFP_MERGE)
    for p in portal_rows:
        add(p.get("gc_id"), p.get("portal") or _PORTAL_DEFAULT, SOURCE_PORTAL)
    for gc_id, key in (user_set or {}).items():
        add(gc_id, key, SOURCE_USER)

    out: dict[str, list[dict]] = {}
    for gc_id in gc_ids:
        methods = [{"key": k, "sources": s} for k, s in found[gc_id].items()]
        out[gc_id] = methods or [{"key": METHOD_BY_HAND, "sources": [SOURCE_DEFAULT]}]
    return out


# ── Reads ────────────────────────────────────────────────────────────────


def _created_row(sb, project_id: str) -> dict | None:
    rows = (
        sb.table("rfp_created_projects")
        .select(
            "source_kind, invitation_method, gc_plan, likely_gc_id, rfp_email_id,"
            " portal_invitation_id, created_at"
        )
        .eq("project_id", project_id)
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else None


def _created_gc_ids(sb, created: dict, gc_created_at: dict[str, str | None]) -> set[str]:
    """The GC(s) the creating RFP stands behind (see the module doc)."""
    ids: set[str] = set()
    if created.get("likely_gc_id"):
        ids.add(str(created["likely_gc_id"]))
    if created.get("rfp_email_id"):
        rows = (
            sb.table("rfp_emails").select("resolved_gc_id")
            .eq("id", created["rfp_email_id"]).limit(1).execute()
        ).data or []
        if rows and rows[0].get("resolved_gc_id"):
            ids.add(str(rows[0]["resolved_gc_id"]))
    if created.get("portal_invitation_id"):
        rows = (
            sb.table("rfp_portal_invitations").select("gc_id")
            .eq("id", created["portal_invitation_id"]).limit(1).execute()
        ).data or []
        if rows and rows[0].get("gc_id"):
            ids.add(str(rows[0]["gc_id"]))
    return ids | gcs_created_by_step(created, gc_created_at)


def _portal_any_enabled(settings) -> bool:
    return any(bool(getattr(settings, flag, False)) for flag in _PORTAL_FLAGS.values())


def _portal_rows(sb, project_id: str, settings) -> list[dict]:
    """{gc_id, portal} of the invitations resolved to the project (at
    `exists`) or that created it (at `created`), with a GC, for the portals
    whose switch is on."""
    rows: list[dict] = []
    for status, column in (("exists", "match_project_id"), ("created", "created_project_id")):
        rows.extend(
            (
                sb.table("rfp_portal_invitations")
                .select("gc_id, portal")
                .eq("status", status)
                .eq(column, project_id)
                .execute()
            ).data or []
        )
    out = []
    for r in rows:
        flag = _PORTAL_FLAGS.get(str(r.get("portal") or ""))
        if r.get("gc_id") and flag and getattr(settings, flag, False):
            out.append(r)
    return out


def methods_for_project(
    sb,
    project_id: str,
    gcs: list[dict],
    merges: list[dict],
    settings,
) -> dict[str, list[dict]]:
    """{gc_id: [{key, sources}]} for the GC rows of one project. `gcs` is
    [{id, created_at, invitation_method}] (the directory row's created_at,
    the link row's user-set method); `merges` are the live merged
    rfp_project_matches rows project_gc_rows already read (empty while RFP
    ingestion is off)."""
    gc_ids = [g["id"] for g in gcs]
    user_set = {g["id"]: g.get("invitation_method") for g in gcs if g.get("invitation_method")}
    created: dict | None = None
    created_ids: set[str] = set()
    portal: list[dict] = []
    if getattr(settings, "rfp_ingest_enabled", False):
        try:
            created = _created_row(sb, project_id)
            if created:
                created_ids = _created_gc_ids(
                    sb, created, {g["id"]: g.get("created_at") for g in gcs}
                )
            if _portal_any_enabled(settings):
                portal = _portal_rows(sb, project_id, settings)
        except Exception:  # noqa: BLE001 - the GC list must still load
            logger.exception("project gc methods: RFP reads failed for %s", project_id)
            created, created_ids, portal = None, set(), []
    return derive_methods(
        gc_ids,
        created=created,
        created_gc_ids=created_ids,
        merges=merges,
        portal_rows=portal,
        user_set=user_set,
    )

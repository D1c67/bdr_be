"""Project Management activation — the seam between bidding and PM.

A bid recorded as WON automatically enters PM at Preconstruction, seeded from
what the bid already knows (winning GC → customer, the amount we bid that GC →
original contract value, est dates → planned dates). Activation is idempotent:
re-recording an outcome, correcting lost→won, or a racing double-submit can
never double-activate (pm_details.project_id is unique and the projects update
is optimistically locked on pm_stage IS NULL).

Correcting an outcome AWAY from won is handled by the caller (services/outcome)
using the predicates here: while the PM record is still an untouched Precon
shell it is auto-retracted (the fat-finger case); once any PM work exists the
correction is refused — daily logs and change orders must not be stranded by a
data-entry edit.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.services.notifications import audit, dismiss_notifications, notify_role
from app.services.pm_workflow import append_pm_stage_event

logger = logging.getLogger(__name__)

# Tables whose rows constitute "PM work" for the retraction check. The module
# tables ship in later migrations (0058–0060); a table missing from the schema
# means that module cannot have data yet, which the probe treats as empty.
_PM_CHILD_TABLES = (
    "pm_documents",
    "change_orders",
    "sov_lines",
    "pay_applications",
    "pm_milestones",
    "daily_logs",
    "rfis",
    "manpower_entries",
    # Special-cased in pm_activity_exists: seeded-from-BOQ rows are created by
    # the activation itself, so only source='manual' rows count as work.
    "pm_materials",
)


def _is_missing_table(exc: Exception) -> bool:
    """Strictly 'this table is not in the schema' — the structured PostgREST /
    Postgres codes first, then their exact message shapes. Deliberately NOT a
    loose 'schema cache' match: PGRST002 ('could not query the database for the
    schema cache', a transient blip during post-DDL reloads) must NOT be treated
    as missing — callers use this fail-open ('no table → no data'), and a
    transient error mistaken for a missing table could green-light retracting a
    PM record that has real work."""
    code = getattr(exc, "code", None)
    if code in ("PGRST205", "42P01"):
        return True
    msg = str(exc).lower()
    return "could not find the table" in msg or "relation" in msg and "does not exist" in msg


def _is_duplicate_pm_details(exc: Exception) -> bool:
    msg = str(exc)
    return "pm_details" in msg and ("23505" in msg or "duplicate" in msg.lower())


def _winning_bid_amount(project_id: str, winning_gc_id: str | None) -> Decimal | None:
    """The number we bid the winning GC — the natural seed for the original
    contract value. Prefer the per-GC outcome snapshot (taken at record time);
    fall back to recomputing from the sent proposal; None when unknowable
    (winner unknown, or legacy sends without amounts) — PATCHable later."""
    if not winning_gc_id:
        return None
    sb = get_supabase()
    rows = (
        sb.table("bid_gc_outcomes")
        .select("our_amount")
        .eq("project_id", project_id)
        .eq("gc_id", winning_gc_id)
        .execute()
    ).data or []
    if rows and rows[0].get("our_amount") is not None:
        return Decimal(str(rows[0]["our_amount"]))
    sent = (
        sb.table("proposal_sends")
        .select("material_amount, labor_amount")
        .eq("project_id", project_id)
        .eq("gc_id", winning_gc_id)
        .eq("status", "sent")
        .execute()
    ).data or []
    if sent:
        from app.services.outcome import our_amount_of  # lazy: outcome imports pm

        return our_amount_of(sent[0].get("material_amount"), sent[0].get("labor_amount"))
    return None


def _gc_name(gc_id: str | None) -> str | None:
    if not gc_id:
        return None
    rows = (
        get_supabase()
        .table("general_contractors")
        .select("name")
        .eq("id", gc_id)
        .execute()
    ).data or []
    return rows[0]["name"] if rows else None


def seed_pm_materials_from_boq(project_id: str) -> int:
    """Copy the bid's materials into pm_materials so PM starts with exactly
    what the BOQ extraction returned. Prefers the confirmed set
    (rfq_line_items — already category-mapped by the PE at BOQ confirm); falls
    back to the latest done extraction for a bid won without confirming, where
    group names match categories by name and the rest stay uncategorized with
    the group name kept as category_label. No-op when any rows already exist
    (a correction cycle that left materials behind must not duplicate them).
    Returns the number of rows inserted."""
    sb = get_supabase()
    try:
        existing = (
            sb.table("pm_materials")
            .select("id")
            .eq("project_id", project_id)
            .limit(1)
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001 — module migration not applied yet
        if _is_missing_table(exc):
            return 0
        raise
    if existing:
        return 0

    rows: list[dict] = []
    rfqs = (
        sb.table("rfqs")
        .select("id, material_category_id")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    if rfqs:
        cat_of = {r["id"]: r["material_category_id"] for r in rfqs}
        items = (
            sb.table("rfq_line_items")
            .select("rfq_id, site_name, description, quantity, unit, notes, sort_order")
            .in_("rfq_id", list(cat_of))
            .execute()
        ).data or []
        items.sort(key=lambda it: it.get("sort_order") or 0)
        for i, it in enumerate(items):
            rows.append(
                {
                    "project_id": project_id,
                    "material_category_id": cat_of.get(it["rfq_id"]),
                    "site_name": it.get("site_name"),
                    "description": (it.get("description") or "").strip(),
                    "quantity": it.get("quantity"),
                    "unit": it.get("unit"),
                    "notes": it.get("notes"),
                    "source": "boq",
                    "sort_order": i,
                }
            )
    else:
        analyses = (
            sb.table("boq_analyses")
            .select("result_json")
            .eq("project_id", project_id)
            .eq("status", "done")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        ).data or []
        result = (analyses[0].get("result_json") if analyses else None) or {}
        cats = (sb.table("material_categories").select("id, name").execute()).data or []
        cat_by_name = {(c.get("name") or "").strip().lower(): c["id"] for c in cats}
        i = 0
        for site in result.get("sites") or []:
            for group in site.get("material_groups") or []:
                label = (group.get("group_name") or "").strip() or None
                cat_id = cat_by_name.get(label.lower()) if label else None
                for it in group.get("items") or []:
                    rows.append(
                        {
                            "project_id": project_id,
                            "material_category_id": cat_id,
                            "category_label": None if cat_id else label,
                            "site_name": site.get("site_name"),
                            "description": (it.get("description") or "").strip(),
                            "quantity": it.get("quantity"),
                            "unit": it.get("unit"),
                            "notes": it.get("notes"),
                            "source": "boq",
                            "sort_order": i,
                        }
                    )
                    i += 1

    rows = [r for r in rows if r["description"]]
    for start in range(0, len(rows), 500):
        sb.table("pm_materials").insert(rows[start : start + 500]).execute()
    return len(rows)


def activate_pm_for_win(
    project_id: str, actor_id: str, winning_gc_id: str | None
) -> bool:
    """Enter a won project into PM at Preconstruction. Returns True when this
    call performed the activation, False when it was a no-op (already in PM,
    abandoned, or lost a race). Exceptions propagate — the outcome upserts are
    idempotent, so the PA simply re-submits and activation retries; a won
    project must never silently fail to reach PM."""
    sb = get_supabase()
    proj = (
        sb.table("projects")
        .select("id, name, pm_stage, abandoned_at, est_start_date, est_finish_date")
        .eq("id", project_id)
        .execute()
    ).data
    if not proj:
        return False
    proj = proj[0]
    if proj.get("pm_stage") is not None:
        return False  # already in PM — idempotent no-op
    if proj.get("abandoned_at"):
        # An abandoned bid doesn't enter PM; reactivating it does (the
        # reactivate endpoint calls activate_pm_if_won).
        return False

    value = _winning_bid_amount(project_id, winning_gc_id)
    today = datetime.now(timezone.utc).date().isoformat()
    details = {
        "project_id": project_id,
        "customer_gc_id": winning_gc_id,
        # Denormalized display name — every PM surface renders customer_name;
        # the FK is kept for integrity but the name must not arrive empty.
        "customer_name": _gc_name(winning_gc_id),
        "original_contract_value": str(value) if value is not None else None,
        "awarded_at": today,
        # Copies, not aliases: PM schedule edits must not mutate the bid estimate.
        "planned_start_date": proj.get("est_start_date"),
        "planned_finish_date": proj.get("est_finish_date"),
        "activated_by": actor_id,
    }
    try:
        sb.table("pm_details").insert(details).execute()
    except Exception as exc:  # noqa: BLE001 — unique project_id: races self-resolve
        if not _is_duplicate_pm_details(exc):
            raise
        # A previous attempt inserted the row but may have crashed before the
        # stage flip below — continue so the optimistic update can heal it.

    updated = (
        sb.table("projects")
        .update({"pm_stage": "precon", "pm_origin": "bid"})
        .eq("id", project_id)
        .is_("pm_stage", "null")
        .execute()
    ).data
    if not updated:
        return False  # concurrent activation already flipped it

    append_pm_stage_event(
        project_id, None, "precon", actor_id, "Won bid — entered Preconstruction"
    )
    # Seed the PM materials list from the bid's BOQ. Best-effort: the stage is
    # already flipped (activation committed), so a seeding failure must land in
    # the logs — not fail the outcome submit that won the job.
    try:
        seeded = seed_pm_materials_from_boq(project_id)
    except Exception:  # noqa: BLE001
        logger.exception("BOQ→PM materials seed failed for project %s", project_id)
        seeded = 0
    audit(
        actor_id,
        "pm.activate",
        "project",
        project_id,
        {"origin": "bid", "materials_seeded": seeded},
    )
    notify_role(
        Role.EXECUTIVE,
        project_id,
        "pm_activated",
        f"{proj['name']} was won — now in Preconstruction",
    )
    return True


def activate_pm_if_won(project_id: str, actor_id: str) -> bool:
    """Activate PM when the project's recorded outcome is a win. Used by
    reactivate: an abandoned-then-won bid enters PM the moment it is revived."""
    rows = (
        get_supabase()
        .table("bid_outcomes")
        .select("result, winning_gc_id")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    if not rows or rows[0].get("result") != "won":
        return False
    return activate_pm_for_win(project_id, actor_id, rows[0].get("winning_gc_id"))


def require_pm_project(project_id: str) -> dict:
    """Module-route guard: 404 when the project doesn't exist, 409 when it has
    no PM life (module data can't hang off a bid-only project). Returns the
    project row (id, name, pm_stage, pm_completed_at)."""
    from fastapi import HTTPException, status  # local: keep the service framework-light

    rows = (
        get_supabase()
        .table("projects")
        .select("id, name, pm_stage, pm_completed_at")
        .eq("id", project_id)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    project = rows[0]
    if project.get("pm_stage") is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Project is not in Project Management"
        )
    return project


def flag_winner_change(project_id: str, project_name: str, winning_gc_id: str | None) -> None:
    """A won→won correction that CHANGES the winning GC after PM began: the PM
    record keeps the seeds from the original winner (customer, contract value),
    so a human must reconcile — activation is an idempotent no-op by design and
    must not silently overwrite PM data that may already have been edited."""
    details = (
        get_supabase()
        .table("pm_details")
        .select("customer_gc_id")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    if not details:
        return
    if winning_gc_id and details[0].get("customer_gc_id") not in (None, winning_gc_id):
        notify_role(
            Role.EXECUTIVE,
            project_id,
            "pm_outcome_conflict",
            f"The winning GC on {project_name} changed after it entered Project "
            "Management — review the PM customer and contract value.",
        )


def pm_activity_exists(project_id: str) -> bool:
    """True when any PM work exists beyond the activation itself: rows in any
    module table, or PM stage moves past the initial NULL→precon event."""
    sb = get_supabase()
    events = (
        sb.table("pm_stage_events")
        .select("id")
        .eq("project_id", project_id)
        .limit(2)
        .execute()
    ).data or []
    if len(events) > 1:
        return True
    for table in _PM_CHILD_TABLES:
        try:
            q = sb.table(table).select("id").eq("project_id", project_id)
            if table == "pm_materials":
                # Seeded-from-BOQ rows are created by the activation itself —
                # only rows a person added count as work worth protecting.
                q = q.eq("source", "manual")
            rows = (q.limit(1).execute()).data or []
        except Exception as exc:  # noqa: BLE001 — module migration not applied yet
            if _is_missing_table(exc):
                continue
            raise
        if rows:
            return True
    return False


def is_retractable(project: dict) -> bool:
    """May this project's PM record be silently undone? Only while it is an
    untouched Precon shell (the "recorded won by mistake" case). `project` must
    carry pm_stage / pm_completed_at and id."""
    return (
        project.get("pm_stage") == "precon"
        and not project.get("pm_completed_at")
        and not pm_activity_exists(project["id"])
    )


def create_direct_project(body, actor_id: str) -> dict:
    """Create a project directly in PM (awarded without a bid, or onboarding an
    already-live job at any stage). The project never enters the bidding
    pipeline: current_stage='pm_only', zero stage_events. Returns the created
    projects row (caller presents it)."""
    from fastapi import HTTPException, status  # local: keep the service framework-light

    sb = get_supabase()
    customer_name = body.customer_name
    if body.customer_gc_id:
        gc = (
            sb.table("general_contractors")
            .select("id, name")
            .eq("id", body.customer_gc_id)
            .execute()
        ).data
        if not gc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer GC not found")
        # Every PM surface renders customer_name — a GC pick must fill it too.
        customer_name = customer_name or gc[0]["name"]

    project_payload = {
        "name": body.name,
        "number": body.number,
        "address": body.address,
        "current_stage": "pm_only",
        "current_owner_role": None,
        "pm_stage": body.initial_stage,
        "pm_origin": "direct",
        "created_by": actor_id,
    }
    created = sb.table("projects").insert(project_payload).execute().data[0]

    try:
        value = body.original_contract_value
        sb.table("pm_details").insert(
            {
                "project_id": created["id"],
                "customer_gc_id": body.customer_gc_id,
                "customer_name": customer_name,
                "original_contract_value": str(value) if value is not None else None,
                "awarded_at": body.awarded_at.isoformat() if body.awarded_at else None,
                "ntp_date": body.ntp_date.isoformat() if body.ntp_date else None,
                "planned_start_date": (
                    body.planned_start_date.isoformat() if body.planned_start_date else None
                ),
                "planned_finish_date": (
                    body.planned_finish_date.isoformat() if body.planned_finish_date else None
                ),
                "actual_start_date": (
                    body.actual_start_date.isoformat() if body.actual_start_date else None
                ),
                "superintendent_name": body.superintendent_name,
                "contract_number": body.contract_number,
                "notes": body.notes,
                "activated_by": actor_id,
            }
        ).execute()
        append_pm_stage_event(
            created["id"],
            None,
            body.initial_stage,
            actor_id,
            "Created directly in Project Management",
        )
    except Exception:
        # Compensating delete (cascade cleans children): no orphan half-created
        # project invisible to both dashboards.
        sb.table("projects").delete().eq("id", created["id"]).execute()
        raise

    audit(
        actor_id,
        "pm.project_create",
        "project",
        created["id"],
        {"number": created["number"], "initial_stage": body.initial_stage},
    )
    return created


def approved_change_total(project_id: str) -> Decimal:
    """Sum of approved change orders — the delta between original and current
    contract value. Tolerates the financials migration (0059) not being applied
    yet (module can't have data → zero)."""
    try:
        rows = (
            get_supabase()
            .table("change_orders")
            .select("amount")
            .eq("project_id", project_id)
            .eq("status", "approved")
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001
        if _is_missing_table(exc):
            return Decimal(0)
        raise
    return sum((Decimal(str(r["amount"])) for r in rows if r.get("amount") is not None), Decimal(0))


def retract_pm(project_id: str, actor_id: str) -> bool:
    """Undo an activation (outcome corrected away from won before any PM work).
    Removes the pm_details shell and the activation event; the audit row keeps
    the record. Returns True when this call performed the retraction."""
    sb = get_supabase()
    updated = (
        sb.table("projects")
        .update({"pm_stage": None, "pm_origin": None})
        .eq("id", project_id)
        .eq("pm_stage", "precon")
        .execute()
    ).data
    if not updated:
        return False  # concurrent change — leave everything alone
    sb.table("pm_details").delete().eq("project_id", project_id).execute()
    sb.table("pm_stage_events").delete().eq("project_id", project_id).execute()
    # Only seeded-from-BOQ rows can exist here (manual rows block retraction
    # via pm_activity_exists) — sweep them so nothing hangs off a bid-only
    # project. Best-effort like the notification sweep below.
    try:
        sb.table("pm_materials").delete().eq("project_id", project_id).execute()
    except Exception as exc:  # noqa: BLE001 — module migration not applied yet
        if not _is_missing_table(exc):
            logger.exception("PM materials sweep failed for project %s", project_id)
    # The pm_activated bell rows now deep-link to a PM page that 404s — sweep
    # them (best-effort; the retraction must not roll back on a cleanup error).
    try:
        dismiss_notifications(project_id=project_id, type_prefixes=["pm_"])
    except Exception:  # noqa: BLE001
        logger.exception("PM notification sweep failed for project %s", project_id)
    audit(actor_id, "pm.retract", "project", project_id, {})
    return True

"""Workflow state machine — the spine of the bidding pipeline.

The pipeline is no longer one linear pointer. The same tasks are grouped into FOUR
CATEGORIES that progress in parallel under a DAG:

    intake            [intake, go_no_go, to_estimator]
    material_numbers  [estimate_received, rfqs, receive_quotes]   unlock: intake complete
    labor_numbers     [labor_numbers, markup]                     unlock: intake complete
    send_out          [gc_pricing, verify, send_out, submitted, bid_outcome]
                                                                  unlock: intake+material+labor complete

`material_numbers` and `labor_numbers` run concurrently once intake completes (both
auto-activate). Within a category tasks are strictly sequential — no skipping.
`declined` (Go/No-Go "No") is a project-global kill.

The source of truth is the `project_category_state` table (4 rows/project). A project
still carries a denormalized `current_stage` / `current_owner_role` "headline" pointer
(recomputed on every transition) so analytics, derive_status, the ?stage= filter and the
due-reminder terminal filters keep working — the deep analytics rework is deferred.

`owner_role` per category is the "whose task" hint; it does NOT hard-gate who may act —
any writer role may advance any category (Verify is the sole exception: Executive / IT
Admin only). `STAGES` and `owner_role_for` are retained for labels, the headline, and
analytics.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException, status

from app.core.roles import INTERNAL_ROLES, Role
from app.core.supabase_client import get_supabase
from app.services import notifications

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StageDef:
    key: str
    order: int
    owner_roles: tuple[Role, ...]
    label: str


# Per-task metadata. `order` is a single integer line kept ONLY for the denormalized
# headline pointer, labels, and (legacy) analytics sorting — it is NOT the gating axis
# anymore; gating is per-category (CATEGORY_TASKS) + the DAG (CATEGORY_PREREQS).
STAGES: dict[str, StageDef] = {
    "intake":            StageDef("intake",            1, (Role.ESTIMATING_ADMIN,), "Intake"),
    "go_no_go":          StageDef("go_no_go",          2, (Role.ESTIMATING_ENGINEER, Role.ESTIMATING_ADMIN, Role.EXECUTIVE), "Go / No-Go"),
    "to_estimator":      StageDef("to_estimator",      3, (Role.ESTIMATING_ADMIN, Role.ESTIMATING_ENGINEER), "To Estimator"),
    "estimate_received": StageDef("estimate_received", 4, (Role.ESTIMATOR, Role.ESTIMATING_ENGINEER), "Estimate Received"),
    "rfqs":              StageDef("rfqs",              5, (Role.ESTIMATING_ENGINEER,), "RFQs"),
    "receive_quotes":    StageDef("receive_quotes",    6, (Role.ESTIMATING_ENGINEER,), "Receive Quotes"),
    "labor_numbers":     StageDef("labor_numbers",     7, (Role.ESTIMATING_ENGINEER,), "Labor Numbers"),
    "markup":            StageDef("markup",            8, (Role.ESTIMATING_ENGINEER,), "Markup"),
    "gc_pricing":        StageDef("gc_pricing",        9, (Role.ESTIMATING_ENGINEER, Role.EXECUTIVE), "GC Pricing"),
    "verify":            StageDef("verify",            10, (Role.EXECUTIVE,), "Verify"),
    "send_out":          StageDef("send_out",          11, (Role.ESTIMATING_ADMIN, Role.ESTIMATING_ENGINEER), "Send Out"),
    "submitted":         StageDef("submitted",         12, (Role.ESTIMATING_ADMIN,), "Submitted"),
    "bid_outcome":       StageDef("bid_outcome",       13, (Role.ESTIMATING_ADMIN,), "Win / Loss"),
    "declined":          StageDef("declined",          99, (), "Declined"),
    # Placeholder for projects created directly in Project Management (never bid).
    # Terminal by construction: no TRANSITIONS edge leads into or out of it, so
    # transition_project can never move a project onto/off this stage — it exists
    # only so STAGES[...] lookups on such rows never KeyError. PM's own lifecycle
    # lives in pm_stage / services/pm_workflow.py, not here.
    "pm_only":           StageDef("pm_only",           98, (), "PM Only"),
    # Same placeholder idea for payroll-only projects imported from the legacy
    # Certified Payroll app (never bid, not in PM). Terminal by construction.
    "cp_only":           StageDef("cp_only",           97, (), "CP Only"),
}


# ── The category model ────────────────────────────────────────────────────────
# Each category's ordered task list (strictly sequential within a category).
CATEGORY_TASKS: dict[str, list[str]] = {
    "intake":           ["intake", "go_no_go", "to_estimator"],
    "material_numbers": ["estimate_received", "rfqs", "receive_quotes"],
    "labor_numbers":    ["labor_numbers", "markup"],
    "send_out":         ["gc_pricing", "verify", "send_out", "submitted", "bid_outcome"],
}
CATEGORY_ORDER: list[str] = ["intake", "material_numbers", "labor_numbers", "send_out"]
CATEGORY_LABELS: dict[str, str] = {
    "intake": "Intake",
    "material_numbers": "Material Numbers",
    "labor_numbers": "Labor Numbers",
    "send_out": "Send Out",
}
# The DAG: which categories must be COMPLETE before a category may start.
CATEGORY_PREREQS: dict[str, list[str]] = {
    "intake": [],
    "material_numbers": ["intake"],
    "labor_numbers": ["intake"],
    "send_out": ["intake", "material_numbers", "labor_numbers"],
}
STAGE_TO_CATEGORY: dict[str, str] = {
    task: cat for cat, tasks in CATEGORY_TASKS.items() for task in tasks
}
# Heads a project can only LEAVE through a dedicated endpoint (not the generic
# /advance): the Go/No-Go decision, the Verify commit, the Send-Out completion, and
# the Win/Loss record. The generic advance refuses these and points at the right flow.
PANEL_OWNED_HEADS: frozenset[str] = frozenset({"go_no_go", "verify", "send_out", "submitted"})


def category_of(task: str) -> str:
    return STAGE_TO_CATEGORY[task]


def next_task_in_category(task: str) -> str | None:
    tasks = CATEGORY_TASKS[category_of(task)]
    i = tasks.index(task)
    return tasks[i + 1] if i + 1 < len(tasks) else None


def owner_role_for(stage: str) -> Role | None:
    defn = STAGES.get(stage)
    return defn.owner_roles[0] if defn and defn.owner_roles else None


def internal_owner_role_for(stage: str) -> Role | None:
    """The first INTERNAL role that owns `stage` (skips the estimator, who co-owns
    `estimate_received` only for access — a handoff must reach a real team inbox)."""
    defn = STAGES.get(stage)
    if not defn:
        return None
    for role in defn.owner_roles:
        if role in INTERNAL_ROLES:
            return role
    return None


# ── Category-state loading + predicates ───────────────────────────────────────
_CS_COLS = "category, current_task, status, owner_role, completed_at"


def _default_state() -> dict[str, dict]:
    """A fresh (pre-seed / legacy fallback) category map: intake active, rest locked."""
    out: dict[str, dict] = {}
    for cat in CATEGORY_ORDER:
        first = CATEGORY_TASKS[cat][0]
        out[cat] = {
            "current_task": first,
            "status": "active" if cat == "intake" else "locked",
            "owner_role": owner_role_for(first).value if owner_role_for(first) else None,
            "completed_at": None,
        }
    return out


def load_category_state(project_id: str) -> dict[str, dict]:
    """The 4-category state map for one project (missing rows filled with defaults)."""
    rows = (
        get_supabase()
        .table("project_category_state")
        .select(_CS_COLS)
        .eq("project_id", project_id)
        .execute()
    ).data or []
    state = _default_state()
    for r in rows:
        state[r["category"]] = {
            "current_task": r["current_task"],
            "status": r["status"],
            "owner_role": r.get("owner_role"),
            "completed_at": r.get("completed_at"),
        }
    return state


def load_category_states(project_ids: list[str]) -> dict[str, dict[str, dict]]:
    """Batch variant, keyed by project_id (each value is a full 4-category map)."""
    out: dict[str, dict[str, dict]] = {pid: _default_state() for pid in project_ids}
    if not project_ids:
        return out
    rows = (
        get_supabase()
        .table("project_category_state")
        .select("project_id, " + _CS_COLS)
        .in_("project_id", project_ids)
        .execute()
    ).data or []
    for r in rows:
        pid = r["project_id"]
        out.setdefault(pid, _default_state())[r["category"]] = {
            "current_task": r["current_task"],
            "status": r["status"],
            "owner_role": r.get("owner_role"),
            "completed_at": r.get("completed_at"),
        }
    return out


def is_category_complete(state: dict[str, dict], category: str) -> bool:
    return state.get(category, {}).get("status") == "complete"


def category_reached(state: dict[str, dict], category: str, task: str) -> bool:
    """True when `category`'s head is AT or PAST `task` (complete counts as past)."""
    cs = state.get(category, {})
    if cs.get("status") == "locked":
        return False
    if cs.get("status") == "complete":
        return True
    tasks = CATEGORY_TASKS[category]
    return tasks.index(cs["current_task"]) >= tasks.index(task)


def category_past(state: dict[str, dict], task: str) -> bool:
    """True when the owning category's head is STRICTLY past `task` (or complete)."""
    cat = category_of(task)
    cs = state.get(cat, {})
    if cs.get("status") == "locked":
        return False
    if cs.get("status") == "complete":
        return True
    tasks = CATEGORY_TASKS[cat]
    return tasks.index(cs["current_task"]) > tasks.index(task)


def category_before(state: dict[str, dict], task: str) -> bool:
    """True when the owning category is ACTIVE and its head is before `task`."""
    cat = category_of(task)
    cs = state.get(cat, {})
    if cs.get("status") != "active":
        return False
    tasks = CATEGORY_TASKS[cat]
    return tasks.index(cs["current_task"]) < tasks.index(task)


# ── Notification dismissal (per-category) ─────────────────────────────────────
# Each notification type / due.<kind>. prefix is pending through a TASK; its category
# is inferred. A task's notification is dismissed only when ITS category advances PAST
# that task — so a labor advance can no longer wrongly silence a material-side reminder.
_STAGE_DISMISS_TYPES: dict[str, str] = {
    "gono_go":              "to_estimator",     # dismissed when intake completes
    "assigned":             "estimate_received",
    "estimate_submitted":   "estimate_received",
    "drawing_changed":      "verify",
    "verified":             "send_out",
    "submitted":            "submitted",
    "proposal_send_failed": "send_out",
    "reverify_required":    "verify",
}
_STAGE_DISMISS_PREFIXES: dict[str, str] = {
    "due.due_from_estimator.":  "to_estimator",
    "due.due_from_vendors.":    "receive_quotes",
    "due.internal_bid.":        "send_out",
    "due.actual_bid.":          "send_out",
}


def _dismiss_stale_notifications(
    project_id: str, advanced_category: str, new_state: dict[str, dict]
) -> None:
    """Dismiss notifications whose task is finished now `advanced_category` advanced.
    Best-effort — must never roll back a committed transition."""
    # stage_handoff is an ephemeral "advanced to X" ping; keep dismissing it project-wide
    # on any advance (a minor over-dismissal under parallel handoffs, deliberately simple).
    types = ["stage_handoff"]
    types += [
        t for t, task in _STAGE_DISMISS_TYPES.items()
        if category_of(task) == advanced_category and category_past(new_state, task)
    ]
    prefixes = [
        p for p, task in _STAGE_DISMISS_PREFIXES.items()
        if category_of(task) == advanced_category and category_past(new_state, task)
    ]
    try:
        notifications.dismiss_notifications(
            project_id=project_id, types=types, type_prefixes=prefixes or None
        )
    except Exception:  # noqa: BLE001 — cleanup must not break the transition
        logger.exception("Notification dismissal failed for project %s", project_id)


# ── Headline recomputation ────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _recompute_headline(project_id: str) -> str:
    """Recompute the denormalized projects.current_stage/current_owner_role from the
    category state. Never called on a declined project (decline sets it directly)."""
    sb = get_supabase()
    state = load_category_state(project_id)
    so = state.get("send_out", {})
    if so.get("status") in ("active", "complete"):
        head = so["current_task"]
    else:
        actives = [
            (STAGES[cs["current_task"]].order, cs["current_task"])
            for cs in state.values()
            if cs.get("status") == "active"
        ]
        if actives:
            head = max(actives, key=lambda t: t[0])[1]
        else:
            comps = [
                (STAGES[cs["current_task"]].order, cs["current_task"])
                for cs in state.values()
                if cs.get("status") == "complete"
            ]
            head = max(comps, key=lambda t: t[0])[1] if comps else "intake"
    owner = owner_role_for(head)
    sb.table("projects").update(
        {"current_stage": head, "current_owner_role": owner}
    ).eq("id", project_id).execute()
    return head


# ── Core mutations ────────────────────────────────────────────────────────────
def _upsert_head(
    project_id: str, category: str, task: str, new_status: str
) -> None:
    get_supabase().table("project_category_state").upsert(
        {
            "project_id": project_id,
            "category": category,
            "current_task": task,
            "status": new_status,
            "owner_role": owner_role_for(task),
            "completed_at": _now_iso() if new_status == "complete" else None,
        },
        on_conflict="project_id,category",
    ).execute()


def _emit_event(
    project_id: str, category: str, from_task: str | None, to_task: str, actor_id: str | None, note: str | None
) -> None:
    get_supabase().table("stage_events").insert(
        {
            "project_id": project_id,
            "from_stage": from_task,
            "to_stage": to_task,
            "category": category,
            "actor_id": actor_id,
            "note": note,
        }
    ).execute()


def _run_fanout(project_id: str, actor_id: str | None) -> None:
    """Activate any locked category whose prerequisites are now all complete. Emits an
    activation event (from=None -> first task) so analytics sees each category start."""
    state = load_category_state(project_id)
    for cat in CATEGORY_ORDER:
        cs = state.get(cat, {})
        if cs.get("status") != "locked":
            continue
        if all(state.get(pr, {}).get("status") == "complete" for pr in CATEGORY_PREREQS[cat]):
            first = CATEGORY_TASKS[cat][0]
            _upsert_head(project_id, cat, first, "active")
            _emit_event(project_id, cat, None, first, actor_id, "Category unlocked")
            state[cat] = {"current_task": first, "status": "active"}


def advance_category(
    project_id: str, category: str, actor_id: str | None, note: str | None = None
) -> dict:
    """Complete the current head task of `category` and move to the next (or mark the
    category complete when it was the last task, unlocking dependents). Returns the
    updated project row (with recomputed headline). Raises 409 if the category isn't
    active."""
    if category not in CATEGORY_TASKS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown category '{category}'")
    state = load_category_state(project_id)
    cs = state.get(category, {})
    if cs.get("status") != "active":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Category '{category}' is not active"
        )
    head = cs["current_task"]
    nxt = next_task_in_category(head)
    if nxt is not None:
        _upsert_head(project_id, category, nxt, "active")
        _emit_event(project_id, category, head, nxt, actor_id, note)
    else:
        # Last task done → the category completes (no self-loop event; completed_at
        # records it). Fan-out may then unlock dependent categories.
        _upsert_head(project_id, category, head, "complete")
        _run_fanout(project_id, actor_id)

    _recompute_headline(project_id)
    _dismiss_stale_notifications(project_id, category, load_category_state(project_id))
    return _project_row(project_id)


def decline_project(
    project_id: str, actor_id: str | None, note: str | None = None
) -> dict:
    """Go/No-Go "No": a project-global kill. Sets the headline to 'declined' (which
    derive_status short-circuits) and records the event; all categories stay frozen
    (material/labor/send_out never unlocked, intake parked at the decision)."""
    sb = get_supabase()
    _emit_event(project_id, "intake", "go_no_go", "declined", actor_id, note)
    _upsert_head(project_id, "intake", "go_no_go", "active")
    updated = (
        sb.table("projects")
        .update({"current_stage": "declined", "current_owner_role": None})
        .eq("id", project_id)
        .execute()
    ).data
    return updated[0] if updated else _project_row(project_id)


def reopen_go_no_go(
    project_id: str, from_task: str, actor_id: str | None, note: str | None = None
) -> dict:
    """Undo of a Go/No-Go decision: park the intake lane back on `go_no_go`, in
    review, from wherever the decision put it (`from_task` is 'to_estimator' after
    a Go, 'declined' after a No-Go).

    The reverse move is recorded as its own stage event — nothing is erased — and
    the headline is recomputed, which is what clears a declined project's
    'declined' current_stage. Callers (services.gono.undo) own the guard that this
    is only reachable while nothing downstream has happened.
    """
    _emit_event(project_id, "intake", from_task, "go_no_go", actor_id, note)
    _upsert_head(project_id, "intake", "go_no_go", "active")
    _recompute_headline(project_id)
    return _project_row(project_id)


def _project_row(project_id: str) -> dict:
    return (
        get_supabase()
        .table("projects")
        .select("*")
        .eq("id", project_id)
        .single()
        .execute()
    ).data


# ── Re-verification on a post-verify pricing edit (scoped to the send_out category) ──
# A pricing-affecting edit on a project whose send_out category has ALREADY passed Verify
# (head is send_out / submitted / bid_outcome) bounces the send_out head back to `verify`
# for a re-commit, WITHOUT disturbing the (already-complete) material/labor categories.
_PAST_VERIFY_HEADS = ("send_out", "submitted", "bid_outcome")


def reopen_verify(
    project_id: str, actor_id: str | None, reason: str
) -> tuple[dict, bool]:
    """Bounce the send_out category head back to `verify`. No-op (moved=False) unless
    the send_out head is past verify. Records reverify_return_stage (first bounce only)
    and clears the committed verification snapshot so the figures are re-editable."""
    sb = get_supabase()
    state = load_category_state(project_id)
    so = state.get("send_out", {})
    head = so.get("current_task")
    if so.get("status") != "active" or head not in _PAST_VERIFY_HEADS:
        return _project_row(project_id), False

    proj = (
        sb.table("projects")
        .select("id, reverify_return_stage")
        .eq("id", project_id)
        .single()
        .execute()
    ).data
    if not proj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")

    # Optimistic lock on the send_out head so a racing edit can't double-apply.
    updated = (
        sb.table("project_category_state")
        .update(
            {
                "current_task": "verify",
                "status": "active",
                "owner_role": owner_role_for("verify"),
                "completed_at": None,
            }
        )
        .eq("project_id", project_id)
        .eq("category", "send_out")
        .eq("current_task", head)
        .execute()
    ).data
    if not updated:
        return proj, False  # a concurrent edit already bounced it

    _emit_event(project_id, "send_out", head, "verify", actor_id, reason)
    if proj.get("reverify_return_stage") is None:
        sb.table("projects").update({"reverify_return_stage": head}).eq(
            "id", project_id
        ).execute()
    sb.table("verifications").update(
        {"committed_at": None, "verified_by": None, "updated_at": "now()"}
    ).eq("project_id", project_id).execute()
    _recompute_headline(project_id)
    return _project_row(project_id), True


def return_from_reverify(
    project_id: str, return_stage: str, actor_id: str | None
) -> dict:
    """After a re-verify commit, send the send_out head back to the task it was on
    before the bounce (`return_stage`) and clear the marker."""
    if return_stage not in CATEGORY_TASKS["send_out"]:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Invalid send_out return task '{return_stage}'"
        )
    sb = get_supabase()
    _emit_event(project_id, "send_out", "verify", return_stage, actor_id, "Re-verify committed")
    _upsert_head(project_id, "send_out", return_stage, "active")
    sb.table("projects").update({"reverify_return_stage": None}).eq(
        "id", project_id
    ).execute()
    _recompute_headline(project_id)
    _dismiss_stale_notifications(project_id, "send_out", load_category_state(project_id))
    return _project_row(project_id)


def maybe_reopen_verify_after_edit(
    project_id: str, actor_id: str | None, reason: str
) -> bool:
    """If a pricing-affecting edit landed on a project whose send_out category has
    passed Verify, bounce it back to `verify` for re-commit and notify the Executive +
    Engineer. No-op otherwise. Best-effort — the pricing write already succeeded."""
    try:
        proj = (
            get_supabase()
            .table("projects")
            .select("current_stage, abandoned_at")
            .eq("id", project_id)
            .single()
            .execute()
        ).data
        if not proj or proj.get("abandoned_at") or proj.get("current_stage") == "declined":
            return False
        state = load_category_state(project_id)
        so = state.get("send_out", {})
        head = so.get("current_task")
        if so.get("status") != "active" or head not in _PAST_VERIFY_HEADS:
            return False
        _, moved = reopen_verify(project_id, actor_id, reason)
        if moved:
            message = (
                f"A pricing-affecting edit was made after Verify — re-commit required ({reason})."
            )
            # submitted/bid_outcome means the bid already left the door.
            if head in ("submitted", "bid_outcome"):
                message += (
                    " The bid already sent to GCs is unchanged; this updates the "
                    "internal pricing record only."
                )
            for role in (Role.EXECUTIVE, Role.ESTIMATING_ENGINEER):
                notifications.notify_role(role, project_id, "reverify_required", message)
        return moved
    except Exception:  # noqa: BLE001 — the edit succeeded; bouncing is best-effort
        logger.exception("Re-verify bounce failed for project %s", project_id)
        return False

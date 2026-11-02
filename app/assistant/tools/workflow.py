"""advance_lane (docs/ASSISTANT_PHASE4_PLAN.md 3.12): move one bidding lane's
head task on by one step, behind a card.

  advance_lane   POST /projects/{id}/advance (write_internal, confirm)

The model chooses the project, the lane and an optional note. Everything
else is fixed here: the request always carries `gono_action="review"` (an
intake lane parks at Go/No-Go for a person, no score is applied and no
decline can result) and `expected_task`, the head the card was priced on, so
a lane a teammate moved answers 409 `lane_moved` and the card supersedes.
The route enforces both again for an assistant principal. The Send Out lane
is not offered at all, and the panel-owned heads (Go/No-Go, Verify, Send Out,
Submitted) are refused with the page that completes them.

Like tools/splits.py this module imports no router or service: the stage
labels, owners and head lists are its own literals, and
tests/test_assistant_workflow_tool.py asserts they equal the service's.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.assistant.registry import (
    EFFECT_WRITE_INTERNAL,
    Proposal,
    ProposalRefused,
    ToolRequest,
    ToolSpec,
    WriteSpec,
    nullable,
    register,
    schema,
)
from app.assistant.tools._common import ToolArgumentError, clip, dict_of, require_uuid, rows_of
from app.assistant.tools.writes import (
    ASSISTANT_IDEMPOTENCY_HEADER,
    CLOSED_TEXT,
    PROJECT_GET,
    PROJECT_ID_PROP,
    project_gate,
    project_label,
    project_ref,
)
from app.core.roles import WRITER_ROLES

# The route side's literals (app/routers/workflow.py, estimator.py, files.py,
# rfqs.py); a test asserts equality.
ADVANCE_POST = ("POST", "/projects/{project_id}/advance")
ASSIGNMENTS_GET = ("GET", "/projects/{project_id}/assignments")
FILES_GET = ("GET", "/projects/{project_id}/files")
RFQ_SENDS_GET = ("GET", "/projects/{project_id}/rfqs/sends")

TOOL_NAME = "advance_lane"
NOTE_MAX = 300

# The lanes the tool moves. send_out is never offered (D13): its tasks are
# panel-owned and sending a proposal is its own flow.
CATEGORIES = ("intake", "material_numbers", "labor_numbers", "select_vendors", "markup")

# services/workflow.py CATEGORY_TASKS and PANEL_OWNED_HEADS (tests assert equality).
CATEGORY_TASKS: dict[str, tuple[str, ...]] = {
    "intake": ("intake", "go_no_go", "to_estimator"),
    "material_numbers": ("estimate_received", "rfqs", "receive_quotes"),
    "labor_numbers": ("labor_numbers",),
    "select_vendors": ("select_vendors",),
    "markup": ("markup",),
}
PANEL_OWNED_HEADS = frozenset({"go_no_go", "verify", "send_out", "submitted"})
# Where each panel-owned head is completed, for the refusal text.
PANEL_TEXTS = {
    "go_no_go": "Go/No-Go is decided on its own page by a person, not moved on from here.",
    "verify": "Verify is committed by an Executive on its own page, not moved on from here.",
    "send_out": "Send Out is completed from its own panel, not moved on from here.",
    "submitted": "Submitted is completed from its own panel, not moved on from here.",
}
# workflow.STAGES labels (tests assert equality).
STAGE_LABELS = {
    "intake": "Intake",
    "go_no_go": "Go / No-Go",
    "to_estimator": "To Estimator",
    "estimate_received": "Estimate Received",
    "rfqs": "RFQs",
    "receive_quotes": "Receive Quotes",
    "select_vendors": "Select Vendors",
    "labor_numbers": "Labor Numbers",
    "markup": "Markup",
    "gc_pricing": "GC Pricing",
    "verify": "Verify",
    "send_out": "Send Out",
    "submitted": "Submitted",
    "bid_outcome": "Win / Loss",
}
# workflow.CATEGORY_LABELS (tests assert equality).
LANE_LABELS = {
    "intake": "Intake",
    "material_numbers": "Material Numbers",
    "labor_numbers": "Labor Numbers",
    "select_vendors": "Select Vendors",
    "markup": "Markup",
    "send_out": "Send Out",
}
# workflow.internal_owner_role_for(head), as the role's value (tests assert
# equality for every head listed in CATEGORY_TASKS).
HEAD_OWNERS = {
    "intake": "estimating_admin",
    "go_no_go": "executive",
    "to_estimator": "estimating_admin",
    "estimate_received": "estimating_engineer_materials",
    "rfqs": "estimating_engineer_materials",
    "receive_quotes": "estimating_engineer_materials",
    "select_vendors": "estimating_engineer_materials",
    "labor_numbers": "estimating_engineer_labor",
    "markup": "estimating_engineer_labor",
}
ROLE_LABELS = {
    "estimating_admin": "Estimating Admin",
    "executive": "Executive",
    "estimating_engineer_materials": "Materials Engineer",
    "estimating_engineer_labor": "Labor Engineer",
    "it_admin": "IT Admin",
}
# file_categories.DRAWING_CATEGORIES (a test asserts equality).
DRAWING_CATEGORIES = frozenset({
    "drawing", "civil_drawing", "structural_drawing", "architectural_drawing", "mechanical_drawing",
    "plumbing_drawing", "electrical_drawing", "fire_protection_drawing", "low_voltage_drawing",
})
CLOSED_STAGES = ("pm_only", "cp_only", "declined")

LANE_NOT_ACTIVE_TEXT = "The {lane} lane is not active yet, so it cannot be moved on."
LANE_COMPLETE_TEXT = "The {lane} lane is already complete."
NO_DRAWING_TEXT = "Upload at least one drawing or plan before completing Intake."
STAGE_CLOSED_TEXT = "This project is not in the bidding flow (it is declined or lives in Project Management or Payroll)."

NO_UNDO = {"code": "no_undo", "text": "Cannot be undone from here."}
WARN_NO_ESTIMATOR = {
    "code": "no_estimator",
    "text": "No estimator is assigned to this project yet. Finishing To Estimator does not send anything to one.",
}
WARN_NO_RFQS = {
    "code": "no_rfqs_sent",
    "text": "No RFQ has been sent to a vendor on this project yet.",
}
INTAKE_PARKS = "Parks at Go/No-Go for a manual decision, no score applied"


def _no_trim(payloads: list[Any], args: Mapping[str, Any], ctx: Any) -> Any:
    return payloads


def _category(args: Mapping[str, Any]) -> str:
    category = args.get("category")
    if category not in CATEGORIES:
        raise ToolArgumentError(f"category must be one of {', '.join(CATEGORIES)}.")
    return str(category)


def _note(args: Mapping[str, Any]) -> str | None:
    text = " ".join(str(args.get("note") or "").split())
    if not text:
        return None
    if len(text) > NOTE_MAX:
        raise ToolArgumentError(f"note must be at most {NOTE_MAX} characters.")
    return text


def _requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    pid = require_uuid(args, "project_id")
    category = _category(args)
    reads = [ToolRequest(route=PROJECT_GET, path=f"/projects/{pid}")]
    # Optional context reads for the card's warnings; a 403 or 404 skips them.
    if category == "intake":
        reads.append(ToolRequest(route=ASSIGNMENTS_GET, path=f"/projects/{pid}/assignments", optional=True))
        reads.append(ToolRequest(route=FILES_GET, path=f"/projects/{pid}/files", optional=True))
    elif category == "material_numbers":
        reads.append(ToolRequest(route=RFQ_SENDS_GET, path=f"/projects/{pid}/rfqs/sends", optional=True))
    return reads


def _lane(project: Mapping[str, Any], category: str) -> dict:
    state = dict_of(dict_of(project.get("category_state")).get(category))
    return {"current_task": state.get("current_task"), "status": state.get("status")}


def _snapshot(args: Mapping[str, Any], reads: list[Any]) -> tuple[dict, dict]:
    category = _category(args)
    project = dict_of(reads[0] if reads else None)
    lane = _lane(project, category)
    return {"category": category, **lane}, project_gate(project)


def _next_head(category: str, head: str) -> str | None:
    tasks = CATEGORY_TASKS[category]
    i = tasks.index(head)
    return tasks[i + 1] if i + 1 < len(tasks) else None


def _has_drawing(files: Any) -> bool | None:
    """True or False from the file list; None when the list was not readable."""
    if not isinstance(files, list):
        return None
    return any(f.get("category") in DRAWING_CATEGORIES for f in rows_of(files))


def _build(args: Mapping[str, Any], reads: list[Any], ctx: Any) -> Proposal:
    pid = require_uuid(args, "project_id")
    category = _category(args)
    note = _note(args)
    project = dict_of(reads[0] if reads else None)
    gate = project_gate(project)
    if gate.get("closed"):
        raise ProposalRefused(CLOSED_TEXT)
    if project.get("current_stage") in CLOSED_STAGES:
        raise ProposalRefused(STAGE_CLOSED_TEXT)
    lane_label = LANE_LABELS[category]
    lane = _lane(project, category)
    head, status = lane.get("current_task"), lane.get("status")
    if status == "complete":
        raise ProposalRefused(LANE_COMPLETE_TEXT.format(lane=lane_label))
    if status != "active" or head not in CATEGORY_TASKS[category]:
        raise ProposalRefused(LANE_NOT_ACTIVE_TEXT.format(lane=lane_label))
    if head in PANEL_OWNED_HEADS:
        raise ProposalRefused(PANEL_TEXTS[head])
    if category == "intake" and head == "to_estimator":
        drawing = _has_drawing(reads[2] if len(reads) > 2 else None)
        if drawing is False:
            raise ProposalRefused(NO_DRAWING_TEXT)

    target, gate = _snapshot(args, reads)
    nxt = _next_head(category, head)
    number = project_label(project)
    from_label = STAGE_LABELS.get(head, str(head))
    to_label = STAGE_LABELS[nxt] if nxt else f"{lane_label} complete"
    rows = [
        _row("project", "Project", number),
        _row("lane", "Lane", lane_label),
        _row("from_task", "From", from_label),
        _row("to_task", "To", to_label),
    ]
    if category == "intake" and head == "intake":
        rows.append(_row("go_no_go", "Go/No-Go", INTAKE_PARKS))
    owner = HEAD_OWNERS.get(nxt or "")
    if owner:
        rows.append(_row("handoff", "Hand-off", f"Notifies the {ROLE_LABELS.get(owner, owner)} by bell and email"))
    elif nxt is None:
        rows.append(_row("handoff", "Hand-off",
                         "Finishing this lane may open other lanes; their owners are notified by bell and email"))
    if note:
        rows.append(_row("note", "Note", note))
    warnings: list[dict] = []
    if category == "intake" and head == "to_estimator":
        assignments = reads[1] if len(reads) > 1 else None
        if isinstance(assignments, list) and not any(
            not a.get("revoked_at") for a in rows_of(assignments)
        ):
            warnings.append(dict(WARN_NO_ESTIMATOR))
    if category == "material_numbers" and head == "rfqs":
        sends = reads[1] if len(reads) > 1 else None
        if isinstance(sends, list) and not any(s.get("status") == "sent" for s in rows_of(sends)):
            warnings.append(dict(WARN_NO_RFQS))
    warnings.append(dict(NO_UNDO))
    card = {
        "v": 1,
        "title": f"{lane_label}: {from_label} to {to_label} on {number}",
        "tool_label": "Bidding workflow",
        "project": project_ref(project),
        "rows": rows,
        "items": [],
        "warnings": warnings,
        "similar": [],
    }
    # The forced fields come from this function, never from the arguments.
    request = ToolRequest(
        route=ADVANCE_POST, path=f"/projects/{pid}/advance",
        json={"category": category, "gono_action": "review", "expected_task": head, "note": note},
        headers={ASSISTANT_IDEMPOTENCY_HEADER: str(ctx.approval_id)},
    )
    return Proposal(request=request, project_id=pid, target=target, gate=gate, preview=card,
                    summary=f"Move {number} {lane_label} from {from_label} to {to_label}.")


def _row(field: str, label: str, after: str) -> dict:
    return {"field": field, "label": label, "before": "", "after": after, "changed": True}


def _result(payload: Any) -> dict:
    """From the advance route's project row: where the project is now."""
    body = dict_of(payload)
    outcome = body.get("gono_outcome")
    return {
        "current_stage": clip(body.get("current_stage"), 40),
        "gono_outcome": clip(outcome, 40) if isinstance(outcome, str) else None,
    }


register(ToolSpec(
    name=TOOL_NAME,
    namespace="bidding",
    sub_app="bidding",
    route=ADVANCE_POST,
    extra_routes=(PROJECT_GET, ASSIGNMENTS_GET, FILES_GET, RFQ_SENDS_GET),
    effect=EFFECT_WRITE_INTERNAL,
    confirm=True,
    schema=schema({
        "project_id": PROJECT_ID_PROP,
        "category": {"type": "string", "enum": list(CATEGORIES), "description": (
            "The lane to move on one step: intake, material_numbers, labor_numbers, select_vendors or markup. "
            "Send Out is not offered.")},
        "note": nullable({"type": "string", "description": f"A short note for the stage log, up to {NOTE_MAX} characters, or null."}),
    }),
    response_trimmer=_no_trim,
    progress_label="Preparing to move a lane",
    roles=WRITER_ROLES,
    description=(
        "Propose moving one bidding lane's current task on by one step (for example Intake to Go/No-Go). Creates an "
        "approval card showing the lane, the task it leaves and the one it enters, who is notified and any warning. "
        "Go/No-Go is never decided by this tool: an intake move parks the project for a person to decide. Not for "
        "Send Out, Verify, Go/No-Go decisions or closed projects. Nothing moves until the user approves. Cannot be "
        "undone from here."
    ),
    requests=_requests,
    max_cards_per_turn=3,
    deferred=True,
    keywords=("advance", "lane", "move", "stage", "next", "intake", "workflow", "step", "handoff"),
    write=WriteSpec(
        preview_requests=_requests,
        build=_build,
        snapshot=_snapshot,
        bound_text_fields=("note",),
        revert_tool=None,
        result_summary=_result,
        writes_tables=("project_category_state", "stage_events", "projects", "notifications", "email_log"),
    ),
))

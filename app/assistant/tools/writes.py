"""The project write tools (docs/ASSISTANT_PHASE1_PLAN.md 3.7). Every tool
here is write_internal and confirm: called inside a turn it never writes,
it becomes an approval card (app/assistant/approvals.py propose), and the
one write runs only in the approval execute job after the human approves.

  set_labor_numbers      PUT /projects/{id}/labor (compare-and-set header)
  restore_labor_numbers  hidden: the revert of set_labor_numbers
  update_project_fields  PATCH /projects/{id} (the assistant allowlist only)
  add_project_gc         POST /projects/{id}/gcs (not revertible)
  create_project_draft   POST /projects (idempotency key = approval id)

Each WriteSpec reads the target through its preview routes, `snapshot`
turns the reads into a normalized target (what the write changes) and gate
(project state that must not move), and `build` turns the args and the
reads into the one request plus the card. `build` and `snapshot` share one
target function, so the execute job's re-read hashes the same way when
nothing changed. Every value in a target, gate or request is canonical-JSON
safe (tools/_common.py norm_value: no float, no Decimal).

This module never imports app.routers.* nor app.models.schemas'
ASSISTANT_PROJECT_FIELDS: the field tuple and the header literals are its
own, and tests/test_assistant_write_tools.py asserts they equal the route
side's, so a registry load never depends on a route module.
"""

from __future__ import annotations

import re
import typing
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any

from app.assistant.registry import (
    EFFECT_WRITE_INTERNAL,
    Proposal,
    ProposalRefused,
    RevertNotPossible,
    ToolRequest,
    ToolSpec,
    WriteSpec,
    nullable,
    register,
    schema,
)
from app.assistant.tools._common import (
    ToolArgumentError,
    dict_of,
    display,
    is_uuid,
    money_text,
    norm_value,
    parse_date,
    parse_local_datetime,
    parse_money,
    require_uuid,
    rows_of,
)
from app.core.roles import ACTUAL_BID_EDITOR_ROLES, WRITER_ROLES
from app.models.schemas import (
    BidMethod,
    CompetitorKnown,
    EstValueBand,
    GCKnown,
    LaborNeeded,
    OwnerType,
    ProjectType,
    ScopeFit,
    SubsNeeded,
    _clean_bidding_url,
)

# The route side's literals (app/routers/projects.py and pricing.py); a test
# asserts they are equal.
ASSISTANT_IDEMPOTENCY_HEADER = "X-Assistant-Idempotency-Key"
LABOR_EXPECTED_HEADER = "X-Assistant-Expected-Updated-At"

# app/services/workflow.py _PAST_VERIFY_HEADS and POST_SUBMISSION_HEADS
# (a test asserts they are equal).
PAST_VERIFY_HEADS = ("send_out", "submitted", "bid_outcome")
POST_SUBMISSION_HEADS = ("submitted", "bid_outcome")
PHASE_RANK = {"open": 0, "verified": 1, "submitted": 2}

LABOR_GET = ("GET", "/projects/{project_id}/labor")
LABOR_PUT = ("PUT", "/projects/{project_id}/labor")
PROJECT_GET = ("GET", "/projects/{project_id}")
PROJECT_PATCH = ("PATCH", "/projects/{project_id}")
PROJECTS_POST = ("POST", "/projects")
SIMILAR_POST = ("POST", "/projects/similar")
CREATED_GET = ("GET", "/projects/assistant-created/{key}")
PROJECT_GCS_GET = ("GET", "/projects/{project_id}/gcs")
PROJECT_GCS_POST = ("POST", "/projects/{project_id}/gcs")
GCS_GET = ("GET", "/gcs")
GC_CONTACTS_GET = ("GET", "/gc-contacts")

TEXT_MAX = 4000
NAME_MAX = 500
LONG_TEXT_MAX = 20000
BREAKDOWN_MAX_LINES = 50
BREAKDOWN_NAME_MAX = 200
CONTACTS_MAX = 50
GCS_MAX = 20
NEEDS_BY_MIN = date(2000, 1, 1)
NEEDS_BY_MAX = date(2100, 12, 31)

CLOSED_TEXT = "This project is abandoned, declined or marked failed to bid."

WARN_REVERIFY = {
    "code": "reverify_bounce",
    "text": "This project is past Verify. Saving sends it back to Verify for a re-commit and notifies "
            "the Executive and the labor engineer.",
}
WARN_PAST_SUBMISSION = {"code": "past_submission", "text": "This project's bid is already submitted."}
WARN_BREAKDOWN_REPLACED = {
    "code": "whole_breakdown_replaced",
    "text": "The whole labor breakdown is replaced by the lines on this card.",
}
WARN_REVERIFY_NOT_RESTORED = {
    "code": "reverify_not_restored",
    "text": "The original change sent this project back to Verify and notified the Executive and the "
            "labor engineer. Reverting restores the labor numbers only; Verify stays reopened.",
}
WARN_AMOUNT_FROM_BREAKDOWN = {
    "code": "amount_from_breakdown",
    "text": "The labor amount is set to the new breakdown total, as the labor panel does.",
}
WARN_AMOUNT_OVERRIDE_KEPT = {
    "code": "amount_override_kept",
    "text": "The labor amount stays at its entered value, which differs from the breakdown total "
            "(a manual override on the labor panel).",
}
STRIPPED_LINE_NAMES = (
    "A breakdown line name came from text BDR read, not from the user. Ask the user to type the line names."
)
WARN_LABOR_ROW_KEPT = {
    "code": "labor_row_kept",
    "text": "BDR cannot delete the labor row; the restore saves it with empty values.",
}

PROJECT_ID_PROP = {
    "type": "string",
    "description": "The project's id (short or full) from app_search, whats_due or the page context.",
}
MONEY_DESC = "Dollars as digits with up to two decimals, for example 12500 or 12500.50."
LOCAL_DESC = "Pacific local time as YYYY-MM-DDTHH:MM, for example 2026-10-22T14:00."
DATE_DESC = "A date as YYYY-MM-DD."


def _no_trim(payloads: list[Any], args: Mapping[str, Any], ctx: Any) -> Any:
    """A confirm tool never runs inside a turn, so it has nothing to trim."""
    return payloads


# ── Shared project helpers ───────────────────────────────────────────────────


def project_ref(project: Mapping[str, Any]) -> dict | None:
    if not project.get("id"):
        return None
    return {"id": str(project["id"]), "number": project.get("number"), "name": project.get("name")}


def project_label(project: Mapping[str, Any]) -> str:
    return str(project.get("number") or "the project")


def project_gate(project: Mapping[str, Any], *, labor: bool = False) -> dict:
    """{"closed", "phase"} (plus "bounces_verify" for the labor tools) from
    GET /projects/{id} and its category_state (3.7)."""
    send_out = dict_of(dict_of(project.get("category_state")).get("send_out"))
    head = send_out.get("current_task")
    closed = (
        bool(project.get("abandoned_at"))
        or project.get("current_stage") == "declined"
        or bool(project.get("failed_to_bid_at"))
    )
    if head in POST_SUBMISSION_HEADS:
        phase = "submitted"
    elif head == "send_out":
        phase = "verified"
    else:
        phase = "open"
    gate: dict[str, Any] = {"closed": closed, "phase": phase}
    if labor:
        gate["bounces_verify"] = send_out.get("status") == "active" and head in PAST_VERIFY_HEADS
    return gate


def _refuse_closed(gate: Mapping[str, Any]) -> None:
    if gate.get("closed"):
        raise ProposalRefused(CLOSED_TEXT)


def _phase_warnings(gate: Mapping[str, Any]) -> list[dict]:
    return [dict(WARN_PAST_SUBMISSION)] if gate.get("phase") == "submitted" else []


def _row(field: str, label: str, before: Any, after: Any, *, changed: bool | None = None) -> dict:
    b, a = norm_value(field, before), norm_value(field, after)
    return {
        "field": field,
        "label": label,
        "before": display(field, before),
        "after": display(field, after),
        "changed": (b != a) if changed is None else changed,
    }


def _text(value: Any, field: str, limit: int, *, allow_blank: bool = False) -> str | None:
    """A free-text argument: None stays None; longer than `limit` refuses;
    a blank string refuses unless `allow_blank`."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolArgumentError(f"{field} must be text.")
    if len(value) > limit:
        raise ToolArgumentError(f"{field} may be at most {limit} characters.")
    if not value.strip() and not allow_blank:
        raise ToolArgumentError(f"{field} cannot be blank.")
    return value


def _bool_arg(value: Any, field: str) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise ToolArgumentError(f"{field} must be true, false or null.")


def _list_arg(value: Any, field: str, limit: int) -> list | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ToolArgumentError(f"{field} must be a list.")
    if len(value) > limit:
        raise ToolArgumentError(f"{field} may hold at most {limit} entries.")
    return value


def _uuid_list(value: Any, field: str, limit: int) -> list[str] | None:
    items = _list_arg(value, field, limit)
    if items is None:
        return None
    out: list[str] = []
    for item in items:
        if not is_uuid(item):
            raise ToolArgumentError(f"{field} must hold ids from list_gc_contacts (short or full).")
        if str(item) not in out:
            out.append(str(item))
    return out


def _needs_by(value: Any, field: str = "needs_by") -> str | None:
    if value is None:
        return None
    text = parse_date(value, field)
    if not (NEEDS_BY_MIN <= date.fromisoformat(text) <= NEEDS_BY_MAX):
        raise ToolArgumentError(f"{field} must be between 2000-01-01 and 2100-12-31.")
    return text


def _stripped_fields(ctx: Any) -> set[str]:
    return {str(s.get("field")) for s in (getattr(ctx, "stripped", None) or ()) if isinstance(s, Mapping)}


# ── set_labor_numbers and restore_labor_numbers ──────────────────────────────

LABOR_KEYS = ("labor_amount", "labor_notes", "verified", "labor_breakdown")
LABOR_LABELS = {
    "labor_amount": "Labor amount",
    "labor_notes": "Notes",
    "verified": "Verified",
    "labor_breakdown": "Breakdown",
}

_BREAKDOWN_LINE = schema({
    "name": {"type": "string", "description": f"The line's label, at most {BREAKDOWN_NAME_MAX} characters."},
    "amount": nullable({"type": "string", "description": MONEY_DESC}),
})


def _labor_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    pid = require_uuid(args, "project_id")
    return [
        ToolRequest(route=LABOR_GET, path=f"/projects/{pid}/labor"),
        ToolRequest(route=PROJECT_GET, path=f"/projects/{pid}"),
    ]


def _breakdown_lines(value: Any) -> list[dict] | None:
    """Stored breakdown lines as {"name", "amount"}; an empty list is None."""
    if not isinstance(value, list):
        return None
    lines = [
        {"name": str(line.get("name") or ""), "amount": norm_value("amount", line.get("amount"))}
        for line in value if isinstance(line, Mapping)
    ]
    return lines or None


def _labor_values(row: Any) -> dict:
    """The four editable fields of a labor row, normalized. No row reads as
    the route's defaults; an empty notes string reads as None."""
    if not isinstance(row, Mapping):
        return {"labor_amount": None, "labor_notes": None, "verified": False, "labor_breakdown": None}
    notes = row.get("labor_notes")
    return {
        "labor_amount": norm_value("labor_amount", row.get("labor_amount")),
        "labor_notes": notes if isinstance(notes, str) and notes != "" else None,
        "verified": bool(row.get("verified")),
        "labor_breakdown": _breakdown_lines(row.get("labor_breakdown")),
    }


def labor_target(row: Any) -> dict:
    """reviewed_by and updated_at are in the target so an edit and undo by
    someone else still changes the hash."""
    present = isinstance(row, Mapping)
    return {
        "exists": present,
        **_labor_values(row),
        "reviewed_by": str(row.get("reviewed_by")) if present and row.get("reviewed_by") else None,
        "updated_at": norm_value("updated_at", row.get("updated_at")) if present else None,
    }


def _labor_reads(reads: list[Any]) -> tuple[Any, dict]:
    labor = reads[0] if reads and isinstance(reads[0], Mapping) else None
    project = dict_of(reads[1] if len(reads) > 1 else None)
    return labor, project


def _labor_snapshot(args: Mapping[str, Any], reads: list[Any]) -> tuple[dict, dict]:
    labor, project = _labor_reads(reads)
    return labor_target(labor), project_gate(project, labor=True)


def _parse_breakdown(value: Any) -> list[dict] | None:
    """None stays None; [] means clear; each line is {name, amount}."""
    lines = _list_arg(value, "labor_breakdown", BREAKDOWN_MAX_LINES)
    if lines is None:
        return None
    out = []
    for line in lines:
        if not isinstance(line, Mapping):
            raise ToolArgumentError("labor_breakdown lines must be objects with name and amount.")
        name = line.get("name")
        name = "" if name is None else _text(name, "labor_breakdown name", BREAKDOWN_NAME_MAX, allow_blank=True)
        amount = line.get("amount")
        out.append({"name": name, "amount": parse_money(amount, "labor_breakdown amount") if amount is not None else None})
    return out


def _stored_money(value: Any, field: str) -> str | None:
    """A stored amount as two decimals, with no size or sign limit."""
    if value is None:
        return None
    money = money_text(value)
    if money is None:
        raise ToolArgumentError(f"{field} must be a number.")
    return money


def _restore_labor_args(args: Mapping[str, Any]) -> dict:
    """The stored values a restore puts back, without the limits meant for
    the model's input: a human-entered row may hold longer notes, more
    lines, longer names or larger (or negative) amounts than the assistant
    may propose, and a revert must still restore them exactly."""
    notes = args.get("labor_notes")
    if notes is not None and not isinstance(notes, str):
        raise ToolArgumentError("labor_notes must be text.")
    lines = args.get("labor_breakdown")
    breakdown = None
    if lines is not None:
        if not isinstance(lines, list):
            raise ToolArgumentError("labor_breakdown must be a list.")
        breakdown = []
        for line in lines:
            if not isinstance(line, Mapping):
                raise ToolArgumentError("labor_breakdown lines must be objects with name and amount.")
            name = line.get("name")
            if name is not None and not isinstance(name, str):
                raise ToolArgumentError("labor_breakdown name must be text.")
            breakdown.append({"name": name or "", "amount": _stored_money(line.get("amount"), "labor_breakdown amount")})
    return {
        "labor_amount": _stored_money(args.get("labor_amount"), "labor_amount"),
        "labor_notes": notes,
        "verified": _bool_arg(args.get("verified"), "verified"),
        "labor_breakdown": breakdown,
    }


def _labor_args(args: Mapping[str, Any]) -> dict:
    amount = args.get("labor_amount")
    return {
        "labor_amount": parse_money(amount, "labor_amount") if amount is not None else None,
        "labor_notes": _text(args.get("labor_notes"), "labor_notes", TEXT_MAX, allow_blank=True),
        "verified": _bool_arg(args.get("verified"), "verified"),
        "labor_breakdown": _parse_breakdown(args.get("labor_breakdown")),
    }


def _breakdown_total(lines: Any) -> Decimal:
    return sum((Decimal(money_text(line.get("amount")) or "0") for line in lines or ()), Decimal("0"))


def _amount_overridden(current: Mapping[str, Any]) -> bool:
    """The labor panel's override test (PricingPanel.tsx laborSeedAmount):
    a stored amount that differs from the stored breakdown total by more
    than half a cent was typed over the total."""
    amount = current.get("labor_amount")
    if amount is None:
        return False
    return abs(Decimal(str(amount)) - _breakdown_total(current.get("labor_breakdown"))) > Decimal("0.005")


def _breakdown_text(lines: Any) -> str:
    if not lines:
        return "Empty"
    total = _breakdown_total(lines)
    count = len(lines)
    return f"{count} line{'s' if count != 1 else ''}, total {display('amount', money_text(total))}"


def _labor_rows(current: Mapping[str, Any], merged: Mapping[str, Any]) -> list[dict]:
    rows = []
    for key in LABOR_KEYS:
        if key == "labor_breakdown":
            rows.append({
                "field": key, "label": LABOR_LABELS[key],
                "before": _breakdown_text(current.get(key)), "after": _breakdown_text(merged.get(key)),
                "changed": current.get(key) != merged.get(key),
            })
        else:
            rows.append(_row(key, LABOR_LABELS[key], current.get(key), merged.get(key)))
    return rows


def _breakdown_items(before: Any, after: Any) -> list[dict]:
    before, after = list(before or []), list(after or [])
    if before == after:
        return []

    def line(item: Any) -> str:
        if not item:
            return "Empty"
        return f"{item.get('name') or 'Unnamed'}: {display('amount', item.get('amount'))}"

    return [
        {"label": f"Line {i + 1}", "before": line(before[i] if i < len(before) else None),
         "after": line(after[i] if i < len(after) else None)}
        for i in range(max(len(before), len(after)))
    ]


def _labor_summary(number: str, current: Mapping[str, Any], merged: Mapping[str, Any]) -> str:
    parts = []
    if merged["labor_amount"] != current["labor_amount"]:
        parts.append(f"amount {display('labor_amount', merged['labor_amount'])}")
    if merged["labor_notes"] != current["labor_notes"]:
        parts.append("notes")
    if merged["verified"] != current["verified"]:
        parts.append(f"verified {display('verified', merged['verified'])}")
    if merged["labor_breakdown"] != current["labor_breakdown"]:
        parts.append(f"breakdown {_breakdown_text(merged['labor_breakdown'])}")
    return f"Labor numbers for {number}: {', '.join(parts)}."


def _labor_proposal(args: Mapping[str, Any], reads: list[Any], merged: dict, *, title: str,
                    warnings: list[dict]) -> Proposal:
    labor, project = _labor_reads(reads)
    target, gate = labor_target(labor), project_gate(project, labor=True)
    current = {k: target[k] for k in LABOR_KEYS}
    pid = str(args["project_id"])
    if gate.get("bounces_verify"):
        warnings.append(dict(WARN_REVERIFY))
    warnings.extend(_phase_warnings(gate))
    number = project_label(project)
    preview = {
        "v": 1,
        "title": f"{title} on {number}",
        "tool_label": "Labor numbers",
        "project": project_ref(project),
        "rows": _labor_rows(current, merged),
        "items": _breakdown_items(current["labor_breakdown"], merged["labor_breakdown"]),
        "warnings": warnings,
        "similar": [],
    }
    request = ToolRequest(route=LABOR_PUT, path=f"/projects/{pid}/labor", json=dict(merged))
    return Proposal(request=request, project_id=pid, target=target, gate=gate, preview=preview,
                    summary=_labor_summary(number, current, merged))


def _set_labor_build(args: Mapping[str, Any], reads: list[Any], ctx: Any) -> Proposal:
    require_uuid(args, "project_id")
    # A line name the binding stripped is refused, never saved as "".
    lines = args.get("labor_breakdown")
    if "labor_breakdown.name" in _stripped_fields(ctx) and isinstance(lines, list) and any(
        isinstance(line, Mapping) and line.get("name") is None for line in lines
    ):
        raise ProposalRefused(STRIPPED_LINE_NAMES)
    given = _labor_args(args)
    labor, project = _labor_reads(reads)
    gate = project_gate(project, labor=True)
    _refuse_closed(gate)
    if all(given[k] is None for k in LABOR_KEYS):
        raise ProposalRefused("Nothing to change.")
    current = {k: v for k, v in labor_target(labor).items() if k in LABOR_KEYS}
    merged = dict(current)
    for key in LABOR_KEYS:
        if given[key] is not None:
            merged[key] = given[key]
    if given["labor_notes"] == "":
        merged["labor_notes"] = None
    if given["labor_breakdown"] == []:
        merged["labor_breakdown"] = None
    warnings = []
    # The labor panel saves the breakdown total as the amount unless the
    # amount was typed over it; pricing and Verify read only the amount.
    if given["labor_breakdown"] and given["labor_amount"] is None:
        if _amount_overridden(current):
            warnings.append(dict(WARN_AMOUNT_OVERRIDE_KEPT))
        else:
            merged["labor_amount"] = money_text(_breakdown_total(merged["labor_breakdown"]))
            if merged["labor_amount"] != current["labor_amount"]:
                warnings.append(dict(WARN_AMOUNT_FROM_BREAKDOWN))
    if merged == current:
        raise ProposalRefused("The labor numbers already have these values.")
    if given["labor_breakdown"] and current["labor_breakdown"]:
        warnings.append(dict(WARN_BREAKDOWN_REPLACED))
    return _labor_proposal(args, reads, merged, title="Set labor numbers", warnings=warnings)


def _restore_labor_build(args: Mapping[str, Any], reads: list[Any], ctx: Any) -> Proposal:
    require_uuid(args, "project_id")
    given = _restore_labor_args(args)
    labor, project = _labor_reads(reads)
    gate = project_gate(project, labor=True)
    _refuse_closed(gate)
    merged = {
        "labor_amount": given["labor_amount"],
        "labor_notes": given["labor_notes"] or None,
        "verified": bool(given["verified"]),
        "labor_breakdown": given["labor_breakdown"] or None,
    }
    current = {k: v for k, v in labor_target(labor).items() if k in LABOR_KEYS}
    if merged == current:
        raise ProposalRefused("The labor numbers already have these values.")
    warnings = []
    if args.get("original_bounced_verify"):
        warnings.append(dict(WARN_REVERIFY_NOT_RESTORED))
    if args.get("original_row_existed") is False:
        warnings.append(dict(WARN_LABOR_ROW_KEPT))
    return _labor_proposal(args, reads, merged, title="Restore labor numbers", warnings=warnings)


def _labor_revert_args(orig: Mapping[str, Any], before: Mapping[str, Any], after: Mapping[str, Any]) -> dict:
    """restore_labor_numbers args: the before target's values exactly (the
    route defaults when there was no row)."""
    target = dict_of(before.get("target"))
    existed = bool(target.get("exists"))
    values = _labor_values(None)
    if existed:
        values = {k: target.get(k) for k in LABOR_KEYS}
        values["verified"] = bool(values["verified"])
    return {
        "project_id": str(orig.get("project_id")),
        **values,
        "original_bounced_verify": bool(dict_of(before.get("gate")).get("bounces_verify")),
        "original_row_existed": existed,
    }


def _labor_bind_keep(args: Mapping[str, Any], reads: list[Any]) -> dict:
    """The current breakdown line names: a whole-list replace resends them,
    and they are this labor row's own data, so binding keeps them."""
    labor, _project = _labor_reads(reads)
    return {"labor_breakdown.name": [line["name"] for line in _labor_values(labor)["labor_breakdown"] or ()]}


def _labor_exec_headers(args: Mapping[str, Any], reads: list[Any]) -> dict:
    labor, _project = _labor_reads(reads)
    updated = labor.get("updated_at") if labor is not None else None
    return {LABOR_EXPECTED_HEADER: str(updated) if updated else "none"}


def _labor_result(payload: Any) -> dict:
    row = dict_of(payload)
    return {"labor_amount": norm_value("labor_amount", row.get("labor_amount")), "verified": bool(row.get("verified"))}


LABOR_WRITES_TABLES = (
    "labor_reviews", "audit_log", "projects", "project_category_state", "stage_events",
    "notifications", "verifications", "email_log",
)

register(ToolSpec(
    name="set_labor_numbers",
    namespace="bidding",
    sub_app="bidding",
    route=LABOR_PUT,
    extra_routes=(LABOR_GET, PROJECT_GET),
    effect=EFFECT_WRITE_INTERNAL,
    confirm=True,
    schema=schema({
        "project_id": PROJECT_ID_PROP,
        "labor_amount": nullable({"type": "string", "description": f"The labor total. {MONEY_DESC} Null leaves it unchanged."}),
        "labor_notes": nullable({"type": "string", "description": (
            f"Labor notes in the user's own words, at most {TEXT_MAX} characters. An empty string clears "
            "the notes; null leaves them unchanged.")}),
        "verified": nullable({"type": "boolean", "description": "The verified flag; null leaves it unchanged."}),
        "labor_breakdown": nullable({"type": "array", "items": _BREAKDOWN_LINE, "description": (
            f"At most {BREAKDOWN_MAX_LINES} lines. A list replaces the whole breakdown, an empty list clears "
            "it, null leaves it unchanged. With labor_amount null, the amount becomes the new breakdown total "
            "unless the stored amount was typed over the old total.")}),
    }),
    response_trimmer=_no_trim,
    progress_label="Preparing a labor numbers change",
    roles=WRITER_ROLES,
    description=(
        "Propose new labor numbers (amount, notes, verified flag, breakdown) on a project's bidding labor "
        "panel. Creates an approval card; nothing changes until the user approves it. Read get_labor first. "
        "Null leaves a field unchanged."
    ),
    requests=_labor_requests,
    write=WriteSpec(
        preview_requests=_labor_requests,
        build=_set_labor_build,
        snapshot=_labor_snapshot,
        bound_text_fields=("labor_notes", "labor_breakdown.name"),
        bind_keep=_labor_bind_keep,
        revert_tool="restore_labor_numbers",
        revert_args=_labor_revert_args,
        result_summary=_labor_result,
        exec_headers=_labor_exec_headers,
        writes_tables=LABOR_WRITES_TABLES,
    ),
))

register(ToolSpec(
    name="restore_labor_numbers",
    namespace="bidding",
    sub_app="bidding",
    route=LABOR_PUT,
    extra_routes=(LABOR_GET, PROJECT_GET),
    effect=EFFECT_WRITE_INTERNAL,
    confirm=True,
    schema=schema({
        "project_id": PROJECT_ID_PROP,
        "labor_amount": nullable({"type": "string", "description": f"{MONEY_DESC} Null clears it."}),
        "labor_notes": nullable({"type": "string", "description": "Null clears the notes."}),
        "verified": {"type": "boolean", "description": "The verified flag."},
        "labor_breakdown": nullable({"type": "array", "items": _BREAKDOWN_LINE, "description": "Null clears it."}),
        "original_bounced_verify": {"type": "boolean", "description": "The original change bounced Verify."},
        "original_row_existed": {"type": "boolean", "description": "A labor row existed before the original change."},
    }),
    response_trimmer=_no_trim,
    progress_label="Preparing a labor numbers restore",
    roles=WRITER_ROLES,
    description="Restore the labor numbers an earlier assistant change replaced (revert only, never offered).",
    requests=_labor_requests,
    offered=False,
    write=WriteSpec(
        preview_requests=_labor_requests,
        build=_restore_labor_build,
        snapshot=_labor_snapshot,
        result_summary=_labor_result,
        exec_headers=_labor_exec_headers,
        writes_tables=LABOR_WRITES_TABLES,
    ),
))


# ── update_project_fields ────────────────────────────────────────────────────

# app/models/schemas.py ASSISTANT_PROJECT_FIELDS (a test asserts equality).
PROJECT_FIELDS = (
    "name", "address", "est_start_date", "est_finish_date", "invitation_at", "job_walk_at",
    "labor_time", "wage_type", "labor_note", "notes", "bid_notes", "bidding_url", "no_bidding_url",
    "is_ngem", "is_rebid", "project_information", "trade_instructions",
)
CLEARABLE_FIELDS = (
    "address", "est_start_date", "est_finish_date", "invitation_at", "job_walk_at", "labor_time",
    "wage_type", "labor_note", "notes", "bid_notes", "project_information", "trade_instructions",
)
BOOL_FIELDS = frozenset({"no_bidding_url", "is_ngem", "is_rebid"})
LOCAL_DATETIME_FIELDS = frozenset({"invitation_at", "job_walk_at"})
PLAIN_DATE_FIELDS = frozenset({"est_start_date", "est_finish_date"})
BIDDING_KEYS = frozenset({"bidding_url", "no_bidding_url"})
FIELD_LIMITS = {"name": NAME_MAX, "project_information": LONG_TEXT_MAX, "trade_instructions": LONG_TEXT_MAX}

# The codes the app's pickers save (bdr_fe/lib/types.ts LABOR_TIMES and
# WAGE_TYPES) and that bid_conditions and analytics read. The columns are
# free text since migration 0049, so a custom value the user asks for still
# passes; the plain words for a code are saved as the code.
CODED_TEXT_FIELDS: dict[str, dict[str, str]] = {
    "labor_time": {
        "day_work": "day_work", "day": "day_work", "days": "day_work", "day_shift": "day_work",
        "daytime": "day_work", "day_time": "day_work",
        "night_work": "night_work", "night": "night_work", "nights": "night_work", "night_shift": "night_work",
        "nighttime": "night_work", "night_time": "night_work",
    },
    "wage_type": {
        "prevailing_wage": "prevailing_wage", "prevailing": "prevailing_wage", "prevailing_wages": "prevailing_wage",
        "pw": "prevailing_wage",
        "non_prevailing_wage": "non_prevailing_wage", "non_prevailing": "non_prevailing_wage",
        "nonprevailing": "non_prevailing_wage", "nonprevailing_wage": "non_prevailing_wage",
        "non_prevailing_wages": "non_prevailing_wage", "not_prevailing_wage": "non_prevailing_wage",
        "not_prevailing": "non_prevailing_wage",
    },
}
CODED_TEXT_DESC = {
    "labor_time": "Labor time: day_work or night_work (plain words like night or night work are saved as the code).",
    "wage_type": (
        "Wage type: prevailing_wage or non_prevailing_wage (plain words like prevailing wage are saved as the code)."
    ),
}


def coded_text(field: str, value: str) -> str:
    """`value` as its code when it names one in plain words (any case,
    spaces or hyphens), else unchanged."""
    codes = CODED_TEXT_FIELDS.get(field)
    if not codes:
        return value
    key = re.sub(r"[\s\-_]+", "_", value.strip().casefold()).strip("_")
    return codes.get(key, value)


FIELD_LABELS = {
    "name": "Name",
    "address": "Address",
    "est_start_date": "Estimated start",
    "est_finish_date": "Estimated finish",
    "invitation_at": "Invitation received",
    "job_walk_at": "Job walk",
    "labor_time": "Labor time",
    "wage_type": "Wage type",
    "labor_note": "Labor note",
    "notes": "Notes",
    "bid_notes": "Bid notes",
    "bidding_url": "Bidding URL",
    "no_bidding_url": "No bidding URL",
    "is_ngem": "NGEM",
    "is_rebid": "Rebid",
    "project_information": "Project information",
    "trade_instructions": "Trade instructions",
    # create_project_draft only
    "budgetary": "Budgetary",
    "internal_bid_at": "Internal bid date",
    "actual_bid_at": "Actual bid date",
    "due_from_estimator_at": "Due from estimator",
    "due_from_vendors_at": "Due from vendors",
    "project_type": "Project type",
    "owner_type": "Owner type",
    "labor_needed": "Labor needed",
    "bid_method": "Bid method",
    "competitor_known": "Competitors known",
    "gc_known": "GCs known",
    "subs_needed": "Subs needed",
    "est_value_band": "Estimated value",
    "scope_fit": "Scope fit",
}


def _field_prop(name: str) -> dict:
    if name in BOOL_FIELDS:
        return nullable({"type": "boolean", "description": f"{FIELD_LABELS[name]}. Null leaves it unchanged."})
    if name in LOCAL_DATETIME_FIELDS:
        desc = f"{FIELD_LABELS[name]}. {LOCAL_DESC}"
    elif name in PLAIN_DATE_FIELDS:
        desc = f"{FIELD_LABELS[name]}. {DATE_DESC}"
    elif name == "bidding_url":
        desc = "The bidding site link, exactly as the user typed it. Setting it answers the bidding URL question."
    elif name in CODED_TEXT_DESC:
        desc = CODED_TEXT_DESC[name]
    else:
        desc = f"{FIELD_LABELS[name]}, at most {FIELD_LIMITS.get(name, TEXT_MAX)} characters."
    return nullable({"type": "string", "description": f"{desc} Null leaves it unchanged."})


def _project_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    pid = require_uuid(args, "project_id")
    return [ToolRequest(route=PROJECT_GET, path=f"/projects/{pid}")]


def _changes_arg(args: Mapping[str, Any]) -> dict:
    changes = args.get("changes")
    if changes is None:
        return {}
    if not isinstance(changes, Mapping):
        raise ToolArgumentError("changes must be an object of field values.")
    unknown = sorted(set(changes) - set(PROJECT_FIELDS))
    if unknown:
        raise ToolArgumentError(f"The assistant cannot change: {', '.join(unknown)}.")
    return dict(changes)


def _clear_arg(args: Mapping[str, Any]) -> list[str]:
    clear = _list_arg(args.get("clear_fields"), "clear_fields", len(CLEARABLE_FIELDS)) or []
    bad = sorted({str(k) for k in clear} - set(CLEARABLE_FIELDS))
    if bad:
        raise ToolArgumentError(f"These fields cannot be cleared: {', '.join(bad)}.")
    return sorted({str(k) for k in clear})


def requested_project_keys(args: Mapping[str, Any]) -> list[str]:
    """The fields a call names (set or cleared), plus both bidding keys when
    either is named. The target covers exactly these, before and after the
    write, so the execute job's re-read and the after snapshot hash over the
    same keys."""
    changes = _changes_arg(args)
    keys = {k for k, v in changes.items() if v is not None} | set(_clear_arg(args))
    if keys & BIDDING_KEYS:
        keys |= BIDDING_KEYS
    return sorted(keys)


def _project_snapshot(args: Mapping[str, Any], reads: list[Any]) -> tuple[dict, dict]:
    project = dict_of(reads[0] if reads else None)
    target = {k: norm_value(k, project.get(k)) for k in requested_project_keys(args)}
    return target, project_gate(project)


def _parse_field(name: str, value: Any, *, is_revert: bool) -> Any:
    if name in BOOL_FIELDS:
        return _bool_arg(value, name)
    if name in LOCAL_DATETIME_FIELDS:
        return parse_local_datetime(value, name, allow_exact=is_revert)
    if name in PLAIN_DATE_FIELDS:
        return parse_date(value, name)
    if not isinstance(value, str):
        raise ToolArgumentError(f"{name} must be text.")
    if name == "name" and not value.strip():
        raise ToolArgumentError("name cannot be blank.")
    if name == "bidding_url":
        try:
            cleaned = _clean_bidding_url(value)
        except ValueError as exc:
            raise ToolArgumentError(str(exc)) from None
        if cleaned is None:
            raise ToolArgumentError("bidding_url cannot be blank; set no_bidding_url instead.")
        return cleaned
    # A revert restores the stored text whatever its length or shape.
    if is_revert:
        return value
    if not value.strip():
        raise ToolArgumentError(f"{name} cannot be blank; use clear_fields to empty it.")
    return coded_text(name, _text(value, name, FIELD_LIMITS.get(name, TEXT_MAX)))


def _project_patch(args: Mapping[str, Any], ctx: Any) -> dict:
    """Every named field with its new value (None for a clear), the bidding
    pair settled the way the route settles it."""
    changes = _changes_arg(args)
    clear = _clear_arg(args)
    is_revert = bool(getattr(ctx, "is_revert", False))
    full = {k: _parse_field(k, v, is_revert=is_revert) for k, v in changes.items() if v is not None}
    both = sorted(set(full) & set(clear))
    if both:
        raise ToolArgumentError(f"A field cannot be both set and cleared: {', '.join(both)}.")
    full.update({k: None for k in clear})
    if full.get("bidding_url"):
        if full.get("no_bidding_url") is True:
            raise ToolArgumentError("Set either bidding_url or no_bidding_url, not both.")
        full["no_bidding_url"] = False
    elif full.get("no_bidding_url") is True:
        full["bidding_url"] = None
    elif full.get("no_bidding_url") is False:
        raise ProposalRefused("Give the bidding URL.")
    if not full:
        if _stripped_fields(ctx):
            raise ProposalRefused("Every value came from untrusted text. Ask the user to type the values.")
        raise ProposalRefused("Nothing to change.")
    return full


def _update_build(args: Mapping[str, Any], reads: list[Any], ctx: Any) -> Proposal:
    pid = require_uuid(args, "project_id")
    full = _project_patch(args, ctx)
    project = dict_of(reads[0] if reads else None)
    target, gate = _project_snapshot(args, reads)
    _refuse_closed(gate)
    patch = {k: v for k, v in full.items() if norm_value(k, v) != target.get(k)}
    if patch.keys() & BIDDING_KEYS:
        patch.update({k: full[k] for k in BIDDING_KEYS if k in full})
    if not patch:
        raise ProposalRefused("The project already has these values.")
    keys = sorted(target)
    rows = [_row(k, FIELD_LABELS[k], project.get(k), full.get(k, project.get(k))) for k in keys]
    number = project_label(project)
    changed = [FIELD_LABELS[k].lower() for k in keys if k in patch]
    preview = {
        "v": 1,
        "title": f"Update project details on {number}",
        "tool_label": "Project details",
        "project": project_ref(project),
        "rows": rows,
        "items": [],
        "warnings": _phase_warnings(gate),
        "similar": [],
    }
    request = ToolRequest(route=PROJECT_PATCH, path=f"/projects/{pid}", json=patch)
    return Proposal(request=request, project_id=pid, target=target, gate=gate, preview=preview,
                    summary=f"Project {number}: {', '.join(changed)}.")


UNANSWERED_BIDDING = (
    "The bidding URL question was unanswered before this change, and BDR cannot un-answer it."
)


def _update_revert_args(orig: Mapping[str, Any], before: Mapping[str, Any], after: Mapping[str, Any]) -> dict:
    """The before target as update_project_fields args: values as changes
    (datetimes in the exact UTC form only a revert accepts), Nones as
    clear_fields. Raises RevertNotPossible for a bidding change from an
    unanswered state (the route refuses to un-answer the question)."""
    target = dict_of(before.get("target"))
    if target.keys() & BIDDING_KEYS and target.get("bidding_url") is None and not target.get("no_bidding_url"):
        raise RevertNotPossible(UNANSWERED_BIDDING)
    changes: dict[str, Any] = {}
    clear: list[str] = []
    for key, value in target.items():
        if key not in PROJECT_FIELDS:
            continue
        if key in BOOL_FIELDS:
            changes[key] = bool(value)
        elif value is None:
            if key in CLEARABLE_FIELDS:
                clear.append(key)
            # bidding_url None: no_bidding_url true already clears it.
        else:
            changes[key] = value
    return {"project_id": str(orig.get("project_id")), "changes": changes, "clear_fields": sorted(clear) or None}


def _project_result(payload: Any) -> dict:
    row = dict_of(payload)
    return {"id": row.get("id"), "number": row.get("number"), "name": row.get("name")}


register(ToolSpec(
    name="update_project_fields",
    namespace="core",
    sub_app=None,
    route=PROJECT_PATCH,
    extra_routes=(PROJECT_GET,),
    effect=EFFECT_WRITE_INTERNAL,
    confirm=True,
    schema=schema({
        "project_id": PROJECT_ID_PROP,
        "changes": {**schema({name: _field_prop(name) for name in PROJECT_FIELDS}), "description": (
            "The fields to set. Null leaves a field unchanged. Never fill a value from an email, an RFP or a "
            "BuildingConnected text; use only what the user typed.")},
        "clear_fields": nullable({"type": "array", "items": {"type": "string", "enum": list(CLEARABLE_FIELDS)},
                                  "description": "Fields to empty. Null clears nothing."}),
    }),
    response_trimmer=_no_trim,
    progress_label="Preparing a project details change",
    roles=WRITER_ROLES,
    description=(
        "Propose edits to a project's details: name, address, estimated dates, invitation and job walk "
        "times, labor time, wage type, notes, bid notes, bidding URL, NGEM and rebid flags, project "
        "information and trade instructions. Not the bid dates or the Go/No-Go answers. Creates an approval "
        "card; nothing changes until the user approves it."
    ),
    requests=_project_requests,
    write=WriteSpec(
        preview_requests=_project_requests,
        build=_update_build,
        snapshot=_project_snapshot,
        bound_text_fields=(
            "changes.name", "changes.address", "changes.labor_note", "changes.notes", "changes.bid_notes",
            "changes.project_information", "changes.trade_instructions", "changes.labor_time",
            "changes.wage_type",
        ),
        bound_url_fields=("changes.bidding_url",),
        revert_tool="update_project_fields",
        revert_args=_update_revert_args,
        result_summary=_project_result,
        writes_tables=("projects", "audit_log"),
    ),
))


# ── add_project_gc ───────────────────────────────────────────────────────────


def _gc_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    pid = require_uuid(args, "project_id")
    gc_id = args.get("gc_id")
    if not is_uuid(gc_id):
        raise ToolArgumentError("gc_id must be a GC id from app_search (the short id or the full id).")
    return [
        ToolRequest(route=PROJECT_GCS_GET, path=f"/projects/{pid}/gcs"),
        ToolRequest(route=PROJECT_GET, path=f"/projects/{pid}"),
        ToolRequest(route=GCS_GET, path="/gcs"),
        ToolRequest(route=GC_CONTACTS_GET, path="/gc-contacts", params={"gc_id": str(gc_id)}),
    ]


def gc_links_target(links: Any) -> list:
    """[gc_id, needs_by, sorted selected contact ids] per linked GC, sorted."""
    out = [
        [str(link.get("id")), norm_value("needs_by", link.get("needs_by")),
         sorted(str(c) for c in (link.get("selected_contact_ids") or []))]
        for link in rows_of(links) if link.get("id")
    ]
    return sorted(out, key=lambda item: item[0])


def _gc_contact_ids(args: Mapping[str, Any], reads: list[Any]) -> tuple[list[str], dict[str, dict]]:
    """(the chosen contact ids, {id: contact} of the GC's live contacts)."""
    gc_id = str(args.get("gc_id"))
    contact_ids = _uuid_list(args.get("contact_ids"), "contact_ids", CONTACTS_MAX) or []
    contacts = {
        str(c.get("id")): c for c in rows_of(reads[3] if len(reads) > 3 else None)
        if str(c.get("gc_id") or gc_id) == gc_id
    }
    return contact_ids, contacts


def _gc_snapshot(args: Mapping[str, Any], reads: list[Any]) -> tuple[dict, dict]:
    """The project's GC links plus the chosen contacts that are still live
    contacts of the GC: a contact deleted or moved after the preview
    changes the target, so the card is superseded (and then refused)
    instead of sending a POST the route answers 400 after linking the GC."""
    links = reads[0] if reads else None
    project = dict_of(reads[1] if len(reads) > 1 else None)
    contact_ids, contacts = _gc_contact_ids(args, reads)
    target = {"links": gc_links_target(links), "contacts": sorted(c for c in contact_ids if c in contacts)}
    return target, project_gate(project)


def _gc_build(args: Mapping[str, Any], reads: list[Any], ctx: Any) -> Proposal:
    pid = require_uuid(args, "project_id")
    gc_id = str(args.get("gc_id"))
    needs_by = _needs_by(args.get("needs_by"))
    contact_ids, contacts = _gc_contact_ids(args, reads)
    target, gate = _gc_snapshot(args, reads)
    _refuse_closed(gate)
    project = dict_of(reads[1] if len(reads) > 1 else None)
    gc = next((g for g in rows_of(reads[2] if len(reads) > 2 else None) if str(g.get("id")) == gc_id), None)
    if gc is None:
        raise ProposalRefused("Unknown GC; use app_search to find it.")
    gc_name = str(gc.get("name") or "The GC")
    if any(item[0] == gc_id for item in target["links"]):
        raise ProposalRefused(f"{gc_name} is already on this project.")
    unknown = [cid for cid in contact_ids or () if cid not in contacts]
    if unknown:
        raise ProposalRefused(f"Some contacts are not contacts of {gc_name}; use list_gc_contacts to find them.")
    names = [str(contacts[cid].get("name") or "Unnamed") for cid in contact_ids or ()]
    number = project_label(project)
    preview = {
        "v": 1,
        "title": f"Add a GC to {number}",
        "tool_label": "Project GCs",
        "project": project_ref(project),
        "rows": [
            _row("gc", "GC", None, gc_name),
            _row("needs_by", "Needs by", None, needs_by),
            _row("contacts", "Contacts", None, ", ".join(names) or None),
        ],
        "items": [],
        "warnings": _phase_warnings(gate),
        "similar": [],
    }
    request = ToolRequest(route=PROJECT_GCS_POST, path=f"/projects/{pid}/gcs",
                          json={"gc_id": gc_id, "needs_by": needs_by, "contact_ids": contact_ids or None})
    return Proposal(request=request, project_id=pid, target=target, gate=gate, preview=preview,
                    summary=f"Add {gc_name} to {number}.")


def _gc_created(args: Mapping[str, Any], payload: Any) -> dict | None:
    pid = str(args.get("project_id") or "")
    if not pid:
        return None
    gc = next((g for g in rows_of(payload) if str(g.get("id")) == str(args.get("gc_id"))), {})
    return {"kind": "project_gc", "id": pid, "label": gc.get("name"), "href": f"/projects/{pid}"}


register(ToolSpec(
    name="add_project_gc",
    namespace="bidding",
    sub_app="bidding",
    route=PROJECT_GCS_POST,
    extra_routes=(PROJECT_GCS_GET, PROJECT_GET, GCS_GET, GC_CONTACTS_GET),
    effect=EFFECT_WRITE_INTERNAL,
    confirm=True,
    schema=schema({
        "project_id": PROJECT_ID_PROP,
        "gc_id": {"type": "string", "description": "The GC's id (short or full) from app_search with kind gcs."},
        "needs_by": nullable({"type": "string", "description": f"When this GC needs our number. {DATE_DESC}"}),
        "contact_ids": nullable({"type": "array", "items": {"type": "string"}, "description": (
            f"Contacts at this GC to bid to, ids from list_gc_contacts, at most {CONTACTS_MAX}.")}),
    }),
    response_trimmer=_no_trim,
    progress_label="Preparing a GC addition",
    roles=WRITER_ROLES,
    description=(
        "Propose adding a general contractor to a project's bid list, with an optional needs-by date and "
        "contacts. Creates an approval card; nothing changes until the user approves it. Cannot be undone "
        "by the assistant."
    ),
    requests=_gc_requests,
    write=WriteSpec(
        preview_requests=_gc_requests,
        build=_gc_build,
        snapshot=_gc_snapshot,
        creates="project_gc",
        created_ref=_gc_created,
        result_summary=lambda payload: {"gc_count": len(rows_of(payload))},
        writes_tables=("project_gcs", "project_gc_contacts", "audit_log"),
    ),
))


# ── create_project_draft ─────────────────────────────────────────────────────

# The nine Go/No-Go answers: the value lists of the Literal types in
# app/models/schemas.py, with a plain-language hint each.
GONO_FIELDS: dict[str, tuple[Any, str]] = {
    "project_type": (ProjectType, "What kind of job (new construction, tenant improvement, casino...)."),
    "owner_type": (OwnerType, "Who owns the project (public agency, casino, private...)."),
    "labor_needed": (LaborNeeded, "The labor the job needs (union, non-union...)."),
    "bid_method": (BidMethod, "How the job is bid (hard bid, CMAR...)."),
    "competitor_known": (CompetitorKnown, "Whether we know the other electrical contractors bidding."),
    "gc_known": (GCKnown, "Whether we know the GCs bidding."),
    "subs_needed": (SubsNeeded, "Whether we need subcontractors and which."),
    "est_value_band": (EstValueBand, "The estimated value of our scope."),
    "scope_fit": (ScopeFit, "Whether the scope fits G3 Electrical."),
}
GONO_VALUES: dict[str, tuple[str, ...]] = {k: tuple(typing.get_args(t)) for k, (t, _hint) in GONO_FIELDS.items()}
CREATE_DATETIMES = ("internal_bid_at", "invitation_at", "due_from_estimator_at", "due_from_vendors_at")
CREATE_TEXTS = ("labor_time", "wage_type", "labor_note", "notes", "address", "bid_notes")
CREATE_BOOLS = ("budgetary", "no_bidding_url", "is_ngem", "is_rebid")
CREATE_DATES = ("est_start_date", "est_finish_date")
CREATE_ROW_ORDER = (
    "name", "budgetary", *CREATE_DATETIMES, "actual_bid_at", *CREATE_DATES, "address", "labor_time",
    "wage_type", "labor_note", "notes", "bid_notes", "bidding_url", "no_bidding_url", "is_ngem", "is_rebid",
    *GONO_FIELDS,
)

_CREATE_GC = schema({
    "gc_id": {"type": "string", "description": "The GC's id (short or full) from app_search with kind gcs."},
    "needs_by": nullable({"type": "string", "description": f"When this GC needs our number. {DATE_DESC}"}),
    "contact_ids": nullable({"type": "array", "items": {"type": "string"},
                             "description": f"Contacts from list_gc_contacts, at most {CONTACTS_MAX}."}),
})


def _create_schema() -> dict:
    props: dict[str, dict] = {
        "name": {"type": "string", "description": f"The project name the user typed, 1 to {NAME_MAX} characters."},
        "budgetary": {"type": "boolean", "description": "True for a budgetary estimate (the number gets a B)."},
        "actual_bid_at": nullable({"type": "string", "description": f"When the GC wants the proposal. {LOCAL_DESC}"}),
        "bidding_url": nullable({"type": "string", "description": (
            "The bidding site link exactly as the user typed it. Give it or set no_bidding_url, not both.")}),
        "no_bidding_url": {"type": "boolean", "description": "True when the user said the project has no bidding link."},
        "is_ngem": {"type": "boolean", "description": "True when the project came from NGEM."},
        "is_rebid": {"type": "boolean", "description": "True when the job was bid before under another invitation."},
        "gcs": nullable({"type": "array", "items": _CREATE_GC,
                         "description": f"The GCs bidding the job, at most {GCS_MAX}."}),
    }
    for name in CREATE_DATETIMES:
        props[name] = {"type": "string", "description": f"{FIELD_LABELS[name]}, required. {LOCAL_DESC}"}
    for name in CREATE_DATES:
        props[name] = nullable({"type": "string", "description": f"{FIELD_LABELS[name]}. {DATE_DESC}"})
    for name in CREATE_TEXTS:
        desc = CODED_TEXT_DESC.get(name) or f"{FIELD_LABELS[name]}, at most {TEXT_MAX} characters."
        props[name] = nullable({"type": "string", "description": desc})
    for name, (_type, hint) in GONO_FIELDS.items():
        props[name] = {"type": "string", "enum": list(GONO_VALUES[name]),
                       "description": f"Go/No-Go answer, required. {hint}"}
    return schema(props)


def _create_name(args: Mapping[str, Any]) -> str | None:
    name = args.get("name")
    if name is None:
        return None
    if not isinstance(name, str) or not name.strip():
        raise ToolArgumentError("name cannot be blank.")
    if len(name) > NAME_MAX:
        raise ToolArgumentError(f"name may be at most {NAME_MAX} characters.")
    return name


def _create_gcs(args: Mapping[str, Any]) -> list[dict]:
    entries = _list_arg(args.get("gcs"), "gcs", GCS_MAX) or []
    out: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping) or not is_uuid(entry.get("gc_id")):
            raise ToolArgumentError("Each gcs entry needs a gc_id from app_search (the short id or the full id).")
        gc_id = str(entry["gc_id"])
        if gc_id in seen:
            raise ToolArgumentError("Each GC may be listed once.")
        seen.add(gc_id)
        out.append({
            "gc_id": gc_id,
            "needs_by": _needs_by(entry.get("needs_by"), "gcs needs_by"),
            "contact_ids": _uuid_list(entry.get("contact_ids"), "gcs contact_ids", CONTACTS_MAX),
        })
    return out


def _create_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    """POST /projects/similar, then GET /gcs when GCs are named, then GET
    /gc-contacts once per GC with contacts, in the order of `gcs`. A name the
    binding stripped sends nothing (build refuses with the reason)."""
    name = _create_name(args)
    if name is None:
        return []
    out = [ToolRequest(route=SIMILAR_POST, path="/projects/similar", json={"name": name})]
    gcs = _create_gcs(args)
    if gcs:
        out.append(ToolRequest(route=GCS_GET, path="/gcs"))
    for gc in gcs:
        if gc["contact_ids"]:
            out.append(ToolRequest(route=GC_CONTACTS_GET, path="/gc-contacts", params={"gc_id": gc["gc_id"]}))
    return out


def _similar_rows(reads: list[Any]) -> tuple[list[dict], list[dict]]:
    body = dict_of(reads[0] if reads else None)
    return rows_of(body.get("similar")), rows_of(body.get("possible_rebids"))


def _create_snapshot(args: Mapping[str, Any], reads: list[Any]) -> tuple[dict, dict]:
    similar, rebids = _similar_rows(reads)
    ids = sorted({str(r.get("id")) for r in [*similar, *rebids] if r.get("id")})
    return {"similar_ids": ids}, {"closed": False}


def _similar_text(rows: list[dict]) -> str:
    return "; ".join(f"{r.get('number') or 'no number'} {r.get('name') or ''}".strip() for r in rows)


def _create_build(args: Mapping[str, Any], reads: list[Any], ctx: Any) -> Proposal:
    stripped = _stripped_fields(ctx)
    name = _create_name(args)
    if name is None:
        if "name" in stripped:
            raise ProposalRefused("The project name came from untrusted text. Ask the user to type it.")
        raise ToolArgumentError("name is required.")
    body: dict[str, Any] = {"name": name}
    for field in CREATE_DATETIMES:
        if args.get(field) is None:
            raise ToolArgumentError(f"{field} is required. Ask the user for it.")
        body[field] = parse_local_datetime(args[field], field)
    actual = args.get("actual_bid_at")
    if actual is not None and getattr(ctx, "role", None) not in ACTUAL_BID_EDITOR_ROLES:
        raise ProposalRefused("Your role cannot set the actual bid date.")
    body["actual_bid_at"] = parse_local_datetime(actual, "actual_bid_at") if actual is not None else None
    for field in GONO_FIELDS:
        value = args.get(field)
        if value not in GONO_VALUES[field]:
            raise ToolArgumentError(f"{field} must be one of: {', '.join(GONO_VALUES[field])}. Ask the user.")
        body[field] = value
    for field in CREATE_BOOLS:
        body[field] = bool(_bool_arg(args.get(field), field))
    for field in CREATE_DATES:
        body[field] = parse_date(args[field], field) if args.get(field) is not None else None
    for field in CREATE_TEXTS:
        value = _text(args.get(field), field, TEXT_MAX, allow_blank=True)
        body[field] = coded_text(field, value) if value and value.strip() else None
    url = args.get("bidding_url")
    if url is not None:
        try:
            url = _clean_bidding_url(url)
        except ValueError as exc:
            raise ToolArgumentError(str(exc)) from None
    if url and body["no_bidding_url"]:
        raise ProposalRefused("Ask whether there is a bidding URL: give it or say there is none, not both.")
    if not url and not body["no_bidding_url"]:
        if "bidding_url" in stripped:
            raise ProposalRefused("Ask the user to type the bidding URL.")
        raise ProposalRefused("Ask whether there is a bidding URL.")
    body["bidding_url"] = url

    gcs = _create_gcs(args)
    gc_rows: dict[str, dict] = {}
    index = 1
    if gcs:
        gc_rows = {str(g.get("id")): g for g in rows_of(reads[index] if len(reads) > index else None)}
        index += 1
    items = []
    for gc in gcs:
        known = gc_rows.get(gc["gc_id"])
        if known is None:
            raise ProposalRefused("Unknown GC; use app_search to find it.")
        gc_name = str(known.get("name") or "The GC")
        names: list[str] = []
        if gc["contact_ids"]:
            contacts = {str(c.get("id")): c for c in rows_of(reads[index] if len(reads) > index else None)
                        if str(c.get("gc_id") or gc["gc_id"]) == gc["gc_id"]}
            index += 1
            if any(cid not in contacts for cid in gc["contact_ids"]):
                raise ProposalRefused(
                    f"Some contacts are not contacts of {gc_name}; use list_gc_contacts to find them."
                )
            names = [str(contacts[cid].get("name") or "Unnamed") for cid in gc["contact_ids"]]
        detail = f"needs by {gc['needs_by']}" if gc["needs_by"] else "no needs-by date"
        if names:
            detail += f"; contacts {', '.join(names)}"
        items.append({"label": f"GC: {gc_name}", "before": "Empty", "after": detail})
    body["gcs"] = [
        {"gc_id": gc["gc_id"], "needs_by": gc["needs_by"], "contact_ids": gc["contact_ids"]} for gc in gcs
    ]

    target, gate = _create_snapshot(args, reads)
    similar, rebids = _similar_rows(reads)
    warnings: list[dict] = []
    if similar:
        warnings.append({"code": "similar_projects",
                         "text": f"Similar projects already exist: {_similar_text(similar)}."})
    if rebids:
        warnings.append({"code": "possible_rebid", "text": f"This may be a rebid of: {_similar_text(rebids)}."})
    rows = []
    for field in CREATE_ROW_ORDER:
        value = body.get(field)
        row = _row(field, FIELD_LABELS[field], None, value)
        row["changed"] = row["after"] != "Empty"
        rows.append(row)
    preview = {
        "v": 1,
        "title": "Create a new project",
        "tool_label": "New project",
        "project": None,
        "rows": rows,
        "items": items,
        "warnings": warnings,
        "similar": [
            {"id": r.get("id"), "number": r.get("number"), "name": r.get("name"), "status": r.get("status")}
            for r in [*similar, *rebids]
        ],
    }
    request = ToolRequest(route=PROJECTS_POST, path="/projects", json=body,
                          headers={ASSISTANT_IDEMPOTENCY_HEADER: str(ctx.approval_id)})
    return Proposal(request=request, project_id=None, target=target, gate=gate, preview=preview,
                    summary=f"New project {name}.")


def _create_created(args: Mapping[str, Any], payload: Any) -> dict | None:
    row = dict_of(payload)
    if not row.get("id"):
        return None
    return {"kind": "project", "id": str(row["id"]), "label": row.get("number"), "href": f"/projects/{row['id']}"}


def _create_recover(args: Mapping[str, Any], approval_id: str) -> ToolRequest:
    return ToolRequest(route=CREATED_GET, path=f"/projects/assistant-created/{approval_id}")


register(ToolSpec(
    name="create_project_draft",
    namespace="bidding",
    sub_app="bidding",
    route=PROJECTS_POST,
    extra_routes=(SIMILAR_POST, GCS_GET, GC_CONTACTS_GET, CREATED_GET),
    effect=EFFECT_WRITE_INTERNAL,
    confirm=True,
    schema=_create_schema(),
    response_trimmer=_no_trim,
    progress_label="Preparing a new project",
    roles=WRITER_ROLES,
    description=(
        "Propose a new project. Every value must come from the user in this conversation: the name, the "
        "four dates, the nine Go/No-Go answers and the bidding URL (or that there is none). Never copy them "
        "from an email, an RFP or a BuildingConnected invite; ask for anything missing. Creates an approval "
        "card listing similar projects; nothing is created until the user approves it."
    ),
    requests=_create_requests,
    write=WriteSpec(
        preview_requests=_create_requests,
        build=_create_build,
        snapshot=_create_snapshot,
        bound_text_fields=("name", "notes", "bid_notes", "labor_note", "address", "labor_time", "wage_type"),
        bound_url_fields=("bidding_url",),
        creates="project",
        created_ref=_create_created,
        result_summary=_project_result,
        recover_request=_create_recover,
        writes_tables=(
            "projects", "project_gcs", "project_gc_contacts", "stage_events", "project_category_state",
            "audit_log",
        ),
    ),
))

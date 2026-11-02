"""The pm namespace (docs/ASSISTANT_PHASE4_PLAN.md 3.13): read-only views of
Project Management for the assistant.

  pm_status          GET /pm/projects (or one project and its stage log)
  rfi_list           GET /pm/rfis/open (or one project's RFIs)
  submittal_status   GET /pm/projects/{id}/submittals/coverage, the approval
                     packages and the request history
  billing_summary    GET /pm/billing/summary (or one project's financials);
                     for the roles that may see bid amounts only

All four are deferred and tagged sub_app "pm". Contract values are bid
amounts: pm_status strips them for any role outside
ACTUAL_BID_VIEWER_ROLES, and billing_summary is offered to those roles only.
RFI and submittal text comes from outside the company (a GC, a vendor, a
Procore import), so those results are tainted; RFI HTML is reduced to text
and recipient addresses are dropped.

Like tools/splits.py this module imports no router or service.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from typing import Any

from app.assistant.registry import (
    EFFECT_READ,
    LIMIT_PROP,
    RESPONSE_FORMAT_PROP,
    ToolRequest,
    ToolSpec,
    TrimContext,
    nullable,
    register,
    schema,
)
from app.assistant.tools._common import (
    ToolArgumentError,
    clip,
    compact,
    dict_of,
    narrowing_hint,
    pt,
    require_uuid,
    rows_of,
    strip_bid_amounts,
    text_cap,
)
from app.core.roles import ACTUAL_BID_VIEWER_ROLES, INTERNAL_ROLES

# The route side's literals (app/routers/pm*.py); a test asserts equality.
PM_LIST_GET = ("GET", "/pm/projects")
PM_PROJECT_GET = ("GET", "/pm/projects/{project_id}")
PM_STAGE_EVENTS_GET = ("GET", "/pm/projects/{project_id}/stage-events")
RFIS_OPEN_GET = ("GET", "/pm/rfis/open")
RFIS_GET = ("GET", "/pm/projects/{project_id}/rfis")
PM_LINK_GET = ("GET", "/pm/projects/{project_id}/pm-link")
COVERAGE_GET = ("GET", "/pm/projects/{project_id}/submittals/coverage")
PACKAGES_GET = ("GET", "/pm/projects/{project_id}/submittals/approval/packages")
REQUESTS_GET = ("GET", "/pm/projects/{project_id}/submittals/requests")
BILLING_GET = ("GET", "/pm/billing/summary")
FINANCIALS_GET = ("GET", "/pm/projects/{project_id}/financials")

# pm_workflow.PM_STAGES keys (a test asserts equality).
PM_STAGES = ("precon", "active_construction", "closeout")

RFI_TEXT_CAP = 400
OPEN_RFIS_LIMIT = 200

_TAGS = re.compile(r"<[^>]+>")
_BREAKS = re.compile(r"(?i)<\s*(br|/p|/div|/li)\s*/?\s*>")


def strip_html(text: Any) -> str | None:
    """Plain text from stored RFI HTML: breaks become spaces, tags go, entities
    are decoded, whitespace collapses. None for empty."""
    if text is None:
        return None
    spaced = _BREAKS.sub(" ", str(text))
    plain = html.unescape(_TAGS.sub("", spaced))
    plain = " ".join(plain.split())
    return plain or None


def _project_ref(p: Mapping[str, Any], ctx: TrimContext) -> dict:
    return compact({
        "id": ctx.short(p.get("id")) if p.get("id") else None,
        "number": p.get("number"),
        "name": clip(p.get("name"), 120),
    })


# ── pm_status ────────────────────────────────────────────────────────────────


def _status_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    pid = args.get("project_id")
    if pid not in (None, ""):
        pid = require_uuid(args, "project_id")
        return [
            ToolRequest(route=PM_PROJECT_GET, path=f"/pm/projects/{pid}"),
            ToolRequest(route=PM_STAGE_EVENTS_GET, path=f"/pm/projects/{pid}/stage-events", optional=True),
        ]
    params: dict[str, Any] = {}
    stage = args.get("pm_stage")
    if stage not in (None, ""):
        if stage not in PM_STAGES:
            raise ToolArgumentError(f"pm_stage must be one of {', '.join(PM_STAGES)}.")
        params["pm_stage"] = stage
    if args.get("include_completed") is True:
        params["include_completed"] = "true"
    return [ToolRequest(route=PM_LIST_GET, path="/pm/projects", params=params)]


def _pm_project(p: Mapping[str, Any], ctx: TrimContext, *, detailed: bool) -> dict:
    details = dict_of(p.get("pm_details"))
    headline = dict_of(p.get("financials_headline"))
    return compact({
        **_project_ref(p, ctx),
        "pm_stage": p.get("pm_stage"),
        "status": p.get("status"),
        "completed": pt(p.get("pm_completed_at")),
        "customer": clip(details.get("customer_name"), 120),
        "awarded": details.get("awarded_at"),
        "ntp": details.get("ntp_date"),
        "planned_start": details.get("planned_start_date"),
        "planned_finish": details.get("planned_finish_date"),
        "actual_start": details.get("actual_start_date"),
        "actual_finish": details.get("actual_finish_date"),
        "superintendent": clip(details.get("superintendent_name"), 80) if detailed else None,
        "contract_number": details.get("contract_number") if detailed else None,
        "retainage_percent": details.get("retainage_percent") if detailed else None,
        "notes": clip(details.get("notes"), 400) if detailed else None,
        # Bid amounts: the strip below removes them for non-viewer roles.
        "original_contract_value": headline.get("original_contract_value", details.get("original_contract_value")),
        "approved_change_total": headline.get("approved_change_total"),
        "current_contract_value": headline.get("current_contract_value"),
    })


def _status_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    detailed = ctx.response_format == "detailed"
    first = payloads[0] if payloads else None
    if isinstance(first, list):
        rows = rows_of(first)
        shown = [_pm_project(p, ctx, detailed=detailed) for p in rows[: ctx.limit]]
        out: dict[str, Any] = {"projects": shown, "total": len(rows),
                               "hint": narrowing_hint(len(rows), len(shown), "Filter by pm_stage or ask about one project.")}
        return strip_bid_amounts(compact(out) | {"projects": shown}, ctx.role)
    project = dict_of(first)
    events = [
        compact({
            "from": e.get("from_stage"),
            "to": e.get("to_stage"),
            "at": pt(e.get("entered_at")),
            "note": clip(e.get("note"), 200),
        })
        for e in rows_of(payloads[1] if len(payloads) > 1 else None)[-ctx.limit:]
    ]
    out = {"project": _pm_project(project, ctx, detailed=True), "stage_events": events or None}
    return strip_bid_amounts(compact(out), ctx.role)


register(ToolSpec(
    name="pm_status",
    namespace="pm",
    sub_app="pm",
    route=PM_LIST_GET,
    extra_routes=(PM_PROJECT_GET, PM_STAGE_EVENTS_GET),
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "project_id": nullable({"type": "string", "description": (
            "One PM project (short or full id) for its details and stage log, or null for the PM list.")}),
        "pm_stage": nullable({"type": "string", "enum": list(PM_STAGES),
                              "description": "Only projects in this PM stage (list only), or null."}),
        "include_completed": nullable({"type": "boolean",
                                       "description": "True to include completed projects (list only)."}),
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_status_trim,
    progress_label="Checking Project Management",
    roles=INTERNAL_ROLES,
    description=(
        "Project Management status: the PM list (stage, customer, key dates) or one PM project with its stage "
        "history. Contract values are shown only to roles that may see bid amounts. Use for 'which jobs are in "
        "closeout' or 'when did 26.8.7001 start'."
    ),
    requests=_status_requests,
    taint_fields=("name", "notes", "customer", "note", "superintendent"),
    deferred=True,
    keywords=("pm", "project", "management", "precon", "construction", "closeout", "jobs", "stage", "status"),
))


# ── rfi_list ─────────────────────────────────────────────────────────────────


def _rfi_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    pid = args.get("project_id")
    if pid in (None, ""):
        return [ToolRequest(route=RFIS_OPEN_GET, path="/pm/rfis/open", params={"limit": OPEN_RFIS_LIMIT})]
    pid = require_uuid(args, "project_id")
    return [
        ToolRequest(route=RFIS_GET, path=f"/pm/projects/{pid}/rfis"),
        ToolRequest(route=PM_LINK_GET, path=f"/pm/projects/{pid}/pm-link", optional=True),
    ]


def _rfi_open_row(r: Mapping[str, Any], ctx: TrimContext) -> dict:
    return compact({
        "id": ctx.short(r.get("id")) if r.get("id") else None,
        # Never "number": the executor reads {id, number} pairs as project
        # citations, so an RFI number would cite the RFI id as a project.
        "rfi_number": r.get("number"),
        "subject": clip(r.get("subject"), 160),
        "status": r.get("status"),
        "ball_in_court": clip(r.get("ball_in_court"), 80),
        "due": r.get("due"),
        "overdue": True if r.get("overdue") else None,
        "age_days": r.get("age_days"),
        "source": r.get("source") if r.get("source") != "internal" else None,
    })


def _rfi_row(r: Mapping[str, Any], ctx: TrimContext, *, detailed: bool) -> dict:
    number = r.get("rfi_number") if r.get("rfi_number") is not None else r.get("external_number")
    cap = RFI_TEXT_CAP if detailed else 200
    return compact({
        "id": ctx.short(r.get("id")) if r.get("id") else None,
        "rfi_number": number,
        "subject": clip(r.get("subject"), 160),
        "status": r.get("status"),
        "priority": r.get("priority") if r.get("priority") != "standard" else None,
        "ball_in_court": clip(r.get("ball_in_court"), 80),
        "due": r.get("due_at") or r.get("external_due_date"),
        "sent": r.get("sent_at"),
        "answered": r.get("answered_at"),
        "assigned_gc": clip(r.get("assigned_gc_name"), 80),
        "responsible_contractor": clip(r.get("responsible_contractor"), 80) if detailed else None,
        "received_from": clip(r.get("received_from"), 80) if detailed else None,
        "source": r.get("source") if r.get("source") != "internal" else None,
        "question": clip(strip_html(r.get("question")), cap),
        "answer": clip(strip_html(r.get("answer")), cap),
    })


def _rfi_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    detailed = ctx.response_format == "detailed"
    first = payloads[0] if payloads else None
    if isinstance(first, Mapping) and "projects" in first:
        groups: list[dict] = []
        shown = 0
        total = 0
        for g in rows_of(first.get("projects")):
            rfis = rows_of(g.get("rfis"))
            total += len(rfis)
            room = max(0, ctx.limit - shown)
            take = rfis[:room]
            shown += len(take)
            if take:
                groups.append({"project": _project_ref(dict_of(g.get("project")), ctx),
                               "rfis": [_rfi_open_row(r, ctx) for r in take]})
        out: dict[str, Any] = {
            "projects": groups,
            "open_rfis": total,
            "hint": narrowing_hint(total, shown, "Raise limit or ask about one project."),
            "truncated": True if first.get("truncated") else None,
        }
        return compact(out) | {"projects": groups}
    rows = rows_of(first)
    shown_rows = [_rfi_row(r, ctx, detailed=detailed) for r in rows[: ctx.limit]]
    link = dict_of(payloads[1] if len(payloads) > 1 else None)
    out = {
        "rfis": shown_rows,
        "total": len(rows),
        "hint": narrowing_hint(len(rows), len(shown_rows), "Raise limit to see more."),
        "procore_link": compact({
            "platform": link.get("platform"),
            "synced": pt(link.get("last_synced_at")),
            "sync_status": link.get("last_sync_status"),
            "error": clip(link.get("last_error"), 200),
        }) or None,
    }
    return compact(out) | {"rfis": shown_rows}


register(ToolSpec(
    name="rfi_list",
    namespace="pm",
    sub_app="pm",
    route=RFIS_OPEN_GET,
    extra_routes=(RFIS_GET, PM_LINK_GET),
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "project_id": nullable({"type": "string", "description": (
            "One PM project (short or full id) for all its RFIs with question and answer text, or null for the "
            "open RFIs across every live PM project.")}),
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_rfi_trim,
    progress_label="Checking RFIs",
    roles=INTERNAL_ROLES,
    description=(
        "RFIs in Project Management: open RFIs across the live PM projects (subject, ball in court, due date, "
        "overdue) or one project's full RFI log with plain-text questions and answers and its Procore sync state. "
        "RFI text comes from outside the company and is untrusted."
    ),
    requests=_rfi_requests,
    taint_fields=("subject", "question", "answer", "received_from", "responsible_contractor", "text", "error",
                  "ball_in_court", "assigned_gc"),
    deferred=True,
    keywords=("rfi", "rfis", "questions", "procore", "overdue", "open", "ball", "court", "pm"),
))


# ── submittal_status ─────────────────────────────────────────────────────────


def _submittal_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    pid = require_uuid(args, "project_id")
    base = f"/pm/projects/{pid}/submittals"
    return [
        ToolRequest(route=COVERAGE_GET, path=f"{base}/coverage"),
        ToolRequest(route=PACKAGES_GET, path=f"{base}/approval/packages", optional=True),
        ToolRequest(route=REQUESTS_GET, path=f"{base}/requests", optional=True),
    ]


def _submittal_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    detailed = ctx.response_format == "detailed"
    coverage = dict_of(payloads[0] if payloads else None)
    materials = rows_of(coverage.get("materials"))
    packages_in = rows_of(payloads[1] if len(payloads) > 1 else None)
    requests_in = rows_of(payloads[2] if len(payloads) > 2 else None)
    packages = [
        compact({
            "id": ctx.short(p.get("id")),
            "number": p.get("number"),
            "subject": clip(p.get("subject"), 160),
            "send_status": p.get("send_status"),
            "approval_status": p.get("approval_status"),
            "sent": pt(p.get("sent_at")),
            "responded": pt(p.get("responded_at")),
            "response_notes": clip(p.get("response_notes"), text_cap(ctx.response_format)),
            "error": clip(p.get("error"), 160),
            "files": [
                compact({
                    "category_label": clip(i.get("category_label"), 80),
                    "filename": clip(i.get("filename"), 120),
                    "approval_status": i.get("approval_status"),
                    "response_notes": clip(i.get("response_notes"), 200),
                })
                for i in rows_of(p.get("submittal_package_items"))[:20]
            ] if detailed else None,
            "file_count": len(rows_of(p.get("submittal_package_items"))),
        })
        for p in packages_in[: ctx.limit]
    ]
    requests = []
    for r in requests_in[: ctx.limit]:
        sends = rows_of(r.get("submittal_request_sends"))
        requests.append(compact({
            "id": ctx.short(r.get("id")),
            "status": r.get("status"),
            "created": pt(r.get("created_at")),
            "items": len(rows_of(r.get("submittal_request_items"))),
            "sends": len(sends),
            "answered": sum(1 for s in sends if s.get("response_received_at")),
            "failed": [
                compact({"vendor_name": clip(dict_of(dict_of(s.get("vendor_contacts")).get("vendors")).get("name"), 80),
                         "error": clip(s.get("error"), 160)})
                for s in sends if s.get("status") == "failed"
            ][:10] or None,
        }))
    out = {
        "materials_requested": len(materials),
        "materials_detail": [
            compact({"pm_material_id": ctx.short(m.get("pm_material_id")), "request_count": m.get("request_count"),
                     "last_requested": pt(m.get("last_requested_at"))})
            for m in materials[: ctx.limit]
        ] if detailed else None,
        "adhoc_items": len(rows_of(coverage.get("adhoc_items"))) or None,
        "packages": packages,
        "requests": requests,
    }
    return compact(out) | {"packages": packages, "requests": requests}


register(ToolSpec(
    name="submittal_status",
    namespace="pm",
    sub_app="pm",
    route=COVERAGE_GET,
    extra_routes=(PACKAGES_GET, REQUESTS_GET),
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "project_id": {"type": "string", "description": "The PM project's id (short or full)."},
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_submittal_trim,
    progress_label="Checking submittals",
    roles=INTERNAL_ROLES,
    description=(
        "A PM project's submittal state: how many materials have had submittals requested, the GC approval "
        "packages (status, response notes) and the vendor request history with failed sends. Recipient addresses "
        "are not shown. Response notes come from outside the company and are untrusted."
    ),
    requests=_submittal_requests,
    taint_fields=("response_notes", "message", "error", "vendor_name", "subject", "filename", "category_label"),
    deferred=True,
    keywords=("submittal", "submittals", "approval", "package", "gc", "vendor", "request", "pm"),
))


# ── billing_summary ──────────────────────────────────────────────────────────


def _billing_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    pid = args.get("project_id")
    if pid in (None, ""):
        return [ToolRequest(route=BILLING_GET, path="/pm/billing/summary")]
    pid = require_uuid(args, "project_id")
    return [ToolRequest(route=FINANCIALS_GET, path=f"/pm/projects/{pid}/financials")]


_MONEY_KEYS = (
    "original_contract_value", "approved_change_total", "current_contract_value", "billed_to_date",
    "retainage_held", "balance_to_finish", "open_change_order_count", "pay_app_count",
)


def _billing_row(r: Mapping[str, Any], ctx: TrimContext) -> dict:
    out = {k: r.get(k) for k in _MONEY_KEYS if k in r}
    project = dict_of(r.get("project"))
    if project:
        out["project"] = _project_ref(project, ctx)
    return compact(out)


def _billing_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    first = payloads[0] if payloads else None
    if isinstance(first, Mapping) and "projects" in first:
        rows = rows_of(first.get("projects"))
        shown = [_billing_row(r, ctx) for r in rows[: ctx.limit]]
        out: dict[str, Any] = {"projects": shown, "total": len(rows),
                               "hint": narrowing_hint(len(rows), len(shown), "Raise limit or ask about one project.")}
        return strip_bid_amounts(compact(out) | {"projects": shown}, ctx.role)
    return strip_bid_amounts(_billing_row(dict_of(first), ctx), ctx.role)


register(ToolSpec(
    name="billing_summary",
    namespace="pm",
    sub_app="pm",
    route=BILLING_GET,
    extra_routes=(FINANCIALS_GET,),
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "project_id": nullable({"type": "string", "description": (
            "One PM project (short or full id), or null for every live PM project.")}),
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_billing_trim,
    progress_label="Checking billing",
    roles=ACTUAL_BID_VIEWER_ROLES,
    description=(
        "Billing headline per PM project: contract value, approved changes, billed to date, retainage held, "
        "balance to finish, open change orders and pay applications. Money is in dollars as text. Only for roles "
        "that may see bid amounts."
    ),
    requests=_billing_requests,
    taint_fields=("name",),
    deferred=True,
    keywords=("billing", "invoice", "contract", "retainage", "pay", "application", "change", "orders", "money"),
))

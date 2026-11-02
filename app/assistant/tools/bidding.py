"""The bidding namespace (docs/ASSISTANT.md, Skills and tools): tools tagged
sub_app bidding because their routes mount under the Bidding flag.

  whats_due   GET /projects/due-checks (today's bids with the three readiness
              checks of the 6:03 digest) plus GET /projects/today (overdue
              carry-over and bids sent today)
  get_labor   GET /projects/{id}/labor
"""

from __future__ import annotations

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
    CONCISE_TEXT,
    clip,
    compact,
    dict_of,
    narrowing_hint,
    pt,
    require_uuid,
    rows_of,
    text_cap,
)
from app.core.roles import INTERNAL_ROLES

SCOPES = ("today", "overdue_and_today")


# ── whats_due ────────────────────────────────────────────────────────────────


def _due_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    return [
        ToolRequest(route=("GET", "/projects/due-checks"), path="/projects/due-checks"),
        ToolRequest(route=("GET", "/projects/today"), path="/projects/today", optional=True),
    ]


def _checks_text(checks: Mapping[str, Any]) -> list[str]:
    missing = []
    if not checks.get("has_quotes"):
        missing.append("vendor quotes")
    if not checks.get("has_labor"):
        missing.append("labor")
    if not checks.get("has_markups"):
        missing.append("markups")
    return missing


def _due_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    checks_rows = rows_of(payloads[0] if payloads else None)
    today_rows = rows_of(payloads[1] if len(payloads) > 1 else None)
    scope = str(args.get("scope") or "today")
    if scope not in SCOPES:
        scope = "today"
    detailed = ctx.response_format == "detailed"

    due_today: list[dict] = []
    seen: set[str] = set()
    for row in checks_rows:
        project = dict_of(row.get("project"))
        checks = dict_of(row.get("checks"))
        pid = str(project.get("id") or "")
        seen.add(pid)
        due_today.append(compact({
            "id": ctx.short(pid),
            "number": project.get("number"),
            "name": clip(project.get("name"), 120),
            "due": pt(project.get("internal_bid_at")),
            "stage": project.get("current_stage"),
            "severity": row.get("severity"),
            "missing": _checks_text(checks) or None,
            "ready": not _checks_text(checks) or None,
            "checks": checks if detailed else None,
        }))
    out: dict[str, Any] = {
        "scope": scope,
        "due_today": due_today[: ctx.limit],
        "due_today_hint": narrowing_hint(len(due_today), min(len(due_today), ctx.limit),
                                         "Raise limit or ask about one project."),
    }
    if scope == "overdue_and_today":
        others = []
        for p in today_rows:
            pid = str(p.get("id") or "")
            if pid in seen:
                continue
            others.append(compact({
                "id": ctx.short(pid),
                "number": p.get("number"),
                "name": clip(p.get("name"), 120),
                "due": pt(p.get("internal_bid_at")),
                "stage": p.get("current_stage"),
                "sent_today": True if p.get("sent_today") else None,
                "overdue": None if p.get("sent_today") else True,
            }))
        out["overdue_or_sent_today"] = others[: ctx.limit]
        out["overdue_hint"] = narrowing_hint(len(others), min(len(others), ctx.limit),
                                             "Raise limit to see more.")
    result = compact(out)
    # An empty list is the answer ("nothing is due today"), not noise to
    # compact away: a bare {"scope": "today"} reads to the model as a failed
    # read ("the tool only returned the scope"), as the 2026-10-22 browser
    # pass showed. The hints stay compacted.
    result["due_today"] = out["due_today"]
    if scope == "overdue_and_today":
        result["overdue_or_sent_today"] = out["overdue_or_sent_today"]
    return result


register(ToolSpec(
    name="whats_due",
    namespace="bidding",
    sub_app="bidding",
    route=("GET", "/projects/due-checks"),
    extra_routes=(("GET", "/projects/today"),),
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "scope": nullable({"type": "string", "enum": list(SCOPES),
                           "description": "today (default) or overdue_and_today."}),
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_due_trim,
    progress_label="Checking what is due",
    roles=INTERNAL_ROLES,
    description=(
        "Bids due today (internal due date, Pacific) with readiness: vendor quotes, labor and "
        "markups entered. scope overdue_and_today adds overdue bids and bids sent today. "
        "Cite project numbers."
    ),
    requests=_due_requests,
    taint_fields=("name",),
))


# ── get_labor ────────────────────────────────────────────────────────────────


def _labor_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    pid = require_uuid(args, "project_id")
    return [ToolRequest(route=("GET", "/projects/{project_id}/labor"), path=f"/projects/{pid}/labor")]


def _labor_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    row = payloads[0] if payloads else None
    if not isinstance(row, dict):
        return {"entered": False, "hint": "No labor numbers entered for this project yet."}
    detailed = ctx.response_format == "detailed"
    breakdown = row.get("labor_breakdown") if isinstance(row.get("labor_breakdown"), list) else []
    # Detailed lists every line, not `limit` of them: set_labor_numbers
    # replaces the whole breakdown, so the model must see every line it
    # has to resend (capped by the executor's output size, which marks a
    # cut result truncated).
    return compact({
        "entered": row.get("labor_amount") is not None,
        "labor_amount": row.get("labor_amount"),
        "verified": bool(row.get("verified")),
        "updated_at": pt(row.get("updated_at")),
        "labor_notes": clip(row.get("labor_notes"), text_cap(ctx.response_format)),
        "breakdown_lines": len(breakdown) or None,
        "breakdown": [
            compact({
                k: (clip(v, CONCISE_TEXT) if k in ("label", "name") else v)
                for k, v in b.items() if k in ("label", "name", "hours", "amount", "rate")
            })
            for b in breakdown if isinstance(b, dict)
        ] if detailed else None,
    })


register(ToolSpec(
    name="get_labor",
    namespace="bidding",
    sub_app="bidding",
    route=("GET", "/projects/{project_id}/labor"),
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "project_id": {"type": "string", "description": "The project's id (short or full)."},
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_labor_trim,
    progress_label="Reading labor numbers",
    roles=INTERNAL_ROLES,
    description="The labor numbers entered for one project: amount, verified flag, notes (detailed adds the breakdown).",
    requests=_labor_requests,
    taint_fields=("labor_notes", "label", "name"),
))

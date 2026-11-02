"""The core namespace: spine tools every internal role gets whatever
sub-apps are on (docs/ASSISTANT.md, Skills and tools, MVP tools).

  app_search           GET /projects/search (projects, GCs, vendors by name)
  get_project_context  GET /projects/{id} plus stage-events and the last
                       five non-estimator notes (both optional: they live
                       under bidding-only routers and 404 when Bidding is off)
  list_teammates       GET /users/teammates
  list_gc_contacts     GET /gc-contacts?gc_id= (a GC's contacts, phase 1: the
                       ids add_project_gc and create_project_draft take)
  load_skill           GET /assistant/skills/{name} (the SKILL.md body)
  find_tools           GET /assistant/tools/find (phase 4, 3.16: names the
                       deferred tools that fit a request, so their
                       definitions reach the model on the next step)

Every trimmer returns names plus short ids (TrimContext.short), honours
response_format and limit, and names the human-authored fields in
taint_fields so the executor can taint the turn.
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
    common_args,
    nullable,
    register,
    schema,
)
from app.assistant.tools._common import (
    ToolArgumentError,
    clip,
    compact,
    dict_of,
    is_uuid,
    narrowing_hint,
    pt,
    require_uuid,
    rows_of,
    text_cap,
)
from app.core.roles import INTERNAL_ROLES

SEARCH_KINDS = ("all", "projects", "gcs", "vendors")
MAX_STAGE_EVENTS = 5
MAX_NOTES = 5
SKILL_NAME_MAX = 60


# ── app_search ───────────────────────────────────────────────────────────────


def _search_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    q = str(args.get("q") or "").strip()
    if len(q) < 1:
        raise ToolArgumentError("q must be at least 1 character.")
    _, limit = common_args(args)
    return [ToolRequest(
        route=("GET", "/projects/search"),
        path="/projects/search",
        params={"q": q[:120], "limit": limit, "offset": 0},
    )]


def _search_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    body = dict_of(payloads[0] if payloads else None)
    kind = str(args.get("kind") or "all")
    if kind not in SEARCH_KINDS:
        kind = "all"
    out: dict[str, Any] = {"q": body.get("q")}
    if kind in ("all", "projects"):
        projects = rows_of(body.get("projects"))
        out["projects"] = [
            compact({
                "id": ctx.short(p.get("id")),
                "number": p.get("number"),
                "name": clip(p.get("name"), 120),
                "stage": p.get("current_stage"),
                "status": p.get("status") if ctx.response_format == "detailed" else None,
                "internal_due": pt(p.get("internal_bid_at")),
                "abandoned": bool(p.get("abandoned_at")) or None,
            })
            for p in projects[: ctx.limit]
        ]
        out["projects_hint"] = narrowing_hint(
            int(body.get("projects_total") or len(projects)), len(out["projects"]),
            "Use a more specific q, or the project number.",
        )
    if kind in ("all", "gcs"):
        out["gcs"] = [
            compact({"id": ctx.short(g.get("id")), "name": clip(g.get("name"), 120)})
            for g in rows_of(body.get("gcs"))[: ctx.limit]
        ]
    if kind in ("all", "vendors"):
        out["vendors"] = [
            compact({"id": ctx.short(v.get("id")), "name": clip(v.get("name"), 120)})
            for v in rows_of(body.get("vendors"))[: ctx.limit]
        ]
    return compact(out)


register(ToolSpec(
    name="app_search",
    namespace="core",
    sub_app=None,
    route=("GET", "/projects/search"),
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "q": {"type": "string", "description": "Name or project number fragment, 1 to 120 characters."},
        "kind": nullable({"type": "string", "enum": list(SEARCH_KINDS),
                          "description": "What to search; null means all."}),
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_search_trim,
    progress_label="Searching projects, GCs and vendors",
    roles=INTERNAL_ROLES,
    description=(
        "Find projects (by name or number), general contractors and vendors by name. "
        "Returns names with short ids; pass a project's id to get_project_context or get_labor."
    ),
    requests=_search_requests,
    taint_fields=("name",),
))


# ── get_project_context ──────────────────────────────────────────────────────


def _context_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    pid = require_uuid(args, "project_id")
    return [
        ToolRequest(route=("GET", "/projects/{project_id}"), path=f"/projects/{pid}"),
        ToolRequest(route=("GET", "/projects/{project_id}/stage-events"),
                    path=f"/projects/{pid}/stage-events", optional=True),
        ToolRequest(route=("GET", "/projects/{project_id}/notes"),
                    path=f"/projects/{pid}/notes", optional=True),
    ]


def _category_summary(state: Any) -> dict | None:
    if not isinstance(state, dict):
        return None
    out = {}
    for cat, vals in state.items():
        if isinstance(vals, dict):
            head = vals.get("current_task") or vals.get("head") or vals.get("task")
            done = vals.get("complete") if "complete" in vals else vals.get("completed")
            out[cat] = compact({"task": head, "complete": done})
    return out or None


def _context_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    project = dict_of(payloads[0] if payloads else None)
    events = rows_of(payloads[1] if len(payloads) > 1 else None)
    notes = rows_of(payloads[2] if len(payloads) > 2 else None)
    cap = text_cap(ctx.response_format)
    detailed = ctx.response_format == "detailed"
    out: dict[str, Any] = compact({
        "id": ctx.short(project.get("id")),
        "number": project.get("number"),
        "name": clip(project.get("name"), 160),
        "stage": project.get("current_stage"),
        "status": project.get("status"),
        "owner_role": project.get("current_owner_role"),
        "internal_due": pt(project.get("internal_bid_at")),
        "actual_due": pt(project.get("actual_bid_at")),
        "due_from_estimator": pt(project.get("due_from_estimator_at")),
        "due_from_vendors": pt(project.get("due_from_vendors_at")),
        "job_walk": pt(project.get("job_walk_at")),
        "address": clip(project.get("address"), 160),
        "abandoned_at": pt(project.get("abandoned_at")),
        "failed_to_bid_reason": clip(project.get("failed_to_bid_reason"), cap),
        "reverify_return_stage": project.get("reverify_return_stage"),
        "notes": clip(project.get("notes"), cap),
        "bid_notes": clip(project.get("bid_notes"), cap),
        "project_information": clip(project.get("project_information"), cap) if detailed else None,
        "trade_instructions": clip(project.get("trade_instructions"), cap) if detailed else None,
        "labor_time": project.get("labor_time"),
        "wage_type": project.get("wage_type"),
        "categories": _category_summary(project.get("category_state")),
    })
    recent = events[-MAX_STAGE_EVENTS:]
    out["stage_events"] = [
        compact({"to": e.get("to_stage"), "from": e.get("from_stage") if detailed else None,
                 "at": pt(e.get("entered_at"))})
        for e in recent
    ]
    if len(events) > len(recent):
        out["stage_events_hint"] = f"last {len(recent)} of {len(events)} stage events"
    kept = [
        n for n in notes
        if str((dict_of(n.get("author")).get("role") or "")) != "estimator"
    ][-MAX_NOTES:]
    out["recent_notes"] = [
        compact({
            "at": pt(n.get("created_at")),
            "author": clip(dict_of(n.get("author")).get("full_name"), 60),
            "body": clip(n.get("body"), cap),
        })
        for n in kept
    ]
    if any(str(dict_of(n.get("author")).get("role") or "") == "estimator" for n in notes):
        out["estimator_notes_hint"] = (
            "Estimator-authored notes exist and are not shown here (external, untrusted)."
        )
    return compact(out)


register(ToolSpec(
    name="get_project_context",
    namespace="core",
    sub_app=None,
    route=("GET", "/projects/{project_id}"),
    extra_routes=(("GET", "/projects/{project_id}/stage-events"),
                  ("GET", "/projects/{project_id}/notes")),
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "project_id": {"type": "string", "description": "The project's id (short or full) from app_search or whats_due."},
        "response_format": RESPONSE_FORMAT_PROP,
    }),
    response_trimmer=_context_trim,
    progress_label="Reading the project",
    roles=INTERNAL_ROLES,
    description=(
        "One project's status: number, name, stage, due dates, the latest stage changes and the "
        "last few internal notes. About 400 tokens. Cite the project number in the answer."
    ),
    requests=_context_requests,
    taint_fields=("name", "notes", "bid_notes", "project_information", "trade_instructions",
                  "body", "address", "failed_to_bid_reason"),
))


# ── list_teammates ───────────────────────────────────────────────────────────


def _teammates_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    return [ToolRequest(route=("GET", "/users/teammates"), path="/users/teammates")]


def _teammates_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    rows = rows_of(payloads[0] if payloads else None)
    detailed = ctx.response_format == "detailed"
    items = [
        compact({
            "id": ctx.short(r.get("id")),
            "name": clip(r.get("full_name"), 80),
            "role": r.get("role"),
            "email": r.get("email") if detailed else None,
        })
        for r in rows[: ctx.limit]
    ]
    return compact({
        "teammates": items,
        "hint": narrowing_hint(len(rows), len(items), "Raise limit to see more."),
    })


register(ToolSpec(
    name="list_teammates",
    namespace="core",
    sub_app=None,
    route=("GET", "/users/teammates"),
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({"response_format": RESPONSE_FORMAT_PROP, "limit": LIMIT_PROP}),
    response_trimmer=_teammates_trim,
    progress_label="Listing teammates",
    roles=INTERNAL_ROLES,
    description="Active internal teammates with their roles (detailed adds email).",
    requests=_teammates_requests,
))


# ── list_gc_contacts ─────────────────────────────────────────────────────────


def _gc_contacts_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    gc_id = args.get("gc_id")
    if not is_uuid(gc_id):
        raise ToolArgumentError("gc_id must be a GC id from app_search with kind gcs (the short id or the full id).")
    return [ToolRequest(route=("GET", "/gc-contacts"), path="/gc-contacts", params={"gc_id": str(gc_id)})]


def _gc_contacts_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    rows = rows_of(payloads[0] if payloads else None)
    items = [
        compact({"id": ctx.short(r.get("id")), "name": clip(r.get("name"), 120), "email": r.get("email")})
        for r in rows[: ctx.limit]
    ]
    result = compact({"hint": narrowing_hint(len(rows), len(items), "Raise limit to see more.")})
    # An empty list is the answer ("this GC has no contacts").
    result["contacts"] = items
    return result


register(ToolSpec(
    name="list_gc_contacts",
    namespace="core",
    sub_app=None,
    route=("GET", "/gc-contacts"),
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "gc_id": {"type": "string", "description": "The GC's id (short or full) from app_search with kind gcs."},
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_gc_contacts_trim,
    progress_label="Listing GC contacts",
    roles=INTERNAL_ROLES,
    description="The contacts of one general contractor (name and email) with short ids for add_project_gc.",
    requests=_gc_contacts_requests,
    taint_fields=("name",),
))


# ── load_skill ───────────────────────────────────────────────────────────────


def _skill_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    name = str(args.get("name") or "").strip().lower()
    if not name or len(name) > SKILL_NAME_MAX or not all(c.isalnum() or c == "-" for c in name):
        raise ToolArgumentError("name must be a skill name from the system prompt (letters, digits and hyphens).")
    return [ToolRequest(route=("GET", "/assistant/skills/{name}"), path=f"/assistant/skills/{name}")]


def _skill_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    body = dict_of(payloads[0] if payloads else None)
    return compact({
        "name": body.get("name"),
        "description": body.get("description"),
        "body": body.get("body"),
    })


register(ToolSpec(
    name="load_skill",
    namespace="core",
    sub_app=None,
    route=("GET", "/assistant/skills/{name}"),
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({"name": {"type": "string", "description": "The skill's name as listed in the system prompt."}}),
    response_trimmer=_skill_trim,
    progress_label="Loading a skill",
    roles=INTERNAL_ROLES,
    description="Load the full instructions of one skill listed in the system prompt.",
    requests=_skill_requests,
))


# ── find_tools (phase 4, docs/ASSISTANT_PHASE4_PLAN.md 3.16) ─────────────────

# The route side's literal (app/routers/assistant.py FIND_TOOLS_ROUTE); a
# test asserts they are equal.
FIND_GET = ("GET", "/assistant/tools/find")
FIND_QUERY_MAX = 200
FIND_LIMIT_DEFAULT = 6
FIND_LIMIT_MAX = 10
FIND_CONCISE_TEXT = 160
FIND_DETAILED_TEXT = 600


def _find_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    query = " ".join(str(args.get("query") or "").split())
    if not query:
        raise ToolArgumentError("query must say, in a few words, what the tool should do.")
    raw = args.get("limit")
    try:
        limit = int(raw) if raw is not None else FIND_LIMIT_DEFAULT
    except (TypeError, ValueError):
        limit = FIND_LIMIT_DEFAULT
    limit = max(1, min(FIND_LIMIT_MAX, limit))
    return [ToolRequest(route=FIND_GET, path=FIND_GET[1], params={"query": query[:FIND_QUERY_MAX], "limit": limit})]


def _find_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    """The matching deferred tools, best first, with their full names (the
    loop surfaces exactly the names listed here, so they are never
    shortened). Descriptions are fixed server text."""
    body = dict_of(payloads[0] if payloads else None)
    cap = FIND_DETAILED_TEXT if ctx.response_format == "detailed" else FIND_CONCISE_TEXT
    tools = []
    for entry in rows_of(body.get("tools")):
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        tools.append(compact({"name": name.strip(), "description": clip(entry.get("description"), cap)}))
    result = compact({"hint": clip(body.get("hint"), 300)})
    # An empty list is the answer ("nothing matches"), not noise to compact away.
    result["tools"] = tools
    return result


register(ToolSpec(
    name="find_tools",
    namespace="core",
    sub_app=None,
    route=FIND_GET,
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "query": {"type": "string", "description": (
            "What you need to do, in a few words (for example: drawings sheet notes, spreadsheet of bid "
            "invitations, who opened the files, open RFIs).")},
        "limit": nullable({"type": "integer", "description": (
            f"Most tools to return, 1 to {FIND_LIMIT_MAX}; null means {FIND_LIMIT_DEFAULT}.")}),
        "response_format": RESPONSE_FORMAT_PROP,
    }),
    response_trimmer=_find_trim,
    progress_label="Looking for a tool",
    roles=INTERNAL_ROLES,
    description=(
        "Find the extra tools that fit a request: plans and drawings, reports and spreadsheets, evidence (who "
        "did what and when), PM and RFIs, certified payroll, moving a lane. Call it before answering about "
        "those; the tools it names can be called on your next step. Not needed for the tools you already have."
    ),
    requests=_find_requests,
))

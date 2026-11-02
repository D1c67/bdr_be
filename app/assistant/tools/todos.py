"""The to-do tools (docs/ASSISTANT_PHASE1_PLAN.md 3.7): the caller's own
list, through the To-Dos routes.

  list_todos     GET /todos (read)
  todo_create    POST /todos (confirm; not revertible, DELETE is never called)
  todo_complete  PATCH /todos/{id} is_done true (confirm; revert todo_reopen)
  todo_reopen    hidden: PATCH /todos/{id} is_done false, the revert only

The confirm tools are write_internal: called inside a turn they become an
approval card, and the write runs only in the approval execute job.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.assistant.registry import (
    EFFECT_READ,
    EFFECT_WRITE_INTERNAL,
    LIMIT_PROP,
    RESPONSE_FORMAT_PROP,
    Proposal,
    ProposalRefused,
    ToolRequest,
    ToolSpec,
    TrimContext,
    WriteSpec,
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
    norm_value,
    parse_date,
    pt,
    rows_of,
    text_cap,
)
from app.assistant.tools.writes import project_ref
from app.core.roles import INTERNAL_ROLES, WRITER_ROLES

TODOS_GET = ("GET", "/todos")
TODOS_POST = ("POST", "/todos")
TODO_PATCH = ("PATCH", "/todos/{todo_id}")
PROJECT_GET = ("GET", "/projects/{project_id}")

# app/routers/todos.py TODO_MAX_CHARS.
TITLE_MAX = 500

TODO_TARGET_KEYS = ("id", "is_done", "completed_at", "title", "due_date")

WARN_COMPLETION_LOST = {
    "code": "completion_time_lost",
    "text": "The original completion time cannot be restored.",
}


def _no_trim(payloads: list[Any], args: Mapping[str, Any], ctx: Any) -> Any:
    return payloads


def _todo_row(field: str, label: str, before: Any, after: Any) -> dict:
    def show(value: Any) -> str:
        if value is None or value == "":
            return "Empty"
        if isinstance(value, bool):
            return "Yes" if value else "No"
        return str(value)

    return {"field": field, "label": label, "before": show(before), "after": show(after),
            "changed": norm_value(field, before) != norm_value(field, after)}


# ── list_todos ───────────────────────────────────────────────────────────────


def _list_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    # Newest first, so a user with more than the route's 500-row window of
    # (mostly done) to-dos still sees the new ones.
    return [ToolRequest(route=TODOS_GET, path="/todos", params={"newest_first": "true"})]


def _list_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    # The route answers newest first; show oldest first again.
    rows = list(reversed(rows_of(payloads[0] if payloads else None)))
    # Open first, then done; each keeps the created order.
    ordered = [r for r in rows if not r.get("is_done")] + [r for r in rows if r.get("is_done")]
    detailed = ctx.response_format == "detailed"
    items = [
        compact({
            "id": ctx.short(r.get("id")),
            "title": clip(r.get("title"), text_cap(ctx.response_format)),
            "due_date": r.get("due_date"),
            "is_done": bool(r.get("is_done")),
            "project_id": ctx.short(r.get("project_id")) if r.get("project_id") else None,
            "source": r.get("source"),
            "completed_at": pt(r.get("completed_at")) if detailed else None,
        })
        for r in ordered[: ctx.limit]
    ]
    result = compact({
        "open": sum(1 for r in rows if not r.get("is_done")),
        "hint": narrowing_hint(len(ordered), len(items), "Raise limit to see more."),
    })
    # An empty list is the answer ("no to-dos"), not noise to compact away.
    result["todos"] = items
    return result


register(ToolSpec(
    name="list_todos",
    namespace="core",
    sub_app=None,
    route=TODOS_GET,
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({"response_format": RESPONSE_FORMAT_PROP, "limit": LIMIT_PROP}),
    response_trimmer=_list_trim,
    progress_label="Reading your to-dos",
    roles=INTERNAL_ROLES,
    description="Your own to-dos, open ones first, with short ids to pass to todo_complete.",
    requests=_list_requests,
    taint_fields=("title",),
))


# ── todo_create ──────────────────────────────────────────────────────────────


def _create_project_id(args: Mapping[str, Any]) -> str | None:
    value = args.get("project_id")
    if value is None:
        return None
    if not is_uuid(value):
        raise ToolArgumentError("project_id must be a project id from app_search (the short id or the full id).")
    return str(value)


def _create_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    """Only the linked project, so the card can show its number."""
    pid = _create_project_id(args)
    return [ToolRequest(route=PROJECT_GET, path=f"/projects/{pid}")] if pid else []


def _create_snapshot(args: Mapping[str, Any], reads: list[Any]) -> tuple[dict, dict]:
    return {}, {}


def _create_build(args: Mapping[str, Any], reads: list[Any], ctx: Any) -> Proposal:
    title = args.get("title")
    if title is None:
        stripped = {str(s.get("field")) for s in (getattr(ctx, "stripped", None) or ()) if isinstance(s, Mapping)}
        if "title" in stripped:
            raise ProposalRefused("The to-do title came from untrusted text. Ask the user to type it.")
        raise ToolArgumentError("title is required.")
    if not isinstance(title, str) or not title.strip():
        raise ToolArgumentError("title cannot be blank.")
    title = title.strip()
    if len(title) > TITLE_MAX:
        raise ToolArgumentError(f"title may be at most {TITLE_MAX} characters.")
    due = parse_date(args["due_date"], "due_date") if args.get("due_date") is not None else None
    pid = _create_project_id(args)
    project = dict_of(reads[0] if reads else None) if pid else {}
    preview = {
        "v": 1,
        "title": "Add a to-do",
        "tool_label": "To-do",
        "project": project_ref(project) if pid else None,
        "rows": [
            _todo_row("title", "Title", None, title),
            _todo_row("due_date", "Due date", None, due),
            _todo_row("project", "Project", None, project.get("number") if pid else None),
        ],
        "items": [],
        "warnings": [],
        "similar": [],
    }
    request = ToolRequest(route=TODOS_POST, path="/todos",
                          json={"title": title, "due_date": due, "project_id": pid})
    return Proposal(request=request, project_id=pid, target={}, gate={}, preview=preview,
                    summary=f"New to-do: {title}.")


def _create_created(args: Mapping[str, Any], payload: Any) -> dict | None:
    row = dict_of(payload)
    if not row.get("id"):
        return None
    return {"kind": "todo", "id": str(row["id"]), "label": row.get("title"), "href": "/todos"}


register(ToolSpec(
    name="todo_create",
    namespace="core",
    sub_app=None,
    route=TODOS_POST,
    extra_routes=(PROJECT_GET,),
    effect=EFFECT_WRITE_INTERNAL,
    confirm=True,
    schema=schema({
        "title": {"type": "string", "description": f"The to-do in the user's own words, 1 to {TITLE_MAX} characters."},
        "due_date": nullable({"type": "string", "description": "The due date as YYYY-MM-DD; null for none."}),
        "project_id": nullable({"type": "string", "description": "The project it is about (short or full id); null for none."}),
    }),
    response_trimmer=_no_trim,
    progress_label="Preparing a to-do",
    roles=WRITER_ROLES,
    description=(
        "Propose a to-do on the user's own list. Creates an approval card; nothing is added until the user "
        "approves it."
    ),
    requests=_create_requests,
    write=WriteSpec(
        preview_requests=_create_requests,
        build=_create_build,
        snapshot=_create_snapshot,
        bound_text_fields=("title",),
        creates="todo",
        created_ref=_create_created,
        result_summary=lambda payload: {"id": dict_of(payload).get("id"), "title": dict_of(payload).get("title")},
        writes_tables=("todos",),
    ),
))


# ── todo_complete and todo_reopen ────────────────────────────────────────────


def _todo_id(args: Mapping[str, Any]) -> str:
    value = args.get("todo_id")
    if not is_uuid(value):
        raise ToolArgumentError("todo_id must be a to-do id from list_todos (the short id or the full id).")
    return str(value)


def _patch_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    # The one to-do by id, whatever its age (the route's list is capped).
    return [ToolRequest(route=TODOS_GET, path="/todos", params={"todo_id": _todo_id(args)})]


def _find(args: Mapping[str, Any], reads: list[Any]) -> dict | None:
    tid = _todo_id(args)
    return next((r for r in rows_of(reads[0] if reads else None) if str(r.get("id")) == tid), None)


def todo_target(row: Mapping[str, Any] | None, todo_id: str) -> dict:
    if row is None:
        return {"id": todo_id, "missing": True}
    return {
        "id": str(row.get("id")),
        "is_done": bool(row.get("is_done")),
        "completed_at": norm_value("completed_at", row.get("completed_at")),
        "title": row.get("title"),
        "due_date": norm_value("due_date", row.get("due_date")),
    }


def _patch_snapshot(args: Mapping[str, Any], reads: list[Any]) -> tuple[dict, dict]:
    return todo_target(_find(args, reads), _todo_id(args)), {}


def _patch_build(args: Mapping[str, Any], reads: list[Any], *, done: bool) -> Proposal:
    tid = _todo_id(args)
    row = _find(args, reads)
    if row is None:
        raise ProposalRefused("Unknown to-do; use list_todos to find it.")
    if bool(row.get("is_done")) == done:
        raise ProposalRefused("That to-do is already done." if done else "That to-do is not done.")
    title = str(row.get("title") or "")
    preview = {
        "v": 1,
        "title": "Complete a to-do" if done else "Reopen a to-do",
        "tool_label": "To-do",
        "project": None,
        "rows": [
            _todo_row("title", "Title", title, title),
            _todo_row("is_done", "Done", bool(row.get("is_done")), done),
        ],
        "items": [],
        "warnings": [] if done else [dict(WARN_COMPLETION_LOST)],
        "similar": [],
    }
    project_id = str(row["project_id"]) if row.get("project_id") else None
    request = ToolRequest(route=TODO_PATCH, path=f"/todos/{tid}", json={"is_done": done})
    return Proposal(request=request, project_id=project_id, target=todo_target(row, tid), gate={},
                    preview=preview, summary=f"{'Complete' if done else 'Reopen'} the to-do: {title}.")


def _todo_result(payload: Any) -> dict:
    row = dict_of(payload)
    return {"id": row.get("id"), "is_done": bool(row.get("is_done"))}


TODO_ID_SCHEMA = schema({
    "todo_id": {"type": "string", "description": "The to-do's id (short or full) from list_todos."},
})

register(ToolSpec(
    name="todo_complete",
    namespace="core",
    sub_app=None,
    route=TODO_PATCH,
    extra_routes=(TODOS_GET,),
    effect=EFFECT_WRITE_INTERNAL,
    confirm=True,
    schema=TODO_ID_SCHEMA,
    response_trimmer=_no_trim,
    progress_label="Preparing a to-do completion",
    roles=WRITER_ROLES,
    description=(
        "Propose marking one of the user's own to-dos done. Creates an approval card; nothing changes until "
        "the user approves it."
    ),
    requests=_patch_requests,
    write=WriteSpec(
        preview_requests=_patch_requests,
        build=lambda args, reads, ctx: _patch_build(args, reads, done=True),
        snapshot=_patch_snapshot,
        revert_tool="todo_reopen",
        revert_args=lambda orig, before, after: {"todo_id": str(orig.get("todo_id"))},
        result_summary=_todo_result,
        writes_tables=("todos",),
    ),
))

register(ToolSpec(
    name="todo_reopen",
    namespace="core",
    sub_app=None,
    route=TODO_PATCH,
    extra_routes=(TODOS_GET,),
    effect=EFFECT_WRITE_INTERNAL,
    confirm=True,
    schema=TODO_ID_SCHEMA,
    response_trimmer=_no_trim,
    progress_label="Preparing a to-do reopen",
    roles=WRITER_ROLES,
    description="Reopen a to-do an earlier assistant change completed (revert only, never offered).",
    requests=_patch_requests,
    offered=False,
    write=WriteSpec(
        preview_requests=_patch_requests,
        build=lambda args, reads, ctx: _patch_build(args, reads, done=False),
        snapshot=_patch_snapshot,
        result_summary=_todo_result,
        writes_tables=("todos",),
    ),
))

"""The memory tools (docs/ASSISTANT_PHASE3_PLAN.md 3.8): the user's own
notes, through the assistant's tool routes.

  memory_list    GET /assistant/tools/memory (read; active and pending notes)
  memory_search  GET /assistant/tools/memory/search (phase 4, 3.15; read; the
                 user's notes ranked against a query)
  memory_save    POST /assistant/tools/memory/save (housekeeping)
  memory_update  POST /assistant/tools/memory/{id}/revise (housekeeping; a
                 proposed change the user approves)
  memory_forget  POST /assistant/tools/memory/{id}/forget (housekeeping;
                 archives, never a permanent delete)

Provenance is the route's: a note is active only when the user typed it in
the current message of an untainted turn, else it waits for the user's
Save. The model only passes kind, title, body and project.

Note text never reaches the ledger: `redact_args` stores lengths and
sha256 hashes only, and the loop stores the same form in the transcript.
`precheck` runs the secret filter first, so a password, an ID number, a
bank or a pay detail is refused before any ledger row or request (the
route runs the same filter again). A row of untrusted origin is listed
under untrusted_title and untrusted_body, the taint fields, so listing
ordinary notes does not taint the conversation.

This module never imports app.routers.*: the route literals are its own,
and tests/test_assistant_memory_tools.py asserts they equal the routes'.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from app.assistant.memory import KINDS, SENSITIVE_MESSAGE, sensitive_kind
from app.assistant.registry import (
    EFFECT_HOUSEKEEPING,
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
    rows_of,
)
from app.core.roles import INTERNAL_ROLES

# The route side's literals (app/routers/assistant_memory.py); a test
# asserts they are equal.
LIST_GET = ("GET", "/assistant/tools/memory")
SAVE_POST = ("POST", "/assistant/tools/memory/save")
SEARCH_GET = ("GET", "/assistant/tools/memory/search")
REVISE_POST = ("POST", "/assistant/tools/memory/{memory_id}/revise")
FORGET_POST = ("POST", "/assistant/tools/memory/{memory_id}/forget")

LIST_STATUSES = ("active", "pending_review")
# memory_search (3.15): the route's own bounds (assistant_memory.SEARCH_LIMIT_MAX
# and its status words); a test asserts they are equal.
SEARCH_STATUSES = ("active", "pending_review", "both")
SEARCH_QUERY_MIN, SEARCH_QUERY_MAX = 2, 200
SEARCH_LIMIT_DEFAULT, SEARCH_LIMIT_MAX = 10, 25
# The route cuts bodies to 300 characters too (assistant_memory.TOOL_BODY_MAX).
BODY_SHOWN = 300
SENSITIVE_KIND = "memory_sensitive"
REVISION_TEXT = "Waiting for the user to approve the change."

# Per-turn ceilings (3.7.1): save and update 3 calls, 1 while the turn is
# tainted; forget 5 (its route refuses a tainted turn anyway).
WRITE_CALLS = 3
WRITE_TAINTED_CALLS = 1
FORGET_CALLS = 5


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ledger_form(args: Mapping[str, Any]) -> dict:
    """The arguments as every ledger row and the stored transcript keep
    them (3.7.2): the kind, the ids, and the title and body as lengths and
    hashes only."""
    title = str(args.get("title") or "")
    body = str(args.get("body") or "")
    return {
        "kind": args.get("kind") if args.get("kind") in KINDS else None,
        "project_id": str(args["project_id"]) if is_uuid(args.get("project_id")) else None,
        "memory_id": str(args["memory_id"]) if is_uuid(args.get("memory_id")) else None,
        "title_len": len(title),
        "title_sha256": _sha(title),
        "body_len": len(body),
        "body_sha256": _sha(body),
    }


def refuse_secrets(args: Mapping[str, Any]) -> tuple[str, str] | None:
    """The precheck: the regex filter on the title and body. The text is
    never echoed; only the fixed sentence goes back to the model."""
    for key in ("title", "body"):
        value = args.get(key)
        if isinstance(value, str) and sensitive_kind(value):
            return SENSITIVE_KIND, SENSITIVE_MESSAGE
    return None


def _memory_id(args: Mapping[str, Any]) -> str:
    value = args.get("memory_id")
    if not is_uuid(value):
        raise ToolArgumentError("memory_id must be a note id from memory_list (the short id or the full id).")
    return str(value)


def _optional_project(args: Mapping[str, Any]) -> str | None:
    value = args.get("project_id")
    if value is None:
        return None
    if not is_uuid(value):
        raise ToolArgumentError("project_id must be a project id from app_search (the short id or the full id).")
    return str(value)


# ── memory_list ──────────────────────────────────────────────────────────────


def _list_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    _, limit = common_args(args)
    params: dict[str, Any] = {"limit": limit}
    if args.get("status") in LIST_STATUSES:
        params["status"] = args["status"]
    if args.get("kind") in KINDS:
        params["kind"] = args["kind"]
    return [ToolRequest(route=LIST_GET, path=LIST_GET[1], params=params)]


def _list_item(row: Mapping[str, Any], ctx: TrimContext) -> dict:
    """One note. A row of untrusted origin keeps its text only under the
    untrusted_* keys, which taint the turn when they are non-empty."""
    tainted = bool(row.get("origin_tainted"))
    title = clip(row.get("title"), BODY_SHOWN)
    body = clip(row.get("body"), BODY_SHOWN)
    return compact({
        "id": ctx.short(row.get("id")),
        "kind": row.get("kind"),
        "status": row.get("status"),
        "pinned": True if row.get("pinned") else None,
        "project_id": ctx.short(row.get("project_id")) if row.get("project_id") else None,
        "title": None if tainted else title,
        "body": None if tainted else body,
        "untrusted_title": title if tainted else None,
        "untrusted_body": body if tainted else None,
    })


def _list_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    rows = rows_of(dict_of(payloads[0] if payloads else None).get("items"))
    shown = rows[: ctx.limit]
    result = compact({
        "pending_review": sum(1 for r in rows if r.get("status") == "pending_review"),
    })
    # An empty list is the answer ("no notes"), not noise to compact away.
    result["notes"] = [_list_item(r, ctx) for r in shown]
    return result


register(ToolSpec(
    name="memory_list",
    namespace="memory",
    sub_app=None,
    route=LIST_GET,
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "status": nullable({"type": "string", "enum": list(LIST_STATUSES),
                            "description": "active or pending_review (waiting for the user's Save); null for both."}),
        "kind": nullable({"type": "string", "enum": list(KINDS), "description": "One kind of note; null for all."}),
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_list_trim,
    progress_label="Reading your notes",
    roles=INTERNAL_ROLES,
    description=(
        "The notes the user keeps for you (active and waiting for the user's Save), with short ids to pass to "
        "memory_update and memory_forget."
    ),
    requests=_list_requests,
    taint_fields=("untrusted_title", "untrusted_body"),
))


# ── memory_search (phase 4, 3.15) ────────────────────────────────────────────


def search_ledger_form(args: Mapping[str, Any]) -> dict:
    """The arguments as every ledger row and the stored transcript keep them
    (3.15): the query can carry what the user typed, so it is kept as a
    length and a hash only."""
    query = str(args.get("q") or "")
    raw = args.get("limit")
    try:
        limit = max(1, min(SEARCH_LIMIT_MAX, int(raw))) if raw is not None else SEARCH_LIMIT_DEFAULT
    except (TypeError, ValueError):
        limit = SEARCH_LIMIT_DEFAULT
    return {
        "q_len": len(query),
        "q_sha256": _sha(query),
        "kind": args.get("kind") if args.get("kind") in KINDS else None,
        "status": args.get("status") if args.get("status") in SEARCH_STATUSES else None,
        "limit": limit,
    }


def _search_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    query = " ".join(str(args.get("q") or "").split())
    if not SEARCH_QUERY_MIN <= len(query) <= SEARCH_QUERY_MAX:
        raise ToolArgumentError(
            f"q must be {SEARCH_QUERY_MIN} to {SEARCH_QUERY_MAX} characters: a few words to look for in the notes."
        )
    params: dict[str, Any] = {"q": query, "limit": search_ledger_form(args)["limit"]}
    if args.get("kind") in KINDS:
        params["kind"] = args["kind"]
    if args.get("status") in SEARCH_STATUSES:
        params["status"] = args["status"]
    return [ToolRequest(route=SEARCH_GET, path=SEARCH_GET[1], params=params)]


def _search_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    """The ranked notes. Like memory_list, a note of untrusted origin keeps
    its text under untrusted_title and untrusted_body only. No [Mn] tags
    (owner question 21): a searched note is not cited as used memory."""
    rows = rows_of(dict_of(payloads[0] if payloads else None).get("items"))
    shown = rows[: ctx.limit]
    notes = []
    for row in shown:
        item = _list_item(row, ctx)
        rank = row.get("rank")
        if isinstance(rank, (int, float)) and not isinstance(rank, bool):
            item["rank"] = round(float(rank), 3)
        notes.append(item)
    result = compact({"hint": narrowing_hint(len(rows), len(shown), "Raise limit or narrow q.")})
    # An empty list is the answer ("no matching notes"), not noise to compact away.
    result["notes"] = notes
    return result


register(ToolSpec(
    name="memory_search",
    namespace="memory",
    sub_app=None,
    route=SEARCH_GET,
    effect=EFFECT_READ,
    confirm=False,
    schema=schema({
        "q": {"type": "string", "description": "What to look for in the user's notes, 2 to 200 characters."},
        "kind": nullable({"type": "string", "enum": list(KINDS), "description": "One kind of note; null for all."}),
        "status": nullable({"type": "string", "enum": list(SEARCH_STATUSES),
                            "description": "active, pending_review (waiting for the user's Save) or both; null means both."}),
        "response_format": RESPONSE_FORMAT_PROP,
        "limit": LIMIT_PROP,
    }),
    response_trimmer=_search_trim,
    progress_label="Searching your notes",
    roles=INTERNAL_ROLES,
    description=(
        "Search the notes the user keeps for you by words, best match first (active notes and notes waiting for "
        "the user's Save, never archived ones). Use it when the user asks what you remember about something; "
        "memory_list shows them all."
    ),
    requests=_search_requests,
    redact_args=search_ledger_form,
    taint_fields=("untrusted_title", "untrusted_body"),
))


# ── memory_save ──────────────────────────────────────────────────────────────


def _save_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    kind = args.get("kind")
    if kind not in KINDS:
        raise ToolArgumentError(f"kind must be one of {', '.join(KINDS)}.")
    title = args.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ToolArgumentError("title cannot be blank.")
    body = args.get("body")
    return [ToolRequest(route=SAVE_POST, path=SAVE_POST[1], json={
        "kind": kind, "title": title, "body": body if isinstance(body, str) else "",
        "project_id": _optional_project(args),
    })]


def _save_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    """The saved note's id, status and the route's fixed message. Similar
    notes are named by id only: their titles may be of untrusted origin."""
    row = dict_of(payloads[0] if payloads else None)
    similar = [compact({"id": ctx.short(s.get("id")), "similarity": s.get("similarity")})
               for s in rows_of(row.get("similar"))]
    return compact({
        "memory_id": ctx.short(row.get("memory_id")),
        "status": row.get("status"),
        "source": row.get("source"),
        "similar": similar,
        "show_note": row.get("show_note") is True,
        "message": row.get("message"),
    })


register(ToolSpec(
    name="memory_save",
    namespace="memory",
    sub_app=None,
    route=SAVE_POST,
    effect=EFFECT_HOUSEKEEPING,
    confirm=False,
    schema=schema({
        "kind": {"type": "string", "enum": list(KINDS), "description": (
            "preference (how the user likes things), about_me, instruction (how to work for the user), "
            "project_note (about one project; give project_id) or reference.")},
        "title": {"type": "string", "description": "A short title, at most 200 characters, in the user's own words."},
        "body": nullable({"type": "string", "description": "The detail, at most 4000 characters; null for none."}),
        "project_id": nullable({"type": "string", "description": (
            "project_note only: the project (short or full id); else null.")}),
    }),
    response_trimmer=_save_trim,
    progress_label="Saving a note",
    roles=INTERNAL_ROLES,
    description=(
        "Save a note the user asked you to remember. A note in the user's own words from this message is saved at "
        "once; any other waits for the user to click Save under your answer. Never save what BDR already stores "
        "(projects, contacts, dates) or a password, ID number, bank or pay detail."
    ),
    requests=_save_requests,
    redact_args=ledger_form,
    precheck=refuse_secrets,
    max_proposals_per_turn=WRITE_CALLS,
    max_tainted_calls_per_turn=WRITE_TAINTED_CALLS,
))


# ── memory_update ────────────────────────────────────────────────────────────


def _revise_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    mid = _memory_id(args)
    title, body = args.get("title"), args.get("body")
    if title is None and body is None:
        raise ToolArgumentError("Give the note a new title or body.")
    return [ToolRequest(route=REVISE_POST, path=f"/assistant/tools/memory/{mid}/revise",
                        json={"title": title, "body": body})]


def _revise_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    row = dict_of(payloads[0] if payloads else None)
    return compact({
        "memory_id": ctx.short(row.get("memory_id")),
        "revision_id": ctx.short(row.get("revision_id")) if row.get("revision_id") else None,
        "status": row.get("status"),
        "show_note": row.get("show_note") is True,
        "message": REVISION_TEXT if row.get("status") == "revision_pending" else None,
    })


register(ToolSpec(
    name="memory_update",
    namespace="memory",
    sub_app=None,
    route=REVISE_POST,
    effect=EFFECT_HOUSEKEEPING,
    confirm=False,
    schema=schema({
        "memory_id": {"type": "string", "description": "The note's id (short or full) from memory_list."},
        "title": nullable({"type": "string", "description": "The new title; null keeps it."}),
        "body": nullable({"type": "string", "description": "The new body; null keeps it."}),
    }),
    response_trimmer=_revise_trim,
    progress_label="Proposing a change to a note",
    roles=INTERNAL_ROLES,
    description=(
        "Propose a change to one of the user's notes. The note keeps its text until the user approves the "
        "change; a note still waiting for the user's Save is revised in place. Pinned notes are the user's alone."
    ),
    requests=_revise_requests,
    redact_args=ledger_form,
    precheck=refuse_secrets,
    max_proposals_per_turn=WRITE_CALLS,
    max_tainted_calls_per_turn=WRITE_TAINTED_CALLS,
))


# ── memory_forget ────────────────────────────────────────────────────────────


def _forget_requests(args: Mapping[str, Any]) -> list[ToolRequest]:
    mid = _memory_id(args)
    return [ToolRequest(route=FORGET_POST, path=f"/assistant/tools/memory/{mid}/forget")]


def _forget_trim(payloads: list[Any], args: Mapping[str, Any], ctx: TrimContext) -> Any:
    row = dict_of(payloads[0] if payloads else None)
    return compact({"memory_id": ctx.short(row.get("memory_id")), "status": row.get("status")})


register(ToolSpec(
    name="memory_forget",
    namespace="memory",
    sub_app=None,
    route=FORGET_POST,
    effect=EFFECT_HOUSEKEEPING,
    confirm=False,
    schema=schema({
        "memory_id": {"type": "string", "description": "The note's id (short or full) from memory_list."},
    }),
    response_trimmer=_forget_trim,
    progress_label="Archiving a note",
    roles=INTERNAL_ROLES,
    description=(
        "Forget one of the user's notes: it is archived (the user can restore it in the Memory tab). Pinned "
        "notes are the user's alone. Refused in a conversation that read outside content."
    ),
    requests=_forget_requests,
    redact_args=ledger_form,
    max_proposals_per_turn=FORGET_CALLS,
))

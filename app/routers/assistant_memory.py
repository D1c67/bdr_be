"""Assistant memory: the tool routes and the Memory tab
(docs/ASSISTANT_PHASE3_PLAN.md 3.8 and 5.3).

Who may call what:

  assistant  the tool routes under /assistant/tools/memory. The reads list
             active and pending notes; save, revise and forget are
             housekeeping steps that trust only the turn context the begun
             ledger row gives (routers.assistant._trusted_turn, 3.7.3):
             provenance is derived from the user's own words in the
             current message, never from model arguments, and a tainted
             turn's note waits for the user's Save with origin_tainted;
  human      every Memory tab route (list, summary, one note, create, edit,
             pin, unpin, archive, restore, approve, reject, the revision
             decisions and the permanent delete): each calls
             _refuse_assistant_principal and the firewall deny-lists it.

Every write that carries text runs the secret filter (memory.sensitive_kind)
first; a hit answers 422 memory_sensitive with `detail.kind` and the text is
never echoed or logged. Every change goes through one RPC call
(repo.memory_apply: the op and its event in one transaction), whose
refusals map to the codes of 5.3. Approve and reject need the updated_at
the person saw, so a Save on text a later turn revised answers 409
memory_stale with the current row.

Declaration order matters: GET /assistant/memories/summary sits above GET
/assistant/memories/{memory_id} (the first full match wins, for Starlette
and for firewall.resolve_path_format).

Plain def handlers: the Supabase SDK is synchronous. Every assistant_* row
goes through app/assistant/repo.py.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.assistant import memory, repo
from app.assistant.schemas import (
    MemoryCreateIn,
    MemoryDecisionIn,
    MemoryForgetToolOut,
    MemoryListOut,
    MemoryOut,
    MemoryPatchIn,
    MemoryProjectOut,
    MemoryReviseIn,
    MemoryReviseToolOut,
    MemoryRevisionOut,
    MemorySaveIn,
    MemorySearchItemOut,
    MemorySearchOut,
    MemorySaveToolOut,
    MemorySimilarOut,
    MemorySummaryOut,
    MemoryToolItemOut,
    MemoryToolListOut,
)
from app.assistant.tools._common import is_uuid
from app.core import supabase_client
from app.core.deps import CurrentUser, require_internal
from app.routers.assistant import (
    PROJECT_NOT_FOUND,
    _before_cursor,
    _detail_error,
    _project_refs,
    _refuse_assistant_principal,
    _require_assistant,
    _require_assistant_principal,
    _trusted_turn,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assistant", tags=["assistant"], dependencies=[Depends(require_internal)])

# Route literals the phase 3 tool specs repeat (app/assistant/tools/memory.py,
# pinned by a test).
TOOL_LIST_ROUTE = "/assistant/tools/memory"
TOOL_SAVE_ROUTE = "/assistant/tools/memory/save"
TOOL_REVISE_ROUTE = "/assistant/tools/memory/{memory_id}/revise"
TOOL_FORGET_ROUTE = "/assistant/tools/memory/{memory_id}/forget"
# Phase 4 (docs/ASSISTANT_PHASE4_PLAN.md 3.15): the memory_search tool's read.
TOOL_SEARCH_ROUTE = "/assistant/tools/memory/search"
SEARCH_LIMIT_MAX = 25
SEARCH_STATUSES = {
    "active": ("active",),
    "pending_review": ("pending_review",),
    "both": ("active", "pending_review"),
}

# Error codes (5.3 and 5.4).
MEMORY_NOT_FOUND = "memory_not_found"
MEMORY_SENSITIVE = "memory_sensitive"
BAD_MEMORY = "bad_memory"
MAILBOX_REVIEW = "mailbox_review"
INCOGNITO = "incognito"
TAINTED_TURN = "tainted_turn"
UNTRUSTED_ORIGIN = "untrusted_origin"

SAVED_TEXT = "Saved."
WAITING_TEXT = "Waiting for the user to click Save."
TOOL_BODY_MAX = 300
TOOL_LIST_MAX = 100

_REFUSALS: dict[str, tuple[int, str]] = {
    "memory_not_found": (status.HTTP_404_NOT_FOUND, "That note was not found."),
    "revision_not_found": (status.HTTP_404_NOT_FOUND, "That proposed change to the note was not found."),
    "memory_stale": (status.HTTP_409_CONFLICT, "The note changed since it was shown. Look at it again first."),
    "memory_wrong_status": (status.HTTP_409_CONFLICT, "The note is not in a state that allows this."),
    "memory_archived": (status.HTTP_409_CONFLICT, "The note is archived. Restore it first."),
    "memory_pinned_user_only": (status.HTTP_409_CONFLICT,
                                "A pinned note is changed only by the user, in the Memory tab."),
    "memory_pin_user_only": (status.HTTP_409_CONFLICT, "Only the user pins a note."),
    "memory_cap_active": (status.HTTP_409_CONFLICT, "There are already 300 active notes. Archive some first."),
    "memory_cap_pinned": (status.HTTP_409_CONFLICT, "At most 40 notes can be pinned."),
    "memory_cap_pinned_chars": (status.HTTP_409_CONFLICT, "Pinned notes are limited to 6000 characters in all."),
    "memory_cap_pending": (status.HTTP_409_CONFLICT,
                           "50 notes are already waiting for the user's Save. Review them in the Memory tab first."),
    "legal_hold": (status.HTTP_409_CONFLICT,
                   "Notes cannot be deleted while your records are on legal hold. Archive it instead."),
}
_TURN_REFUSALS = {
    MAILBOX_REVIEW: "Notes are not read or saved while reviewing a mailbox.",
    INCOGNITO: "This message is Incognito, so nothing is saved.",
    TAINTED_TURN: "This conversation read outside content, so notes are archived from the Memory tab.",
}


def _error(status_code: int, code: str, message: str, **detail: Any) -> HTTPException:
    return _detail_error(status_code, code, message, **detail)


def _memory_id(value: str) -> str:
    value = (value or "").strip()
    if not is_uuid(value):
        raise _error(status.HTTP_404_NOT_FOUND, MEMORY_NOT_FOUND, _REFUSALS[MEMORY_NOT_FOUND][1])
    return value


# ── Shapes ───────────────────────────────────────────────────────────────────


def _revision_out(rev: dict | None) -> MemoryRevisionOut | None:
    if not rev or not rev.get("id"):
        return None
    return MemoryRevisionOut(id=str(rev["id"]), title=str(rev.get("title") or ""), body=str(rev.get("body") or ""),
                             origin_tainted=bool(rev.get("origin_tainted")), created_at=str(rev.get("created_at") or ""))


def _memory_out(row: dict, *, revision: dict | None = None, project: dict | None = None,
                warning: str | None = None) -> MemoryOut:
    """A note as the Memory tab shows it (never the idempotency key)."""
    pid = row.get("project_id")
    return MemoryOut(
        id=str(row["id"]),
        kind=str(row.get("kind") or ""),
        title=str(row.get("title") or ""),
        body=str(row.get("body") or ""),
        project=MemoryProjectOut(**(project or {"id": str(pid)})) if pid else None,
        status=str(row.get("status") or ""),
        source=str(row.get("source") or ""),
        pinned=bool(row.get("pinned")),
        origin_tainted=bool(row.get("origin_tainted")),
        last_used_at=row.get("last_used_at"),
        use_count=int(row.get("use_count") or 0),
        archived_at=row.get("archived_at"),
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
        pending_revision=_revision_out(revision),
        warning=warning,
    )


def _outs(user: CurrentUser, rows: list[dict]) -> list[MemoryOut]:
    """MemoryOut for each row: the pending revisions and the projects in one
    read each."""
    if not rows:
        return []
    ids = [str(r["id"]) for r in rows]
    revisions = repo.pending_revisions(user.id, ids)
    projects = _project_refs(user, [r.get("project_id") for r in rows])
    return [_memory_out(r, revision=revisions.get(str(r["id"])), project=projects.get(str(r.get("project_id") or "")))
            for r in rows]


def _out(user: CurrentUser, row: dict, warning: str | None = None) -> MemoryOut:
    out = _outs(user, [row])[0]
    return out.model_copy(update={"warning": warning}) if warning else out


def _refused(user: CurrentUser, exc: repo.MemoryRefused) -> HTTPException:
    """repo.MemoryRefused as the 5.3 error; memory_stale carries the
    current row (the card or the tab re-renders it before a Save)."""
    status_code, message = _REFUSALS.get(exc.code, (status.HTTP_409_CONFLICT, "The note could not be changed."))
    detail: dict[str, Any] = {}
    if exc.code == "memory_stale" and exc.memory and exc.memory.get("id"):
        detail["memory"] = _out(user, exc.memory).model_dump()
    return _error(status_code, exc.code, message, **detail)


def _load(user: CurrentUser, memory_id: str) -> dict:
    row = repo.get_memory(user.id, _memory_id(memory_id))
    if row is None:
        raise _error(status.HTTP_404_NOT_FOUND, MEMORY_NOT_FOUND, _REFUSALS[MEMORY_NOT_FOUND][1])
    return row


# ── Field checks ─────────────────────────────────────────────────────────────


def _clean(kind: Any, title: Any, body: Any, project_id: Any) -> dict:
    try:
        return memory.clean_fields(kind, title, body, project_id)
    except memory.BadMemory as exc:
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_MEMORY, str(exc)) from None


def _refuse_secrets(*texts: Any) -> None:
    """The regex filter on every text a write carries. The text is never
    echoed or logged; only its category is answered."""
    for text in texts:
        kind = memory.sensitive_kind(str(text or ""))
        if kind:
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, MEMORY_SENSITIVE, memory.SENSITIVE_MESSAGE, kind=kind)


def _require_project(project_id: str | None) -> None:
    if not project_id:
        return
    rows = (supabase_client.get_supabase().table("projects").select("id")
            .eq("id", project_id).limit(1).execute()).data or []
    if not rows:
        raise _error(status.HTTP_404_NOT_FOUND, PROJECT_NOT_FOUND, "Project not found.")


def _apply(user: CurrentUser, op: str, **kw: Any) -> dict:
    try:
        return repo.memory_apply(user.id, op, **kw)
    except repo.MemoryRefused as exc:
        raise _refused(user, exc) from None


def _turn_refusal(code: str) -> HTTPException:
    return _error(status.HTTP_409_CONFLICT, code, _TURN_REFUSALS[code])


def _memory_turn(request: Request, user: CurrentUser, tool_name: str) -> dict:
    """The trusted turn of a memory write: never in Mailbox review mode and
    never in an Incognito turn (the toolsets drop these tools there too)."""
    turn = _trusted_turn(request, user, tool_name)
    _require_assistant(user)
    if turn.get("mode") == "mailbox_review":
        raise _turn_refusal(MAILBOX_REVIEW)
    if turn.get("incognito"):
        raise _turn_refusal(INCOGNITO)
    return turn


# ── Tool routes (the assistant's own) ───────────────────────────────────────


@router.get(TOOL_LIST_ROUTE.removeprefix("/assistant"), response_model=MemoryToolListOut)
def tool_list_memories(
    status_filter: Literal["active", "pending_review"] | None = Query(default=None, alias="status"),
    kind: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=50, ge=1, le=TOOL_LIST_MAX),
    user: CurrentUser = Depends(require_internal),
) -> MemoryToolListOut:
    """memory_list: active and pending notes (never archived), bodies cut to
    300 characters. The trimmer moves an origin_tainted row's text into
    untrusted fields."""
    _require_assistant_principal(user)
    _require_assistant(user)
    if kind is not None and kind not in memory.KINDS:
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_MEMORY, f"kind must be one of {', '.join(memory.KINDS)}.")
    rows = repo.list_memories(user.id, status=status_filter or repo.MEMORY_LIVE_STATUSES, kind=kind, limit=limit)
    return MemoryToolListOut(items=[
        MemoryToolItemOut(
            id=str(r["id"]), kind=str(r.get("kind") or ""), title=str(r.get("title") or ""),
            body=str(r.get("body") or "")[:TOOL_BODY_MAX], status=str(r.get("status") or ""),
            pinned=bool(r.get("pinned")), project_id=r.get("project_id"),
            origin_tainted=bool(r.get("origin_tainted")),
        )
        for r in rows
    ])


@router.get(TOOL_SEARCH_ROUTE.removeprefix("/assistant"), response_model=MemorySearchOut)
def tool_search_memories(
    q: str | None = Query(default=None, max_length=200),
    kind: str | None = Query(default=None, max_length=40),
    status_filter: Literal["active", "pending_review", "both"] = Query(default="both", alias="status"),
    limit: int = Query(default=10, ge=1, le=SEARCH_LIMIT_MAX),
    user: CurrentUser = Depends(require_internal),
) -> MemorySearchOut:
    """memory_search (3.15): the person's own active and pending notes
    ranked against `q` (never archived, never another person's), bodies cut
    to 300 characters, best first. The assistant's own route, like
    memory_list: a human gets the bare 404. The trimmer moves an
    origin_tainted row's text into untrusted fields; the query is never
    logged here."""
    _require_assistant_principal(user)
    _require_assistant(user)
    if kind is not None and kind not in memory.KINDS:
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_MEMORY, f"kind must be one of {', '.join(memory.KINDS)}.")
    # Checked here, after the principal gate, so a human always gets the 404.
    text = (q or "").strip()
    if len(text) < 2:
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_MEMORY, "Search with at least 2 characters.")
    rows = repo.search_memories(user.id, text, SEARCH_STATUSES[status_filter], kind, limit)
    return MemorySearchOut(items=[
        MemorySearchItemOut(
            id=str(r["id"]), kind=str(r.get("kind") or ""), title=str(r.get("title") or ""),
            body=str(r.get("body") or "")[:TOOL_BODY_MAX], status=str(r.get("status") or ""),
            pinned=bool(r.get("pinned")), project_id=r.get("project_id"),
            origin_tainted=bool(r.get("origin_tainted")),
            rank=float(r["rank"]) if isinstance(r.get("rank"), (int, float)) else None,
        )
        for r in rows[:limit]
        if r.get("id") and str(r.get("user_id") or user.id) == str(user.id)
    ])


@router.post(TOOL_SAVE_ROUTE.removeprefix("/assistant"), response_model=MemorySaveToolOut)
def tool_save_memory(body: MemorySaveIn, request: Request,
                     user: CurrentUser = Depends(require_internal)) -> MemorySaveToolOut:
    """memory_save (3.8.2): {kind, title, body, project_id} only. The note is
    active only when the user typed it in the current message of an
    untainted turn; otherwise it waits for the user's Save (and a tainted
    turn marks it origin_tainted). Idempotent by the step key."""
    turn = _memory_turn(request, user, "memory_save")
    fields = _clean(body.kind, body.title, body.body, body.project_id)
    _require_project(fields["project_id"])
    _refuse_secrets(fields["title"], fields["body"])
    status_, source, origin_tainted = memory.derive_provenance(
        fields["title"], fields["body"], str(turn.get("user_text") or ""),
        tainted=bool(turn.get("tainted")), mailbox_tainted=bool(turn.get("mailbox_tainted")),
    )
    try:
        similar = repo.memory_similar(user.id, fields["title"])
    except Exception:  # noqa: BLE001 - the near-duplicate hint is best effort
        logger.exception("assistant: similar notes could not be read for %s", user.id)
        similar = []
    res = _apply(user, "create", actor="assistant", fields={
        **fields, "status": status_, "source": source, "origin_tainted": origin_tainted,
    }, conversation_id=turn.get("conversation_id"), message_id=turn.get("message_id"),
        idempotency_key=turn.get("step_key"))
    row = res.get("memory") or {}
    memory_id = str(row.get("id") or "")
    return MemorySaveToolOut(
        memory_id=memory_id,
        status=str(row.get("status") or status_),
        source=str(row.get("source") or source),
        origin_tainted=bool(row.get("origin_tainted")),
        similar=[MemorySimilarOut(id=str(s["id"]), title=str(s.get("title") or ""),
                                  similarity=float(s.get("similarity") or 0))
                 for s in similar if s.get("id") and str(s["id"]) != memory_id],
        show_note=True,
        message=SAVED_TEXT if row.get("status") == "active" else WAITING_TEXT,
    )


@router.post(TOOL_REVISE_ROUTE.removeprefix("/assistant"), response_model=MemoryReviseToolOut)
def tool_revise_memory(memory_id: str, body: MemoryReviseIn, request: Request,
                       user: CurrentUser = Depends(require_internal)) -> MemoryReviseToolOut:
    """memory_update (3.8.3). A pending note is revised in place and stays
    pending; an active note gets a proposed revision the user approves (the
    live text stays until then; a newer proposal supersedes an older one).
    A pinned note is the user's alone."""
    turn = _memory_turn(request, user, "memory_update")
    row = _load(user, memory_id)
    if row.get("status") == "archived":
        raise _refused(user, repo.MemoryRefused("memory_archived"))
    if row.get("pinned"):
        raise _refused(user, repo.MemoryRefused("memory_pinned_user_only"))
    if body.title is None and body.body is None:
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_MEMORY, "Give the note a new title or body.")
    fields = _clean(row.get("kind"), body.title if body.title is not None else row.get("title"),
                    body.body if body.body is not None else row.get("body"), row.get("project_id"))
    _refuse_secrets(fields["title"], fields["body"])
    tainted = bool(turn.get("tainted") or turn.get("mailbox_tainted"))
    op = "revise_pending" if row.get("status") == "pending_review" else "propose_revision"
    res = _apply(user, op, actor="assistant", memory_id=str(row["id"]),
                 expected_updated_at=body.expected_updated_at,
                 fields={"title": fields["title"], "body": fields["body"], "origin_tainted": tainted},
                 conversation_id=turn.get("conversation_id"), message_id=turn.get("message_id"),
                 idempotency_key=turn.get("step_key"))
    revision = res.get("revision") or {}
    return MemoryReviseToolOut(
        memory_id=str(row["id"]),
        revision_id=str(revision["id"]) if revision.get("id") else None,
        status="pending_review" if op == "revise_pending" else "revision_pending",
        show_note=True,
    )


@router.post(TOOL_FORGET_ROUTE.removeprefix("/assistant"), response_model=MemoryForgetToolOut)
def tool_forget_memory(memory_id: str, request: Request,
                       user: CurrentUser = Depends(require_internal)) -> MemoryForgetToolOut:
    """memory_forget (3.8.4): archives (soft), never a permanent delete. A
    pinned note is the user's alone, and a tainted turn is refused: a
    planted "forget everything" must not change the user's notes."""
    turn = _memory_turn(request, user, "memory_forget")
    if turn.get("tainted") or turn.get("mailbox_tainted"):
        raise _turn_refusal(TAINTED_TURN)
    row = _load(user, memory_id)
    res = _apply(user, "forget", actor="assistant", memory_id=str(row["id"]),
                 conversation_id=turn.get("conversation_id"), message_id=turn.get("message_id"),
                 idempotency_key=turn.get("step_key"))
    return MemoryForgetToolOut(memory_id=str(row["id"]), status=str((res.get("memory") or {}).get("status") or "archived"))


# ── The Memory tab (human only) ──────────────────────────────────────────────

# One page of the Memory tab (the Active cap, so Active always fits in one).
MEMORY_PAGE_MAX = 300


@router.get("/memories", response_model=MemoryListOut)
def list_memories(
    status_filter: Literal["active", "pending_review", "archived"] | None = Query(default=None, alias="status"),
    kind: str | None = Query(default=None, max_length=40),
    before: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=MEMORY_PAGE_MAX, ge=1, le=MEMORY_PAGE_MAX),
    user: CurrentUser = Depends(require_internal),
) -> MemoryListOut:
    """The caller's notes, most recently changed first, each with its
    pending revision and project. A full page carries `next_before` (its
    last updated_at): Archived has no cap, so it can pass one page."""
    _refuse_assistant_principal(user)
    _require_assistant(user)
    if kind is not None and kind not in memory.KINDS:
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_MEMORY, f"kind must be one of {', '.join(memory.KINDS)}.")
    cursor = _before_cursor(before)
    rows = repo.list_memories(user.id, status=status_filter, kind=kind, limit=limit, before=cursor)
    next_before = str(rows[-1].get("updated_at")) if rows and len(rows) >= limit else None
    return MemoryListOut(items=_outs(user, rows), next_before=next_before)


# Declared above /memories/{memory_id}: "summary" is never read as an id.
@router.get("/memories/summary", response_model=MemorySummaryOut)
def memory_summary(user: CurrentUser = Depends(require_internal)) -> MemorySummaryOut:
    _refuse_assistant_principal(user)
    _require_assistant(user)
    return MemorySummaryOut(**repo.memory_summary(user.id))


@router.get("/memories/{memory_id}", response_model=MemoryOut)
def read_memory(memory_id: str, user: CurrentUser = Depends(require_internal)) -> MemoryOut:
    _refuse_assistant_principal(user)
    _require_assistant(user)
    return _out(user, _load(user, memory_id))


@router.post("/memories", response_model=MemoryOut, status_code=status.HTTP_201_CREATED)
def create_memory(body: MemoryCreateIn, user: CurrentUser = Depends(require_internal)) -> MemoryOut:
    """A note written by hand in the Memory tab: active, source
    user_settings."""
    _refuse_assistant_principal(user)
    _require_assistant(user)
    fields = _clean(body.kind, body.title, body.body, body.project_id)
    _require_project(fields["project_id"])
    _refuse_secrets(fields["title"], fields["body"])
    res = _apply(user, "create", actor="user", fields={**fields, "status": "active", "source": "user_settings"})
    return _out(user, res["memory"])


@router.patch("/memories/{memory_id}", response_model=MemoryOut)
def edit_memory(memory_id: str, body: MemoryPatchIn, user: CurrentUser = Depends(require_internal)) -> MemoryOut:
    """Op edit, fenced on the updated_at the form rendered (409
    memory_stale with the current row). Only the fields sent change."""
    _refuse_assistant_principal(user)
    _require_assistant(user)
    row = _load(user, memory_id)
    sent = body.model_fields_set - {"expected_updated_at"}
    merged = {
        "kind": body.kind if "kind" in sent and body.kind is not None else row.get("kind"),
        "title": body.title if "title" in sent and body.title is not None else row.get("title"),
        "body": body.body if "body" in sent and body.body is not None else row.get("body"),
        "project_id": body.project_id if "project_id" in sent else row.get("project_id"),
    }
    fields = _clean(merged["kind"], merged["title"], merged["body"], merged["project_id"])
    if "project_id" in sent and fields["project_id"] != row.get("project_id"):
        _require_project(fields["project_id"])
    _refuse_secrets(fields["title"], fields["body"])
    changed = {k: fields[k] for k in ("kind", "title", "body", "project_id") if k in sent}
    res = _apply(user, "edit", actor="user", memory_id=str(row["id"]),
                 expected_updated_at=body.expected_updated_at, fields=changed)
    return _out(user, res["memory"])


def _simple(user: CurrentUser, memory_id: str, op: str) -> MemoryOut:
    row = _load(user, memory_id)
    res = _apply(user, op, actor="user", memory_id=str(row["id"]))
    saved = res.get("memory") or row
    warning = UNTRUSTED_ORIGIN if op == "pin" and saved.get("origin_tainted") else None
    return _out(user, saved, warning)


@router.post("/memories/{memory_id}/pin", response_model=MemoryOut)
def pin_memory(memory_id: str, user: CurrentUser = Depends(require_internal)) -> MemoryOut:
    """The user's click may pin a note of untrusted origin; the answer then
    warns, and the note still loads as data, never as an instruction."""
    _refuse_assistant_principal(user)
    _require_assistant(user)
    return _simple(user, memory_id, "pin")


@router.post("/memories/{memory_id}/unpin", response_model=MemoryOut)
def unpin_memory(memory_id: str, user: CurrentUser = Depends(require_internal)) -> MemoryOut:
    _refuse_assistant_principal(user)
    _require_assistant(user)
    return _simple(user, memory_id, "unpin")


@router.post("/memories/{memory_id}/archive", response_model=MemoryOut)
def archive_memory(memory_id: str, user: CurrentUser = Depends(require_internal)) -> MemoryOut:
    _refuse_assistant_principal(user)
    _require_assistant(user)
    return _simple(user, memory_id, "archive")


@router.post("/memories/{memory_id}/restore", response_model=MemoryOut)
def restore_memory(memory_id: str, user: CurrentUser = Depends(require_internal)) -> MemoryOut:
    _refuse_assistant_principal(user)
    _require_assistant(user)
    return _simple(user, memory_id, "restore")


@router.post("/memories/{memory_id}/approve", response_model=MemoryOut)
def approve_memory(
    memory_id: str,
    body: MemoryDecisionIn,
    via: Literal["card"] | None = Query(default=None),
    user: CurrentUser = Depends(require_internal),
) -> MemoryOut:
    """Save a pending note (active, user_confirmed); `?via=card` is the
    inline card's Save (op confirm). Fenced on the updated_at the person
    saw."""
    _refuse_assistant_principal(user)
    _require_assistant(user)
    row = _load(user, memory_id)
    res = _apply(user, "confirm" if via == "card" else "approve", actor="user", memory_id=str(row["id"]),
                 expected_updated_at=body.expected_updated_at)
    return _out(user, res["memory"])


@router.post("/memories/{memory_id}/reject", response_model=MemoryOut)
def reject_memory(memory_id: str, body: MemoryDecisionIn, user: CurrentUser = Depends(require_internal)) -> MemoryOut:
    _refuse_assistant_principal(user)
    _require_assistant(user)
    row = _load(user, memory_id)
    res = _apply(user, "reject", actor="user", memory_id=str(row["id"]), expected_updated_at=body.expected_updated_at)
    return _out(user, res["memory"])


def _revision(user: CurrentUser, memory_id: str, revision_id: str, op: str) -> MemoryOut:
    row = _load(user, memory_id)
    rid = (revision_id or "").strip()
    if not is_uuid(rid):
        raise _refused(user, repo.MemoryRefused("revision_not_found"))
    res = _apply(user, op, actor="user", memory_id=str(row["id"]), fields={"revision_id": rid})
    return _out(user, res.get("memory") or row)


@router.post("/memories/{memory_id}/revisions/{revision_id}/approve", response_model=MemoryOut)
def approve_memory_revision(memory_id: str, revision_id: str,
                            user: CurrentUser = Depends(require_internal)) -> MemoryOut:
    """Take a proposed change: fenced on the note's updated_at when it was
    proposed (409 memory_stale when the note moved since)."""
    _refuse_assistant_principal(user)
    _require_assistant(user)
    return _revision(user, memory_id, revision_id, "approve_revision")


@router.post("/memories/{memory_id}/revisions/{revision_id}/reject", response_model=MemoryOut)
def reject_memory_revision(memory_id: str, revision_id: str,
                           user: CurrentUser = Depends(require_internal)) -> MemoryOut:
    _refuse_assistant_principal(user)
    _require_assistant(user)
    return _revision(user, memory_id, revision_id, "reject_revision")


@router.delete("/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_memory(memory_id: str, user: CurrentUser = Depends(require_internal)) -> Response:
    """Permanent delete (3.8.7): the row, its events and revisions, the id
    in used_memory_ids and the memory tool calls, outputs and results that
    carried it. 409 legal_hold while the person is on legal hold (the tab
    offers Archive). The tombstone is an audit row with no text."""
    _refuse_assistant_principal(user)
    _require_assistant(user)
    row = _load(user, memory_id)
    _apply(user, "delete", actor="user", memory_id=str(row["id"]))
    try:
        from app.services import notifications

        notifications.audit(user.id, "assistant.memory_deleted", "assistant_memory", str(row["id"]),
                            {"kind": row.get("kind"), "origin_tainted": bool(row.get("origin_tainted"))})
    except Exception:  # noqa: BLE001 - the note is gone; the tombstone is best effort
        logger.exception("assistant: memory delete audit failed")
    return Response(status_code=status.HTTP_204_NO_CONTENT)

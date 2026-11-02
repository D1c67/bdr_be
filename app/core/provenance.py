"""Actor provenance: who is really behind a write (docs/ASSISTANT.md, Actor
provenance details).

The assistant acts inside BDR as a server-minted principal whose CurrentUser
is the human (created_by and every actor FK stay the human for integrity).
What distinguishes an assistant write from a human one is this contextvar:
the assistant shim (app/assistant/shim.py) enters `acting_as_assistant` around
every in-process request, and every writer that fills the 0172 provenance
columns reads `fields()` instead of threading a new argument through 60
router signatures.

Outside an assistant run `fields()` is empty, so a writer can spread it
unconditionally and a human write sends no new key at all (the column
defaults fill 'human' and NULL; code deployed before 0172 never names a
column that does not exist yet):

    row = {"actor_id": user.id, **provenance.fields(user.id), ...}

`stamp()` is for the one table whose provenance means "last writer"
(labor_reviews): it always sends both keys, so a human save resets the row.
`metadata_fields()` is the same pair for notification metadata.

The actor label "<Full Name>'s assistant" is never stored; `display_actor`
derives it wherever an actor is rendered, from the human's current name.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

ACTOR_HUMAN = "human"
ACTOR_ASSISTANT = "assistant"
ACTOR_SYSTEM = "system"
ACTOR_KINDS: frozenset[str] = frozenset({ACTOR_HUMAN, ACTOR_ASSISTANT, ACTOR_SYSTEM})

ASSISTANT_LABEL_SUFFIX = "'s assistant"
UNKNOWN_ACTOR = "Unknown user"


@dataclass(frozen=True)
class AssistantActor:
    """The identity the shim set for the current request: the human the
    assistant acts for, the run (turn or approval execution) and, when the
    request executes an approved action, the approval row it came from."""

    user_id: str
    run_id: str
    approval_id: str | None = None


_current: ContextVar[AssistantActor | None] = ContextVar("bdr_assistant_actor", default=None)


def current() -> AssistantActor | None:
    """The assistant actor of this context, or None for a human or system
    request."""
    return _current.get()


def is_assistant() -> bool:
    return _current.get() is not None


@contextmanager
def acting_as_assistant(
    user_id: str, run_id: str, approval_id: str | None = None
) -> Iterator[AssistantActor]:
    """Mark everything inside the block as done by `user_id`'s assistant in
    run `run_id`. Nesting is refused: a run never acts for a second user.

    Context variables follow the task: Starlette copies the context into the
    threadpool a sync dependency or route runs in, and into the tasks its
    middleware spawns, so a `def` route called through the shim sees it."""
    if not user_id or not run_id:
        raise ValueError("acting_as_assistant needs a user_id and a run_id")
    if _current.get() is not None:
        raise RuntimeError("already acting as an assistant in this context")
    actor = AssistantActor(user_id=str(user_id), run_id=str(run_id),
                           approval_id=str(approval_id) if approval_id else None)
    token = _current.set(actor)
    try:
        yield actor
    finally:
        _current.reset(token)


def fields(actor_id: str | None = None) -> dict[str, str]:
    """The two 0172 provenance columns for a row written right now:
    {"actor_kind": "assistant", "assistant_run_id": <run>} inside an
    assistant run, {} otherwise (the column defaults record a human).

    `actor_id` is the actor the row names (actor_id, user_id, sent_by,
    uploaded_by, ...). When it is given and is not the human the assistant
    acts for, the row is someone else's (a notification fanned out to a
    second user, a system write inside the request), so it gets {} too.
    The check constraint (actor_kind <> 'assistant' or assistant_run_id is
    not null) can never trip on this output."""
    actor = _current.get()
    if actor is None:
        return {}
    if actor_id is not None and str(actor_id) != actor.user_id:
        return {}
    return {"actor_kind": ACTOR_ASSISTANT, "assistant_run_id": actor.run_id}


def stamp() -> dict[str, str | None]:
    """Both provenance columns, always: the assistant pair inside a run,
    {"actor_kind": "human", "assistant_run_id": None} otherwise. Only for
    rows whose provenance means "last writer" (labor_reviews, rewritten by
    an upsert), where a human save must reset an earlier assistant tag."""
    actor = _current.get()
    if actor is None:
        return {"actor_kind": ACTOR_HUMAN, "assistant_run_id": None}
    return {"actor_kind": ACTOR_ASSISTANT, "assistant_run_id": actor.run_id}


def metadata_fields() -> dict[str, str]:
    """The provenance pair for a notification's metadata jsonb: {} outside
    an assistant run, else the same two keys as `fields()`."""
    actor = _current.get()
    if actor is None:
        return {}
    return {"actor_kind": ACTOR_ASSISTANT, "assistant_run_id": actor.run_id}


def display_actor(full_name: str | None, actor_kind: str | None = ACTOR_HUMAN) -> str:
    """The actor as a record renders it: the human's current name, or
    "<name>'s assistant" when the row carries actor_kind='assistant'. The
    label is derived here, never stored, so a rename never rewrites history.
    A missing name falls back to UNKNOWN_ACTOR so the suffix never dangles."""
    name = (full_name or "").strip() or UNKNOWN_ACTOR
    if actor_kind == ACTOR_ASSISTANT:
        return f"{name}{ASSISTANT_LABEL_SUFFIX}"
    return name

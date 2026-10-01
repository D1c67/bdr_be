"""Calling In (docs/CALLING_IN.md section 6): the two call lists, a project's
per-GC detail, and logging / editing / deleting calls.

Reads take `require_internal` (writers plus the read-only Accountant), writes
`require_writer`; deleting a call is Executive and IT Admin only. Every
handler is a plain `def`: the Supabase SDK is sync and must run in FastAPI's
threadpool, never on the event loop.

The actual bid date is shown on these routes to everyone who can open them
(owner decision 1, section 2): every project here has already been sent. The
project-page call log (`GET /projects/{id}/call-log`) is NOT part of that
exception and keeps the normal ACTUAL_BID_VIEWER_ROLES redaction.
"""

from __future__ import annotations

import uuid
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.core.deps import CurrentUser, require_internal, require_role, require_writer
from app.core.roles import ACTUAL_BID_VIEWER_ROLES, Role
from app.core.supabase_client import get_supabase
from app.services import calling_in as ci
from app.services.notifications import audit

router = APIRouter(prefix="/calling-in", tags=["calling-in"])
# The read-only call log lives on the project page, under /projects.
project_router = APIRouter(prefix="/projects", tags=["calling-in"])

Round = Literal["pre_bid", "post_bid"]
Outcome = Literal["spoke", "voicemail", "no_answer"]

NOTE_MAX = 4000
NAME_MAX = 200
PHONE_MAX = 50

_ROUND_WORDS = {ci.PRE_BID: "before-bid", ci.POST_BID: "after-bid"}


# ── bodies ───────────────────────────────────────────────────────────────────


def _clean_note(value: str | None) -> str | None:
    if value is None:
        return None
    note = str(value).strip()
    if not note:
        raise ValueError("A note is required.")
    if len(note) > NOTE_MAX:
        raise ValueError(f"The note is longer than {NOTE_MAX} characters.")
    return note


class NewContactIn(BaseModel):
    """A GC contact created from the call screen: an ordinary gc_contacts row
    on that GC (name required, phone and email optional)."""

    name: str = Field(max_length=NAME_MAX)
    phone: str | None = Field(default=None, max_length=PHONE_MAX)
    email: EmailStr | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value):
        name = str(value or "").strip()
        if not name:
            raise ValueError("A new contact needs a name.")
        return name

    @field_validator("phone", "email", mode="before")
    @classmethod
    def _blank_to_none(cls, value):
        if value is None:
            return None
        value = str(value).strip()
        return value or None


class CallIn(BaseModel):
    gc_id: UUID
    round: Round
    outcome: Outcome
    note: str
    contact_ids: list[UUID] = Field(default_factory=list, max_length=50)
    new_contacts: list[NewContactIn] = Field(default_factory=list, max_length=20)

    @field_validator("note", mode="before")
    @classmethod
    def _note(cls, value):
        return _clean_note(value)

    @model_validator(mode="after")
    def _one_contact(self):
        if not self.contact_ids and not self.new_contacts:
            raise ValueError("Pick at least one contact (or add a new one).")
        return self


class CallPatch(BaseModel):
    """Any of the fields. `contact_ids`, when sent, replaces the call's
    contacts; `new_contacts` are created on the GC and added."""

    outcome: Outcome | None = None
    note: str | None = None
    contact_ids: list[UUID] | None = Field(default=None, max_length=50)
    new_contacts: list[NewContactIn] | None = Field(default=None, max_length=20)

    @field_validator("note", mode="before")
    @classmethod
    def _note(cls, value):
        return _clean_note(value)


# ── helpers ──────────────────────────────────────────────────────────────────


def _uuid_or_404(value: str, what: str = "Project") -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found") from None


def _facts_or_404(sb, project_id: str) -> ci.ProjectFacts:
    facts = ci.load_facts(sb, [project_id]).get(project_id)
    if facts is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return facts


def _existing_contacts(sb, gc_id: str, contact_ids: list[UUID]) -> list[dict]:
    """Snapshot entries for existing contacts, in the order given. 422 unless
    every id is a contact of this GC."""
    wanted = list(dict.fromkeys(str(c) for c in contact_ids))
    if not wanted:
        return []
    rows = (
        sb.table("gc_contacts")
        .select("id, gc_id, name, email, phone")
        .in_("id", wanted)
        .execute()
    ).data or []
    by_id = {r["id"]: r for r in rows}
    if any(cid not in by_id or by_id[cid].get("gc_id") != gc_id for cid in wanted):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Every contact on a call must be a contact of that GC.",
        )
    return [
        {
            "gc_contact_id": cid,
            "name": by_id[cid].get("name") or "",
            "phone": by_id[cid].get("phone"),
            "email": by_id[cid].get("email"),
        }
        for cid in wanted
    ]


def _create_contacts(sb, gc_id: str, new_contacts: list[NewContactIn]) -> list[dict]:
    """Create the new contacts on the GC first (they show up everywhere else)
    and return their snapshot entries."""
    if not new_contacts:
        return []
    payload = [
        {
            "gc_id": gc_id,
            "name": c.name,
            "phone": c.phone,
            "email": str(c.email) if c.email else None,
        }
        for c in new_contacts
    ]
    inserted = (sb.table("gc_contacts").insert(payload).execute()).data or []
    return [
        {
            "gc_contact_id": r.get("id"),
            "name": r.get("name") or "",
            "phone": r.get("phone"),
            "email": r.get("email"),
        }
        for r in inserted
    ]


def _call_or_404(sb, call_id: str) -> dict:
    rows = (
        sb.table("call_in_calls").select(ci.CALL_COLS).eq("id", call_id).limit(1).execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call not found")
    return rows[0]


def _serialize(sb, row: dict, user: CurrentUser) -> dict:
    names = ci.profile_names(sb, [row["created_by"]] if row.get("created_by") else [])
    return ci.serialize_call(row, user_id=user.id, role=user.role, user_names=names)


def _sync(project_id: str) -> None:
    """Close the entry the change cleared, inside the request. Best-effort:
    the call is already saved and the poller is the backstop."""
    try:
        ci.sync_project_entries(project_id)
    except Exception:  # noqa: BLE001
        ci.logger.exception("calling in: in-request entry sync failed (project=%s)", project_id)


# ── reads ────────────────────────────────────────────────────────────────────


@router.get("/summary")
def calling_in_summary(_: CurrentUser = Depends(require_internal)):
    """{open_count, pre_bid, post_bid} for the sidebar badge."""
    return ci.current_summary()


@router.get("")
def calling_in_lists(_: CurrentUser = Depends(require_internal)):
    """Both lists (section 6 Entry shape), sorted by bid_at ascending."""
    return ci.current_lists()


@router.get("/projects/{project_id}")
def calling_in_project(
    project_id: str,
    round_: str | None = Query(None, alias="round", pattern="^(pre_bid|post_bid)$"),
    user: CurrentUser = Depends(require_internal),
):
    """One project's GCs for a round: pricing we sent, done state, contacts
    and calls. Works for either round whether or not the project is on that
    list now. 404 when the project has no eligible (sent) GC."""
    project_id = _uuid_or_404(project_id)
    sb = get_supabase()
    now = ci._now()
    facts = _facts_or_404(sb, project_id)
    if not facts.sends:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No proposal has been sent on this project")
    ci.apply_live_gc_names(sb, [facts])
    round_ = round_ or ci.default_round(facts, now)
    gc_ids = sorted({s.gc_id for s in ci.round_sends(facts, round_)})

    contacts_by_gc: dict[str, list[dict]] = {}
    if gc_ids:
        rows = (
            sb.table("gc_contacts")
            .select("id, gc_id, name, email, phone")
            .in_("gc_id", gc_ids)
            .execute()
        ).data or []
        for r in rows:
            contacts_by_gc.setdefault(r["gc_id"], []).append(r)

    links = (
        sb.table("project_gcs").select("id, gc_id").eq("project_id", project_id).execute()
    ).data or []
    project_contact_ids: set[str] = set()
    if links:
        selected = (
            sb.table("project_gc_contacts")
            .select("project_gc_id, gc_contact_id")
            .in_("project_gc_id", [link["id"] for link in links])
            .execute()
        ).data or []
        project_contact_ids = {s["gc_contact_id"] for s in selected if s.get("gc_contact_id")}

    outcome_rows = (
        sb.table("bid_gc_outcomes")
        .select("gc_id, gc_award_result")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    gc_outcomes = {r["gc_id"]: r.get("gc_award_result") for r in outcome_rows}

    entered = ci.entered_map(ci.load_open_entries(sb, project_id)).get((project_id, round_))
    return ci.build_detail(
        facts,
        round_,
        now,
        entered_at=entered,
        contacts_by_gc=contacts_by_gc,
        project_contact_ids=project_contact_ids,
        gc_outcomes=gc_outcomes,
        user_id=user.id,
        role=user.role,
        user_names=ci.profile_names(sb, ci.call_user_ids(facts)),
    )


@project_router.get("/{project_id}/call-log")
def project_call_log(project_id: str, user: CurrentUser = Depends(require_internal)):
    """Read-only call log for the project page: both rounds, every call.
    bid_at follows the normal actual-bid redaction."""
    project_id = _uuid_or_404(project_id)
    sb = get_supabase()
    now = ci._now()
    facts = _facts_or_404(sb, project_id)
    ci.apply_live_gc_names(sb, [facts])
    sent_ids = {s.gc_id for s in facts.sends}
    extra = ci.gc_names(sb, {c.gc_id for c in facts.calls if c.gc_id not in sent_ids})
    return ci.build_call_log(
        facts,
        now,
        user_id=user.id,
        role=user.role,
        user_names=ci.profile_names(sb, ci.call_user_ids(facts)),
        extra_gc_names=extra,
        show_bid_at=user.role in ACTUAL_BID_VIEWER_ROLES,
    )


# ── writes ───────────────────────────────────────────────────────────────────


@router.post("/projects/{project_id}/calls", status_code=status.HTTP_201_CREATED)
def log_call(project_id: str, body: CallIn, user: CurrentUser = Depends(require_writer)):
    """Log a call. 409 when the round's window is closed for that GC or the
    round does not cover the GC; 422 when a contact is not that GC's. New
    contacts are created on the GC first. The call that clears the last GC
    closes the list entry and dismisses its notifications right here."""
    project_id = _uuid_or_404(project_id)
    sb = get_supabase()
    now = ci._now()
    facts = _facts_or_404(sb, project_id)
    gc_id = str(body.gc_id)
    words = _ROUND_WORDS[body.round]
    if not any(s.gc_id == gc_id for s in ci.round_sends(facts, body.round)):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This GC is not on the {words} call list for this project "
            "(no proposal was sent to them for this round).",
        )
    if not ci.gc_window_open(facts, body.round, gc_id, now):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"The {words} call window is closed for this project."
        )
    contacts = _existing_contacts(sb, gc_id, body.contact_ids)
    created = _create_contacts(sb, gc_id, body.new_contacts)
    row = (
        sb.table("call_in_calls")
        .insert(
            {
                "project_id": project_id,
                "gc_id": gc_id,
                "round": body.round,
                "outcome": body.outcome,
                "note": body.note,
                "contacts": contacts + created,
                "called_at": ci.iso(now),
                "created_by": user.id,
            }
        )
        .execute()
    ).data[0]
    audit(
        user.id,
        "call_in.log",
        "project",
        project_id,
        {
            "call_id": row.get("id"),
            "gc_id": gc_id,
            "round": body.round,
            "outcome": body.outcome,
            "contacts": len(contacts) + len(created),
            "new_contacts": len(created),
        },
    )
    if body.outcome == ci.DONE_OUTCOME:
        _sync(project_id)
    return _serialize(sb, row, user)


@router.patch("/calls/{call_id}")
def edit_call(call_id: str, body: CallPatch, user: CurrentUser = Depends(require_writer)):
    """Edit a call: the author only."""
    call_id = _uuid_or_404(call_id, "Call")
    sb = get_supabase()
    row = _call_or_404(sb, call_id)
    if not row.get("created_by") or row["created_by"] != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only the person who logged this call can edit it."
        )
    patch: dict = {}
    if body.outcome is not None and body.outcome != row.get("outcome"):
        patch["outcome"] = body.outcome
    if body.note is not None and body.note != row.get("note"):
        patch["note"] = body.note
    if body.contact_ids is not None or body.new_contacts:
        if body.contact_ids is not None:
            base = _existing_contacts(sb, row["gc_id"], body.contact_ids)
        else:
            base = list(row.get("contacts") or [])
        if not base and not body.new_contacts:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "A call needs at least one contact."
            )
        patch["contacts"] = base + _create_contacts(sb, row["gc_id"], body.new_contacts or [])
    if not patch:
        return _serialize(sb, row, user)
    fields = sorted(patch)
    patch["edited_by"] = user.id
    updated = (
        sb.table("call_in_calls").update(patch).eq("id", call_id).execute()
    ).data or [{**row, **patch}]
    audit(
        user.id,
        "call_in.edit",
        "project",
        row["project_id"],
        {"call_id": call_id, "gc_id": row["gc_id"], "round": row["round"], "fields": fields},
    )
    _sync(row["project_id"])
    return _serialize(sb, updated[0], user)


@router.delete("/calls/{call_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_call(
    call_id: str,
    user: CurrentUser = Depends(require_role(Role.EXECUTIVE, Role.IT_ADMIN)),
):
    """Delete a call: Executive and IT Admin only."""
    call_id = _uuid_or_404(call_id, "Call")
    sb = get_supabase()
    row = _call_or_404(sb, call_id)
    sb.table("call_in_calls").delete().eq("id", call_id).execute()
    audit(
        user.id,
        "call_in.delete",
        "project",
        row["project_id"],
        {
            "call_id": call_id,
            "gc_id": row["gc_id"],
            "round": row["round"],
            "outcome": row["outcome"],
            "created_by": row.get("created_by"),
        },
    )
    _sync(row["project_id"])
    return Response(status_code=status.HTTP_204_NO_CONTENT)

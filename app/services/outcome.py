"""Win/Loss (bid outcome), the final step after a bid is submitted.

G3 bids the same job to several GCs, so the outcome is two independent facts PER
GC: did that GC win the overall job, and did that GC go with our number? The PA
records this once GCs report back (often partial — most fields tolerate 'unknown').
G3's overall result (won/lost/no_award), the winning GC, and a free-text note live
on the project-level `bid_outcomes` row.

"Which GCs we bid to" is the set of `proposal_sends` rows with status='sent' — the
same definition Send Out uses. Each carries the number we bid that GC
(material_amount + labor_amount); we snapshot it onto the per-GC outcome at record
time so "how far off our number was" stays correct even if pricing later changes.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.models.schemas import BidOutcomeIn
from app.services import workflow
from app.services.notifications import audit, notify_role


class OutcomeError(Exception):
    """User-actionable failure; the router surfaces .args[0] as the detail."""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


# ── pure helpers (no DB) ─────────────────────────────────────────────────────


def _dec(v) -> Decimal | None:
    return Decimal(str(v)) if v is not None else None


def _s(v: Decimal | None) -> str | None:
    return str(v) if v is not None else None


def our_amount_of(material, labor) -> Decimal | None:
    """The number we bid a GC = material + labor. None only when both are absent
    (legacy proposal_sends rows generated before per-GC amounts existed)."""
    if material is None and labor is None:
        return None
    return (_dec(material) or Decimal(0)) + (_dec(labor) or Decimal(0))


def won_via_us(gc_rows: list[dict]) -> bool:
    """True when some GC won the job AND went with our number — the only shape
    that means G3 actually gets the work. Drives the UI's suggested result."""
    return any(
        r.get("gc_award_result") == "won" and r.get("our_bid_selection") == "used_us"
        for r in gc_rows
    )


def merge_gc_outcomes(sent_gcs: list[dict], recorded_rows: list[dict]) -> list[dict]:
    """One row per GC we bid to, merging any recorded outcome onto it. Pure so it
    can be unit-tested without a DB. Decimals serialize as strings (proposal
    convention); unrecorded GCs default to 'unknown'."""
    by_gc = {r["gc_id"]: r for r in recorded_rows}
    out = []
    for g in sent_gcs:
        rec = by_gc.get(g["gc_id"]) or {}
        out.append(
            {
                "gc_id": g["gc_id"],
                "gc_name": g["gc_name"],
                "emails": g.get("emails", []),
                "our_amount": _s(g.get("our_amount")),
                "gc_award_result": rec.get("gc_award_result", "unknown"),
                "our_bid_selection": rec.get("our_bid_selection", "unknown"),
                "winning_amount": _s(_dec(rec.get("winning_amount"))),
            }
        )
    return out


# ── DB-backed ────────────────────────────────────────────────────────────────


def _sent_gcs(project_id: str) -> list[dict]:
    """The GCs we actually bid to (sent proposals), with the number we bid each
    and their contact emails (for the 'request feedback' reminder). Sorted by name."""
    rows = (
        get_supabase()
        .table("proposal_sends")
        .select(
            "gc_id, gc_name, material_amount, labor_amount,"
            " general_contractors(gc_contacts(email))"
        )
        .eq("project_id", project_id)
        .eq("status", "sent")
        .execute()
    ).data or []
    out = []
    for r in rows:
        gc = r.get("general_contractors") or {}
        emails = sorted(
            c["email"] for c in (gc.get("gc_contacts") or []) if c.get("email")
        )
        out.append(
            {
                "gc_id": r["gc_id"],
                "gc_name": r["gc_name"],
                "our_amount": our_amount_of(r.get("material_amount"), r.get("labor_amount")),
                "emails": emails,
            }
        )
    return sorted(out, key=lambda g: g["gc_name"].lower())


def outcome_overview(project_id: str) -> dict:
    """Everything the Win/Loss panel needs: the per-GC grid (seeded from the GCs
    we bid to, merged with anything already recorded) plus the project-level
    result/winning GC/notes."""
    sb = get_supabase()
    sent = _sent_gcs(project_id)
    outcome = (
        sb.table("bid_outcomes").select("*").eq("project_id", project_id).execute()
    ).data
    outcome = outcome[0] if outcome else None
    gc_rows = (
        sb.table("bid_gc_outcomes").select("*").eq("project_id", project_id).execute()
    ).data or []
    gcs = merge_gc_outcomes(sent, gc_rows)
    return {
        "recorded": outcome is not None,
        "result": outcome["result"] if outcome else None,
        "winning_gc_id": outcome.get("winning_gc_id") if outcome else None,
        "notes": outcome.get("notes") if outcome else None,
        "recorded_at": outcome.get("recorded_at") if outcome else None,
        "suggested_result": "won" if won_via_us(gcs) else None,
        "gcs": gcs,
    }


def record_outcome(project_id: str, user_id: str, body: BidOutcomeIn) -> dict:
    """Record (or correct) the bid outcome. From 'submitted' this also transitions
    the project to the terminal 'bid_outcome' stage; once there, re-recording just
    updates in place so the PA can fix it as more feedback comes in."""
    sb = get_supabase()
    project = (
        sb.table("projects").select("id, name, current_stage").eq("id", project_id)
        .single().execute()
    ).data
    if not project:
        raise OutcomeError("Project not found", status_code=404)
    if project["current_stage"] not in ("submitted", "bid_outcome"):
        raise OutcomeError(
            "Project is not awaiting an outcome — the bid must be Submitted first."
        )

    sent = _sent_gcs(project_id)
    sent_by_id = {g["gc_id"]: g for g in sent}
    if body.winning_gc_id is not None and body.winning_gc_id not in sent_by_id:
        raise OutcomeError("The winning GC must be one of the GCs we bid to.", status_code=400)
    for gc in body.gcs:
        if gc.gc_id not in sent_by_id:
            raise OutcomeError(
                "An outcome was submitted for a GC we did not bid to.", status_code=400
            )

    sb.table("bid_outcomes").upsert(
        {
            "project_id": project_id,
            "result": body.result,
            "winning_gc_id": body.winning_gc_id,
            "notes": body.notes,
            "recorded_by": user_id,
        },
        on_conflict="project_id",
    ).execute()

    if body.gcs:
        sb.table("bid_gc_outcomes").upsert(
            [
                {
                    "project_id": project_id,
                    "gc_id": gc.gc_id,
                    "gc_award_result": gc.gc_award_result,
                    "our_bid_selection": gc.our_bid_selection,
                    # our_amount is snapshotted server-side from what we actually
                    # bid that GC — never trusted from the client.
                    "our_amount": _s(sent_by_id[gc.gc_id]["our_amount"]),
                    "winning_amount": _s(gc.winning_amount),
                }
                for gc in body.gcs
            ],
            on_conflict="project_id,gc_id",
        ).execute()

    if project["current_stage"] == "submitted":
        note = f"Outcome recorded: {body.result}"
        if body.winning_gc_id:
            note += f" — won by {sent_by_id[body.winning_gc_id]['gc_name']}"
        workflow.transition_project(project_id, "bid_outcome", user_id, note)

    for role in (Role.PM, Role.EXECUTIVE):
        notify_role(
            role, project_id, "bid_outcome",
            f"Bid outcome recorded ({body.result}) for {project['name']}",
        )
    audit(user_id, "project.bid_outcome", "project", project_id,
          {"result": body.result, "winning_gc_id": body.winning_gc_id, "gcs": len(body.gcs)})
    return outcome_overview(project_id)

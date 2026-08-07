"""Per-GC proposal generation and dispatch (Send Out, step 10).

Cross-GC isolation is the design driver: a proposal reaching the wrong GC
would be commercially damaging, so every layer re-proves the mapping —
generation stamps gc_id on the file and a content hash on the send row, and
`assert_send_isolation` re-verifies the exact bytes against the live GC row
immediately before each email. Sends are per-GC with independent failure.

Every GC on the project is a bid candidate; deciding NOT to bid to one is
done by simply never sending them a proposal. The stage ends only by the
PA's explicit "Done sending" (`complete_send_out`) — never automatically —
and the never-sent GCs recorded at that moment are the durable "did not bid
to them" evidence (there is no flag; the absence of a sent row IS the data).

Recipients: each GC has gc_contacts (0028); the PA picks contacts per send in
the confirm dialog (default all with an email). proposal_sends.gc_email holds
the recipient list a send actually used — written when the row is claimed for
sending so crash recovery can match it against email_log.to_addrs exactly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx

from app.core.config import get_settings
from app.core.supabase_client import get_supabase
from app.services import email_branding, graph_email, office_preview, storage
from app.services.notifications import audit, dismiss_notifications, notify_role
from app.services.proposal_docx import (
    DOCX_MIME,
    ProposalContext,
    ProposalRenderError,
    build_filename,
    load_template_bytes,
    render_proposal,
    validate_output,
    validate_pdf_isolation,
)

logger = logging.getLogger(__name__)

# Read-side claim staleness: a row stuck at 'sending' longer than this is
# presumed crashed and may be reclaimed (after the email_log duplicate check).
SENDING_STALE_MINUTES = 10

OUTCOME_UNKNOWN_PREFIX = "outcome unknown"
# Exceptions where Graph may have accepted the message even though we never
# read the response — retrying blindly could double-send.
_OUTCOME_UNKNOWN_EXC = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)

LABOR_TIME_TEXT = {"day_work": "DAY", "night_work": "NIGHT"}
WAGE_TEXT = {"prevailing_wage": "Prevailing Wage", "non_prevailing_wage": "Non-prevailing wage"}


class ProposalSendError(Exception):
    """User-actionable failure; the router surfaces .args[0] as the detail."""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


# ── amounts ────────────────────────────────────────────────────────────────


# Wire keys of the per-GC bid figures, in display order. 'material' is the
# residual materials bucket; the three breakouts are None while their section
# is not on the project.
AMOUNT_KEYS = ("material", "gear", "underground", "low_voltage", "labor")
_SECTION_AMOUNT_KEYS = ("gear", "underground", "low_voltage")


def resolved_verify_numbers(originals: dict, verification: dict) -> dict[str, Decimal | None]:
    """Pure: the ten pricing figures (labor/residual-materials/section costs +
    the five markups) after the verify step. A COMMITTED snapshot is read as-is
    (the commit stored every resolved number): a NULL legacy figure is 0 and a
    NULL section figure is None, meaning the section was not part of the
    committed decomposition, which keeps legacy pre-sections snapshots (where
    materials_amount carries the full figure) computing exactly as before.
    Uncommitted: the draft override wins per key, else the live upstream
    figure; missing legacy figures resolve to 0, absent sections to None."""
    from app.routers.pricing import VERIFY_NUMBERS, VERIFY_SECTION_NUMBERS, _num

    committed = bool(verification and verification.get("committed_at"))
    final: dict[str, Decimal | None] = {}
    for key in VERIFY_NUMBERS:
        v = _num(verification, key)
        if v is None and not committed:
            v = originals.get(key)
        if v is None and key not in VERIFY_SECTION_NUMBERS:
            v = Decimal(0)
        final[key] = v
    return final


def amounts_from_final(final: dict[str, Decimal | None]) -> dict[str, Decimal | None]:
    """Pure: the default per-GC bid figures, each section's cost plus its
    project-wide markup. An absent section (cost None) stays None; a present
    section with no markup entered carries its cost as-is. The total sums the
    present figures only."""
    material = final["materials_amount"] + final["materials_markup_amount"]
    labor = final["labor_amount"] + final["labor_markup_amount"]
    out: dict[str, Decimal | None] = {"material": material}
    total = material + labor
    for key in _SECTION_AMOUNT_KEYS:
        amount = final[f"{key}_amount"]
        if amount is None:
            out[key] = None
        else:
            out[key] = amount + (final[f"{key}_markup_amount"] or Decimal(0))
            total += out[key]
    out["labor"] = labor
    out["total"] = total
    return out


def proposal_amounts(originals: dict, verification: dict) -> dict[str, Decimal | None]:
    """The green table's figures (material, present sections, labor, TOTAL).
    Committed snapshots are read as-is; uncommitted figures fall back to the
    upstream values (same semantics the verify step and pricing summary use)."""
    return amounts_from_final(resolved_verify_numbers(originals, verification))


def pricing_basis(final: dict[str, Decimal | None]) -> dict[str, Decimal | None]:
    """Pure: the shared cost basis and default markups every per-GC price
    decomposes against. The costs are the project's: a per-GC price change
    never moves them; it lands entirely in that GC's markup. A section not on
    the project has a None cost and a None markup."""
    return {
        "material_cost": final["materials_amount"],
        "gear_cost": final["gear_amount"],
        "underground_cost": final["underground_amount"],
        "low_voltage_cost": final["low_voltage_amount"],
        "labor_cost": final["labor_amount"],
        "material_markup": final["materials_markup_amount"],
        "gear_markup": final["gear_markup_amount"],
        "underground_markup": final["underground_markup_amount"],
        "low_voltage_markup": final["low_voltage_markup_amount"],
        "labor_markup": final["labor_markup_amount"],
    }


def gc_markups(
    basis: dict[str, Decimal | None], resolved: dict[str, Decimal | None]
) -> dict[str, Decimal | None]:
    """Pure: one GC's effective markups: its resolved price minus the shared
    cost basis, per section. Changing a GC's figure for one section moves only
    that section's markup. A below-cost price yields a negative markup, carried
    as-is: never clamped and never rebalanced into the cost side. Absent
    sections produce None."""
    out: dict[str, Decimal | None] = {}
    for key in AMOUNT_KEYS:
        cost = basis[f"{key}_cost"]
        price = resolved[key]
        out[f"{key}_markup"] = price - cost if cost is not None and price is not None else None
    return out


def resolve_gc_amounts(
    defaults: dict[str, Decimal | None], gc: dict
) -> dict[str, Decimal | None]:
    """One GC's bid figures: G3 sometimes bids different numbers to different
    GCs, so a per-GC override (project_gcs) wins per figure over the
    committed-pricing default. Sections not on the project stay None. The total
    always re-sums over the present figures, never stored."""
    out: dict[str, Decimal | None] = {}
    for key in AMOUNT_KEYS:
        override = gc.get(f"{key}_override")
        out[key] = override if override is not None else defaults.get(key)
    out["total"] = sum((v for v in out.values() if v is not None), Decimal(0))
    return out


# proposal_sends stamp columns, keyed by their wire/amount name.
_STAMP_COLUMNS = {
    "material": "material_amount",
    "gear": "gear_amount",
    "underground": "underground_amount",
    "low_voltage": "low_voltage_amount",
    "labor": "labor_amount",
}


def stamped_amounts(row: dict) -> dict[str, Decimal | None] | None:
    """The five figures rendered into this row's document at generation time
    (None per key = that section was not on the project); None for rows
    generated before per-GC amounts existed (nothing to check)."""
    if row.get("material_amount") is None and row.get("labor_amount") is None:
        return None
    return {key: _dec(row.get(col)) for key, col in _STAMP_COLUMNS.items()}


def stamp_figures(stamped: dict[str, Decimal | None]) -> tuple[str, ...]:
    """The formatted figures a stamped document must carry: material, each
    stamped section, labor, then the re-summed total (same ordering the box
    renders)."""
    values = [stamped.get(key) for key in AMOUNT_KEYS]
    present = [v for v in values if v is not None]
    total = sum(present, Decimal(0))
    return tuple(format_money(v) for v in present) + (format_money(total),)


def section_validation_kwargs(
    stamped: dict[str, Decimal | None] | None, includes_generator: bool
) -> dict:
    """Pure: the section-aware kwargs validate_output / validate_pdf_isolation
    take, derived from a row's stamps. A NULL section stamp means that row was
    removed from the document, so its label must be absent; the generator
    caption exists only when the gear row itself was rendered. Pre-feature rows
    (no stamps) skip the section checks entirely, keeping their behavior."""
    if stamped is None:
        return {"includes_generator": False, "removed_sections": ()}
    return {
        "includes_generator": includes_generator and stamped.get("gear") is not None,
        "removed_sections": tuple(
            key for key in _SECTION_AMOUNT_KEYS if stamped.get(key) is None
        ),
    }


def format_money(d: Decimal) -> str:
    if d == d.to_integral_value():
        return f"${d:,.0f}"
    return f"${d:,.2f}"


def lines_hash(lines: list[str]) -> str:
    return hashlib.sha256(json.dumps(lines, separators=(",", ":")).encode()).hexdigest()


# ── cover email ────────────────────────────────────────────────────────────

GC_NAME_TOKEN = "<GC Name>"


def build_cover_email(project: dict) -> tuple[str, str]:
    """Subject + plain-text cover note. Plain text is the editable/stored form;
    the G3-branded HTML shell (logo + signature) is applied at send time via
    `email_branding.render_proposal_email`. The closing identity lives in the
    signature block, so the body must NOT re-sign "G3 Electrical"."""
    subject = f"[G3 Electrical] Proposal — {project['name']} ({project['number']})"
    body = (
        f"Dear {GC_NAME_TOKEN},\n\n"
        f"G3 Electrical is pleased to submit our proposal for "
        f"{project['name']} ({project['number']}). Please find our proposal attached.\n\n"
        f"We appreciate the opportunity to bid and look forward to working with you.\n\n"
        f"Thank you,"
    )
    return subject, body


# ── shared queries ─────────────────────────────────────────────────────────


def _dec(v) -> Decimal | None:
    return Decimal(str(v)) if v is not None else None


def _project_gcs(project_id: str) -> list[dict]:
    rows = (
        get_supabase()
        .table("project_gcs")
        .select(
            "proposal_material_amount, proposal_gear_amount,"
            " proposal_underground_amount, proposal_low_voltage_amount,"
            " proposal_labor_amount,"
            " general_contractors(id, name, gc_contacts(id, name, email))"
        )
        .eq("project_id", project_id)
        .execute()
    ).data or []
    out = []
    for r in rows:
        gc = r.get("general_contractors") or {}
        if gc.get("id"):
            out.append(
                {
                    "id": gc["id"],
                    "name": gc["name"],
                    "contacts": gc.get("gc_contacts") or [],
                    "material_override": _dec(r.get("proposal_material_amount")),
                    "gear_override": _dec(r.get("proposal_gear_amount")),
                    "underground_override": _dec(r.get("proposal_underground_amount")),
                    "low_voltage_override": _dec(r.get("proposal_low_voltage_amount")),
                    "labor_override": _dec(r.get("proposal_labor_amount")),
                }
            )
    return out


def join_recipients(recipients: list[str]) -> str:
    """Single source of the recipient-list string format. MUST match
    graph_email.send_mail's email_log.to_addrs join (', ') — crash recovery
    proves a send happened by exact string equality between the two."""
    return ", ".join(recipients)


def resolve_recipients(live_gc: dict, chosen_ids: list[str] | None) -> list[str]:
    """Emails for a send: the chosen contacts', or every contact with an email
    when no explicit choice was posted. Raises if a chosen contact vanished or
    lost its email — the PA confirmed a list that no longer exists, so fail
    closed and make them reopen the dialog. Sorted for determinism."""
    contacts = {c["id"]: c for c in (live_gc.get("contacts") or [])}
    if chosen_ids is None:
        picked = [c for c in contacts.values() if c.get("email")]
    else:
        picked = []
        for cid in dict.fromkeys(chosen_ids):  # de-dupe, keep order
            contact = contacts.get(cid)
            if not contact or not contact.get("email"):
                raise ProposalSendError(
                    f"A selected contact for {live_gc.get('name', 'this GC')} is no longer "
                    "on file (or has no email) — reopen the send dialog and review recipients."
                )
            picked.append(contact)
    return sorted({c["email"] for c in picked})


def cc_recipients(recipients: list[str]) -> list[str]:
    """The internal bids desk, copied on every proposal that goes out to a GC.
    Deliberately NOT merged into `recipients`: the To line is the isolation
    contract (assert_send_isolation requires every To address to be a live
    contact of this one GC, and gc_email to equal join_recipients exactly), so
    the CC rides alongside it instead. Configured via PROPOSAL_CC; empty
    disables it. Dropped when the address is already on the To line so nobody
    is addressed twice."""
    addr = (get_settings().proposal_cc or "").strip()
    if not addr:
        return []
    lowered = {r.lower() for r in recipients}
    return [] if addr.lower() in lowered else [addr]


def align_sections_to_basis(sections: dict[str, dict], basis: dict) -> dict[str, dict]:
    """A snapshot committed before the sections release has no breakout
    decomposition: its basis cost is null even when the category is live on the
    project, and set_gc_amounts 409s any override for it. The editors key off
    `present`, so it must mirror the basis, not the live partition, or the UI
    would offer a column every save rejects."""
    out = dict(sections)
    for key in ("gear", "underground", "low_voltage"):
        if basis.get(f"{key}_cost") is None and out.get(key, {}).get("present"):
            out[key] = {**out[key], "present": False}
    return out


def amounts_overview(project_id: str) -> dict:
    """The GC Pricing numbers editor's data: the pricing-base default plus each
    GC's overrides and resolved per-section figures. Absent sections are null
    everywhere (default, override, resolved, markup). Decimals go over the wire
    as strings (pricing_summary convention)."""
    from app.routers.pricing import (
        _get_one,
        _materials_rows,
        _sections_wire,
        _verify_originals,
        section_summary,
    )

    verification = _get_one("verifications", project_id)
    committed = bool(verification and verification.get("committed_at"))
    # The per-GC base: committed overrides win per key, else the upstream Markup
    # figure. Computed even before the Verify commit so the GC Pricing step (which
    # runs *before* Verify) always has a base for each GC to override against; the
    # `committed` flag below still tells the UI whether pricing is locked.
    final = resolved_verify_numbers(_verify_originals(project_id), verification or {})
    basis = pricing_basis(final)
    defaults = amounts_from_final(final)
    sections = align_sections_to_basis(
        _sections_wire(section_summary(_materials_rows(project_id))), basis
    )

    locked = {
        r["gc_id"]
        for r in (
            get_supabase()
            .table("proposal_sends")
            .select("gc_id, status")
            .eq("project_id", project_id)
            .in_("status", ["sent", "sending"])
            .execute()
        ).data
        or []
    }

    def _s(v: Decimal | None) -> str | None:
        return str(v) if v is not None else None

    gcs = []
    for gc in sorted(_project_gcs(project_id), key=lambda g: g["name"].lower()):
        resolved = resolve_gc_amounts(defaults, gc)
        markups = gc_markups(basis, resolved)
        entry = {"gc_id": gc["id"], "gc_name": gc["name"]}
        for key in AMOUNT_KEYS:
            entry[f"{key}_override"] = _s(gc[f"{key}_override"])
        for key in AMOUNT_KEYS:
            entry[key] = _s(resolved[key])
        entry["total"] = _s(resolved["total"])
        # This GC's effective markups (price minus shared cost, per section):
        # the ONLY thing a per-GC price change moves. Negative when the GC is
        # being bid below cost.
        for key in AMOUNT_KEYS:
            entry[f"{key}_markup"] = _s(markups[f"{key}_markup"])
        entry["locked"] = gc["id"] in locked
        gcs.append(entry)
    return {
        "committed": committed,
        "default": {
            "material": _s(defaults["material"]),
            "gear": _s(defaults["gear"]),
            "underground": _s(defaults["underground"]),
            "low_voltage": _s(defaults["low_voltage"]),
            "labor": _s(defaults["labor"]),
            "total": _s(defaults["total"]),
        },
        # The shared decomposition the per-GC figures read against: section
        # costs (never moved by a per-GC edit) plus the project-default markups.
        "basis": {k: _s(v) for k, v in basis.items()},
        # Which sections are on the project, with the category names behind
        # each (raw DB strings); drives which editors/rows the UI renders.
        "sections": sections,
        "gcs": gcs,
    }


# Send-out lane heads at which per-GC proposal work is still allowed: from GC
# Pricing (where the numbers are first set) through Bid Outcome. The stage head
# stopped being the lock here once GCs could be added late — a GC joining the
# project after "Done sending" still needs a document generated and sent, and a
# GC that already has the bid may need it emailed again.
#
# What protects an already-submitted bid is the PER-GC lock, not the stage: a
# row at 'sent'/'sending' refuses amount edits (set_gc_amounts), is excluded
# from generation targets, and is skipped by send_proposals. Those rules hold
# at every head, so widening the window cannot disturb a bid already out.
GC_AMOUNTS_EDITABLE_HEADS = frozenset(
    {"gc_pricing", "verify", "send_out", "submitted", "bid_outcome"}
)

# Heads at which documents may be generated and proposals sent / re-sent.
# Same set, named separately because they answer different questions.
SEND_WINDOW_HEADS = GC_AMOUNTS_EDITABLE_HEADS


def assert_gc_amounts_editable(current_task: str | None) -> None:
    if current_task not in GC_AMOUNTS_EDITABLE_HEADS:
        raise ProposalSendError(
            "GC pricing cannot be changed before the bid reaches GC Pricing."
        )


def send_window_head(project_id: str) -> str:
    """The send_out lane head, asserting proposal work is allowed at it.

    Replaces the old `current_task != "send_out"` guard on generate/send/mark.
    Send Out is still where the bid normally goes out; Submitted and Bid Outcome
    stay open so a late-added GC can be bid and any GC can be sent its document
    again. Only `complete_send_out` still demands the head be exactly 'send_out'
    — it is the transition itself, and it may only happen once."""
    from app.services import workflow

    head = workflow.load_category_state(project_id).get("send_out", {}).get("current_task")
    if head not in SEND_WINDOW_HEADS:
        raise ProposalSendError("Project has not reached the Send Out stage.")
    return head


# Post-submission proposal work is recorded in audit_log (every call below
# stamps `head`) and in proposal_send_events, NOT as a stage_event. Two reasons:
# analytics derives time-in-stage by diffing consecutive stage_events, so a
# same-stage row would split one "submitted" span into two bogus samples; and
# the "Done sending" note is a timestamped statement about the moment it was
# pressed, which later activity does not falsify. audit_log rows carrying
# entity='project' already surface in the activity feed.


def assert_section_overrides_allowed(
    basis: dict[str, Decimal | None], amounts: dict[str, Decimal | None]
) -> None:
    """Pure: a per-GC figure may only be set for a section that is on the
    project (its shared cost basis exists). Presence is monotonic (RFQs are
    never deleted), so an override can never be orphaned by a later change."""
    for key in _SECTION_AMOUNT_KEYS:
        if amounts.get(key) is not None and basis[f"{key}_cost"] is None:
            raise ProposalSendError("That pricing section is not on this project")


def set_gc_amounts(
    project_id: str,
    gc_id: str,
    amounts: dict[str, Decimal | None],
    user_id: str,
) -> dict:
    """Set (or clear, with None) one GC's proposal amount overrides (keys
    material / gear / underground / low_voltage / labor; full replace). Allowed
    from the GC Pricing step through Send Out, until that GC's proposal is
    sent/sending. An already-generated document goes stale — send fails closed
    on the stamp mismatch until regenerated."""
    sb = get_supabase()
    project = (
        sb.table("projects").select("id, current_stage").eq("id", project_id)
        .single().execute()
    ).data
    if not project:
        raise ProposalSendError("Project not found", status_code=404)
    from app.routers.pricing import _get_one, _verify_originals
    from app.services import workflow

    assert_gc_amounts_editable(
        workflow.load_category_state(project_id).get("send_out", {}).get("current_task")
    )
    membership = (
        sb.table("project_gcs").select("id").eq("project_id", project_id)
        .eq("gc_id", gc_id).limit(1).execute()
    ).data
    if not membership:
        raise ProposalSendError("GC is not on this project", status_code=404)
    locked = (
        sb.table("proposal_sends").select("id").eq("project_id", project_id)
        .eq("gc_id", gc_id).in_("status", ["sent", "sending"]).limit(1).execute()
    ).data
    if locked:
        raise ProposalSendError(
            "This GC's proposal has been sent (or a send is in progress) — amounts are locked."
        )
    verification = _get_one("verifications", project_id)
    basis = pricing_basis(
        resolved_verify_numbers(_verify_originals(project_id), verification or {})
    )
    assert_section_overrides_allowed(basis, amounts)
    sb.table("project_gcs").update(
        {
            col: str(amounts[key]) if amounts.get(key) is not None else None
            for key, col in (
                ("material", "proposal_material_amount"),
                ("gear", "proposal_gear_amount"),
                ("underground", "proposal_underground_amount"),
                ("low_voltage", "proposal_low_voltage_amount"),
                ("labor", "proposal_labor_amount"),
            )
        }
    ).eq("project_id", project_id).eq("gc_id", gc_id).execute()
    # Audit the change as what it is: a markup adjustment. The shared cost
    # basis is untouched by design; what moved is this GC's effective markup
    # per section (negative when the GC is now bid below cost).
    overview = amounts_overview(project_id)
    mine = next((g for g in overview["gcs"] if g["gc_id"] == gc_id), {})
    detail = {"gc_id": gc_id}
    for key in AMOUNT_KEYS:
        detail[f"{key}_amount"] = (
            str(amounts[key]) if amounts.get(key) is not None else None
        )
    for key in AMOUNT_KEYS:
        detail[f"{key}_markup"] = mine.get(f"{key}_markup")
    detail["cost_basis"] = {
        key: overview["basis"][f"{key}_cost"] for key in AMOUNT_KEYS
    }
    audit(user_id, "proposal.amounts_set", "project", project_id, detail)
    return overview


def retire_unsent_proposals(project_id: str, gc_id: str) -> None:
    """A GC removed from the project keeps its sent history; never-sent rows
    are retired so the Send Out panel stops offering them (the same sweep
    generate_documents runs for GCs it finds removed)."""
    get_supabase().table("proposal_sends").update({"status": "superseded"}).eq(
        "project_id", project_id
    ).eq("gc_id", gc_id).in_("status", ["generated", "failed"]).execute()


def send_out_outcome(
    gcs: list[dict], sent_gc_ids: set[str], external_gc_ids: set[str] = frozenset()
) -> tuple[list[str], list[str], list[str]]:
    """(emailed, external, skipped) GC names for the completion record.
    External = submitted through a third-party application ("Mark as
    submitted", no email from us); skipped = on the project but never sent a
    proposal — the 'decided not to bid to them' signal the stage-event note
    preserves."""
    emailed = sorted(
        g["name"] for g in gcs if g["id"] in sent_gc_ids and g["id"] not in external_gc_ids
    )
    external = sorted(g["name"] for g in gcs if g["id"] in external_gc_ids)
    skipped = sorted(g["name"] for g in gcs if g["id"] not in sent_gc_ids)
    return emailed, external, skipped


def build_done_sending_note(
    emailed: list[str], external: list[str], skipped: list[str]
) -> str:
    """The stage-event note "Done sending" writes — the durable prose record of
    who got the bid and how (emailed vs. third-party) and who we chose not to
    bid to."""
    parts = []
    if emailed:
        parts.append("emailed: " + ", ".join(emailed))
    if external:
        parts.append("submitted via third-party application: " + ", ".join(external))
    if skipped:
        parts.append("skipped (no bid): " + ", ".join(skipped))
    return "Done sending" + (" — " + "; ".join(parts) if parts else "")


def complete_send_out(project_id: str, user_id: str) -> dict:
    """The PA's explicit "Done sending" — the only way Send Out ends. Requires
    at least one sent proposal (a bid was actually submitted to someone,
    whether emailed by us or marked submitted via a third-party application);
    everything else is the PA's judgment, not a count the system enforces."""
    from app.core.roles import Role
    from app.services import workflow

    sb = get_supabase()
    project = (
        sb.table("projects").select("id, name, current_stage").eq("id", project_id)
        .single().execute()
    ).data
    if not project:
        raise ProposalSendError("Project not found", status_code=404)
    if workflow.load_category_state(project_id).get("send_out", {}).get("current_task") != "send_out":
        raise ProposalSendError("Project is not at the Send Out stage.")
    rows = (
        sb.table("proposal_sends").select("gc_id, status, sent_via").eq("project_id", project_id)
        .execute()
    ).data or []
    if any(r["status"] == "sending" for r in rows):
        raise ProposalSendError(
            "A proposal send is in progress or unresolved — wait or retry it first."
        )
    sent_gc_ids = {r["gc_id"] for r in rows if r["status"] == "sent"}
    external_gc_ids = {
        r["gc_id"] for r in rows if r["status"] == "sent" and r.get("sent_via") == "external"
    }
    if not sent_gc_ids:
        raise ProposalSendError(
            "No proposal has been sent yet — send at least one (or mark one as "
            "submitted) before marking the bid submitted.",
            status_code=400,
        )

    emailed, external, skipped = send_out_outcome(
        _project_gcs(project_id), sent_gc_ids, external_gc_ids
    )
    sent = sorted(emailed + external)
    note = build_done_sending_note(emailed, external, skipped)
    # Advance the send_out category head send_out → submitted.
    workflow.advance_category(project_id, "send_out", user_id, note)
    # Submission news for the whole bid team: both engineer focuses + Executive
    # (send-out is the Labor engineer's lane, but the Materials engineer's
    # pricing just went out the door too).
    for role in (
        Role.ESTIMATING_ENGINEER_MATERIALS,
        Role.ESTIMATING_ENGINEER_LABOR,
        Role.EXECUTIVE,
    ):
        notify_role(
            role, project_id, "submitted",
            f"Bid submitted — proposals sent for {project['name']}",
        )
    audit(user_id, "project.send_out_complete", "project", project_id,
          {"sent_gcs": sent, "emailed_gcs": emailed, "external_gcs": external,
           "skipped_gcs": skipped})
    return {"stage": "submitted", "sent": sent, "emailed": emailed,
            "external": external, "skipped": skipped}


def _all_project_gc_names(project_id: str) -> list[str]:
    rows = (
        get_supabase()
        .table("project_gcs")
        .select("general_contractors(name)")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    return [r["general_contractors"]["name"] for r in rows if r.get("general_contractors")]


# ── generation ─────────────────────────────────────────────────────────────


def _fmt_optional(v: Decimal | None) -> str | None:
    return format_money(v) if v is not None else None


def build_base_context(
    project: dict,
    draft: dict,
    amounts: dict[str, Decimal | None],
    includes_generator: bool = False,
) -> ProposalContext:
    missing = [
        label
        for label, value in (
            ("address", project.get("address")),
            ("labor time", project.get("labor_time")),
            ("wage type", project.get("wage_type")),
        )
        if not value
    ]
    if missing:
        raise ProposalSendError(
            "Project is missing required proposal fields: "
            + ", ".join(missing)
            + " — set them in the project details.",
            status_code=400,
        )
    # Angle brackets are placeholder syntax in the template; a field containing
    # one would be indistinguishable from an unreplaced token (the output
    # validator hard-fails on any surviving bracket).
    bracketed = [
        label
        for label, value in (
            ("project name", project.get("name")),
            ("project number", project.get("number")),
            ("address", project.get("address")),
        )
        if value and ("<" in str(value) or ">" in str(value))
    ]
    if bracketed:
        raise ProposalSendError(
            "Remove '<' and '>' characters from: " + ", ".join(bracketed),
            status_code=400,
        )
    tz = ZoneInfo(get_settings().display_timezone)
    return ProposalContext(
        project_number=project["number"],
        project_name=project["name"],
        address=project["address"],
        gc_name="",  # per-GC via dataclasses.replace
        date_str=datetime.now(tz).strftime("%m/%d/%Y"),
        # Presets map to their proposal wording; a custom value prints as typed.
        labor_time=LABOR_TIME_TEXT.get(project["labor_time"], project["labor_time"]),
        wage_text=WAGE_TEXT.get(project["wage_type"], project["wage_type"]),
        material_amount=format_money(amounts["material"]),
        # Section rows: None = not on the project; proposal_docx removes the
        # whole pricing-box row before replacement.
        gear_amount=_fmt_optional(amounts["gear"]),
        underground_amount=_fmt_optional(amounts["underground"]),
        low_voltage_amount=_fmt_optional(amounts["low_voltage"]),
        labor_amount=format_money(amounts["labor"]),
        total_amount=format_money(amounts["total"]),
        # The caption only exists while the gear row itself is rendered.
        includes_generator=includes_generator and amounts["gear"] is not None,
        scope_lines=tuple(draft["lines_json"]),
    )


def generate_documents(project_id: str, draft_id: str, user_id: str) -> list[dict]:
    """Render + store one proposal per GC on the project. All-or-nothing: any
    render failure aborts the whole batch so the review UI never shows a
    partial GC↔file mapping. Rows already 'sent' are left untouched.

    Runs at any head in SEND_WINDOW_HEADS: after the bid is submitted this is
    how a late-added GC gets its document, and because sent rows are never
    targets, an already-delivered proposal can never be rewritten by it."""
    from app.routers.pricing import (
        _get_one,
        _materials_rows,
        _verify_originals,
        section_summary,
    )

    sb = get_supabase()
    project = sb.table("projects").select("*").eq("id", project_id).single().execute().data
    if not project:
        raise ProposalSendError("Project not found", status_code=404)

    head = send_window_head(project_id)
    verification = _get_one("verifications", project_id)
    if not verification or not verification.get("committed_at"):
        raise ProposalSendError("Executive must verify/commit pricing first")

    draft = (
        sb.table("proposal_drafts").select("*").eq("id", draft_id).eq("project_id", project_id)
        .single().execute()
    ).data
    if not draft:
        raise ProposalSendError("Proposal draft not found", status_code=404)
    if not draft.get("approved_at") or not draft.get("lines_json"):
        raise ProposalSendError("Proposal lines must be approved first")
    latest = (
        sb.table("proposal_drafts").select("id").eq("project_id", project_id)
        .order("created_at", desc=True).limit(1).execute()
    ).data
    if latest and latest[0]["id"] != draft_id:
        raise ProposalSendError("A newer draft exists — approve and generate from the latest.")

    gcs = _project_gcs(project_id)
    if not gcs:
        raise ProposalSendError("No GC on this project — add one first.", status_code=400)

    existing = (
        sb.table("proposal_sends").select("*").eq("project_id", project_id).execute()
    ).data or []
    by_gc = {r["gc_id"]: r for r in existing}

    if any(r["status"] == "sending" for r in existing):
        raise ProposalSendError(
            "A proposal send is in progress or unresolved — wait or retry it first."
        )

    member_ids = {gc["id"] for gc in gcs}
    # Sent rows are never targets: their document is what the GC already holds
    # and it must stay byte-for-byte reproducible. This is also what makes
    # generating safe after the bid was submitted — a late GC gets a document,
    # everyone already bid keeps theirs untouched.
    targets = [gc for gc in gcs if by_gc.get(gc["id"], {}).get("status") != "sent"]
    if not targets:
        raise ProposalSendError("Every GC's proposal has already been sent.")

    bad_gc_names = [gc["name"] for gc in gcs if "<" in gc["name"] or ">" in gc["name"]]
    if bad_gc_names:
        raise ProposalSendError(
            "GC names cannot contain '<' or '>' — rename: " + ", ".join(bad_gc_names),
            status_code=400,
        )

    # Filename collision check across the batch (two GCs sanitizing identically).
    names = [build_filename(project["number"], gc["name"]) for gc in gcs]
    if len(names) != len(set(names)):
        raise ProposalSendError(
            "Two GCs on this project produce the same proposal filename — "
            "rename one of the GCs before generating.",
            status_code=400,
        )

    amounts = proposal_amounts(_verify_originals(project_id), verification)
    # The generator caption rides on the live section decomposition; the gear
    # row itself must also be rendered (build_base_context enforces that).
    includes_generator = section_summary(_materials_rows(project_id))["gear"][
        "includes_generator"
    ]
    base_ctx = build_base_context(project, draft, amounts, includes_generator)
    template = load_template_bytes()
    digest = lines_hash(list(draft["lines_json"]))
    all_names = _all_project_gc_names(project_id)

    # Render + validate EVERYTHING before touching storage or the DB.
    rendered: list[tuple[dict, str, bytes, dict[str, Decimal | None]]] = []
    for gc in targets:
        gc_amounts = resolve_gc_amounts(amounts, gc)
        ctx = replace(
            base_ctx,
            gc_name=gc["name"],
            material_amount=format_money(gc_amounts["material"]),
            gear_amount=_fmt_optional(gc_amounts["gear"]),
            underground_amount=_fmt_optional(gc_amounts["underground"]),
            low_voltage_amount=_fmt_optional(gc_amounts["low_voltage"]),
            labor_amount=format_money(gc_amounts["labor"]),
            total_amount=format_money(gc_amounts["total"]),
        )
        try:
            docx = render_proposal(template, ctx)
            validate_output(
                docx,
                gc_name=gc["name"],
                scope_lines=ctx.scope_lines,
                other_gc_names=tuple(n for n in all_names if n != gc["name"]),
                amounts=stamp_figures(gc_amounts),
                # Freshly rendered bytes must carry the caption iff the context
                # did, and no trace of the section rows that were removed.
                includes_generator=ctx.includes_generator,
                removed_sections=tuple(
                    key for key in _SECTION_AMOUNT_KEYS if gc_amounts[key] is None
                ),
            )
        except ProposalRenderError as exc:
            raise ProposalSendError(f"Generation failed for {gc['name']}: {exc}", 422) from exc
        rendered.append((gc, build_filename(project["number"], gc["name"]), docx, gc_amounts))

    created: list[dict] = []
    for gc, filename, docx, gc_amounts in rendered:
        path = storage.build_object_path(project_id, "proposal", filename)
        storage.upload_file(path, docx, DOCX_MIME)
        file_row = (
            sb.table("project_files")
            .insert(
                {
                    "project_id": project_id,
                    "category": "proposal",
                    "storage_path": path,
                    "filename": filename,
                    "gc_id": gc["id"],
                    "uploaded_by": user_id,
                    "mime_type": DOCX_MIME,
                    "size_bytes": len(docx),
                    "preview_status": "pending",
                }
            )
            .execute()
        ).data[0]

        prior = by_gc.get(gc["id"])
        fields = {
            "draft_id": draft_id,
            "gc_name": gc["name"],
            # Recipients are picked at send time (gc_contacts); the claim that
            # starts a send writes the actual list here.
            "gc_email": None,
            "file_id": file_row["id"],
            "lines_hash": digest,
            # The figures this document actually carries — send-time staleness
            # check + the per-GC audit record of what we bid them. Absent
            # sections are stamped NULL (their rows are not in the document).
            "material_amount": str(gc_amounts["material"]),
            "gear_amount": (
                str(gc_amounts["gear"]) if gc_amounts["gear"] is not None else None
            ),
            "underground_amount": (
                str(gc_amounts["underground"])
                if gc_amounts["underground"] is not None
                else None
            ),
            "low_voltage_amount": (
                str(gc_amounts["low_voltage"])
                if gc_amounts["low_voltage"] is not None
                else None
            ),
            "labor_amount": str(gc_amounts["labor"]),
            "status": "generated",
            "error": None,
            "sent_at": None,
            "sent_by": None,
            "email_log_id": None,
        }
        if prior:
            row = (
                sb.table("proposal_sends").update(fields).eq("id", prior["id"]).execute()
            ).data[0]
            # The replaced (never-sent) document is now unreachable — clean it up.
            if prior.get("file_id") and prior["file_id"] != file_row["id"]:
                _delete_file_row(prior["file_id"])
        else:
            row = (
                sb.table("proposal_sends")
                .insert({"project_id": project_id, "gc_id": gc["id"], **fields})
                .execute()
            ).data[0]
        audit(user_id, "proposal.generate", "project_file", file_row["id"],
              {"gc_id": gc["id"], "filename": filename, "draft_id": draft_id})
        created.append({**row, "_file": file_row})

    # GCs removed from the project keep their history; never-sent rows are retired.
    for r in existing:
        if r["gc_id"] not in member_ids and r["status"] not in ("sent", "superseded"):
            sb.table("proposal_sends").update({"status": "superseded"}).eq("id", r["id"]).execute()

    audit(user_id, "proposal.generate_docs", "project", project_id,
          {"draft_id": draft_id, "gcs": len(created), "head": head})
    return created


def _delete_file_row(file_id: str) -> None:
    sb = get_supabase()
    rec = (
        sb.table("project_files").select("storage_path, preview_path").eq("id", file_id)
        .single().execute()
    ).data
    if not rec:
        return
    for path in (rec.get("storage_path"), rec.get("preview_path")):
        if not path:
            continue
        try:
            storage.delete_file(path)
        except Exception:  # noqa: BLE001 — orphaned object is acceptable, broken row is not
            logger.warning("could not delete storage object %s for replaced proposal", path)
    sb.table("project_files").delete().eq("id", file_id).execute()


# ── transmission history ───────────────────────────────────────────────────


def record_send_event(
    row: dict,
    *,
    kind: str,
    via: str,
    recipients: list[str] | None,
    email_log_id: str | None,
    user_id: str,
    event_id: str | None = None,
) -> dict | None:
    """Append (or complete) one transmission of this GC's bid.

    proposal_sends holds ONE row per GC — whether they were bid and with which
    document — so it has nowhere to put a second delivery. This is where each
    actual transmission lands: the first one alongside the send/mark that
    stamped the row, and every re-send after it. `event_id` completes a row
    previously claimed at 'sending' (the re-send lock); without it a finished
    row is inserted outright. Never raises: history must not fail a send that
    already left the building."""
    sb = get_supabase()
    fields = {
        "kind": kind,
        "via": via,
        "file_id": row.get("file_id"),
        "recipients": join_recipients(recipients) if recipients else None,
        "status": "sent",
        "error": None,
        "email_log_id": email_log_id,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "sent_by": user_id,
    }
    try:
        if event_id:
            return (
                sb.table("proposal_send_events").update(fields).eq("id", event_id)
                .execute()
            ).data[0]
        return (
            sb.table("proposal_send_events")
            .insert(
                {
                    "proposal_send_id": row["id"],
                    "project_id": row["project_id"],
                    "gc_id": row["gc_id"],
                    **fields,
                }
            )
            .execute()
        ).data[0]
    except Exception:  # noqa: BLE001 — the mail is already sent; log and move on
        logger.exception("could not record proposal send event for %s", row.get("id"))
        return None


def send_events_by_proposal(project_id: str) -> dict[str, list[dict]]:
    """Every transmission on the project, grouped by proposal_sends id and
    ordered oldest first — the per-GC delivery history the Send Out panel
    renders under each row."""
    rows = (
        get_supabase()
        .table("proposal_send_events")
        .select("*")
        .eq("project_id", project_id)
        .order("created_at")
        .execute()
    ).data or []
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["proposal_send_id"], []).append(r)
    return out


# ── send ───────────────────────────────────────────────────────────────────


def assert_send_isolation(
    *,
    row: dict,
    file_row: dict,
    docx_bytes: bytes,
    recipients: list[str],
    live_gc: dict,
    project: dict,
    draft: dict | None,
    other_gc_names: tuple[str, ...],
    expected_amounts: dict[str, Decimal] | None = None,
    includes_generator: bool = False,
) -> None:
    """Defense-in-depth before the ONE network call that can leak a document.
    Pure: raises ProposalSendError with the reason; callers pass live rows
    (`row` is the freshly claimed proposal_sends row, so its gc_email is the
    recipient list this very send wrote). `includes_generator` is the live
    project-level flag; the gear stamp gates whether the caption row can exist
    in these bytes at all (section_validation_kwargs)."""
    gc_id = row["gc_id"]
    if not live_gc or live_gc.get("id") != gc_id:
        raise ProposalSendError("GC is no longer on this project — regenerate.")
    if file_row.get("gc_id") != gc_id:
        raise ProposalSendError("ISOLATION: file does not belong to this GC — regenerate.")
    if file_row.get("project_id") != row["project_id"]:
        raise ProposalSendError("ISOLATION: file belongs to a different project.")
    if file_row.get("category") != "proposal":
        raise ProposalSendError("ISOLATION: file is not a generated proposal.")

    if not recipients:
        raise ProposalSendError(
            f"{row['gc_name']} has no contact with an email on file (or none was selected).",
            status_code=400,
        )
    live_emails = {c["email"] for c in (live_gc.get("contacts") or []) if c.get("email")}
    if not set(recipients) <= live_emails:
        raise ProposalSendError(
            f"{row['gc_name']}'s recipients are no longer on file — reopen the send dialog."
        )
    if row.get("gc_email") != join_recipients(recipients):
        raise ProposalSendError(
            "ISOLATION: recipient list does not match the claimed send row — retry."
        )

    expected = build_filename(project["number"], row["gc_name"])
    if file_row.get("filename") != expected:
        raise ProposalSendError("ISOLATION: filename does not match this GC — regenerate.")

    if draft is None or draft.get("id") != row.get("draft_id"):
        raise ProposalSendError("Proposal draft changed — regenerate documents.")
    if not draft.get("approved_at"):
        raise ProposalSendError("Proposal lines are no longer approved — regenerate.")
    if lines_hash(list(draft.get("lines_json") or [])) != row.get("lines_hash"):
        raise ProposalSendError(
            "Proposal lines changed since documents were generated — regenerate."
        )

    # Per-GC amounts: the document must still say what the live settings say
    # (override edited after generation → stale doc), and the bytes must carry
    # the stamped figures. All five pairs are compared NULL-safe, so a section
    # appearing (or a stamp missing one, e.g. a pre-sections document on a
    # sectioned project) fails closed exactly like a moved number. Rows stamped
    # before per-GC amounts existed have no stamp to prove; they keep the
    # pre-feature behavior.
    stamped = stamped_amounts(row)
    if stamped is not None and expected_amounts is not None:
        if any(stamped.get(k) != expected_amounts.get(k) for k in AMOUNT_KEYS):
            raise ProposalSendError(
                "Amounts changed since this document was generated — regenerate documents."
            )

    validate_output(
        docx_bytes,
        gc_name=row["gc_name"],
        scope_lines=tuple(draft["lines_json"]),
        other_gc_names=other_gc_names,
        amounts=stamp_figures(stamped) if stamped is not None else (),
        **section_validation_kwargs(stamped, includes_generator),
    )


def _reclaim_stuck_sending(row: dict, subject: str) -> dict:
    """A row stuck at 'sending' means we crashed mid-send. If email_log proves
    the mail went out, mark it sent; otherwise release it as failed so the PA
    can retry. Never resend on guesswork."""
    sb = get_supabase()
    updated_at = row.get("updated_at") or ""
    try:
        stamp = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        stamp = datetime.now(timezone.utc)
    if datetime.now(timezone.utc) - stamp < timedelta(minutes=SENDING_STALE_MINUTES):
        return row  # genuinely in flight — leave it alone

    logs = (
        sb.table("email_log")
        .select("id, status, to_addrs, subject, created_at")
        .eq("project_id", row["project_id"])
        .eq("subject", subject)
        .eq("status", "sent")
        .order("created_at", desc=True)
        .limit(20)
        .execute()
    ).data or []
    # gc_email is the exact recipient-list string the claim wrote; send_mail
    # logs to_addrs with the same join, so equality (not substring) is the proof.
    proof = next(
        (log for log in logs if row.get("gc_email") and log["to_addrs"] == row["gc_email"]), None
    )
    if proof:
        fields = {"status": "sent", "sent_at": proof["created_at"], "email_log_id": proof["id"],
                  "error": None}
    else:
        fields = {"status": "failed",
                  "error": f"{OUTCOME_UNKNOWN_PREFIX} — send interrupted; verify in Sent Items "
                           "before retrying."}
    return sb.table("proposal_sends").update(fields).eq("id", row["id"]).execute().data[0]


def _prepare_extra_attachments(
    sb, project_id: str, file_ids: list[str] | None
) -> list[tuple[str, bytes]]:
    """Download the PA's Modify Files picks as (filename, bytes) send_mail
    tuples, shared by every email in the dispatch.

    Guards, in order: every id must be a project file here (unsent estimator
    drafts excluded, same rule as RFQ attachments); generated proposal
    documents are refused outright (attaching GC A's proposal to GC B's email
    is exactly the cross-GC leak this module exists to prevent); editable
    Office files are converted to immutable PDFs like the proposal itself.
    All failures raise ProposalSendError BEFORE any claim is taken, so a bad
    pick never strands a row at 'sending'.

    These are shared project documents (specs, addenda, bond forms) attached
    identically for every GC, so the per-GC isolation scans deliberately do
    not run over them.
    """
    ids = list(dict.fromkeys(file_ids or []))
    if not ids:
        return []
    from app.services.estimator_rounds import exclude_unsent

    q = (
        sb.table("project_files")
        .select("id, filename, storage_path, category")
        .eq("project_id", project_id)
        .in_("id", ids)
    )
    rows = exclude_unsent(q).execute().data or []
    found = {r["id"]: r for r in rows}
    missing = [i for i in ids if i not in found]
    if missing:
        raise ProposalSendError(
            "Extra attachment not available in this project (an estimator file "
            f"must be sent to the team before it can be attached): {missing[0]}",
            status_code=400,
        )
    out: list[tuple[str, bytes]] = []
    for i in ids:
        r = found[i]
        if r["category"] == "proposal":
            raise ProposalSendError(
                "A generated proposal document cannot be added as an extra "
                "attachment. Remove it from the Modify Files list.",
                status_code=400,
            )
        content = storage.download_file(r["storage_path"])
        filename = r["filename"]
        if office_preview.is_office_file(filename):
            try:
                content = office_preview.convert_for_send(content, filename)
            except office_preview.ConversionError as exc:
                raise ProposalSendError(
                    f"Could not convert {filename} to PDF. Retry, or remove it "
                    f"from the Modify Files list. ({exc})"
                ) from exc
            filename = office_preview.pdf_filename(filename)
        out.append((filename, content))
    return out


def send_proposals(
    project_id: str,
    user_id: str,
    proposal_ids: list[str],
    email_body: str | None = None,
    force: bool = False,
    contacts: dict[str, list[str]] | None = None,
    extra_file_ids: list[str] | None = None,
) -> dict:
    sb = get_supabase()
    project = sb.table("projects").select("*").eq("id", project_id).single().execute().data
    if not project:
        raise ProposalSendError("Project not found", status_code=404)

    head = send_window_head(project_id)

    rows = (
        sb.table("proposal_sends").select("*").eq("project_id", project_id)
        .in_("id", proposal_ids).execute()
    ).data or []
    if len(rows) != len(set(proposal_ids)):
        raise ProposalSendError(
            "Proposals were regenerated since you opened the confirm dialog — review again.",
            status_code=404,
        )

    subject, default_body = build_cover_email(project)
    body_template = email_body or default_body
    # Validated/downloaded once, before any row is claimed: a bad pick fails
    # the whole request cleanly instead of stranding rows at 'sending'.
    extra_attachments = _prepare_extra_attachments(sb, project_id, extra_file_ids)
    extras_bytes = sum(len(b) for _, b in extra_attachments)
    live_gcs = {gc["id"]: gc for gc in _project_gcs(project_id)}
    all_names = _all_project_gc_names(project_id)
    # Live default amounts for the staleness check — pricing is committed by
    # the time send_out is reachable, but stay defensive (None = skip check).
    from app.routers.pricing import (
        _get_one,
        _materials_rows,
        _verify_originals,
        section_summary,
    )

    # The live generator flag (presence is monotonic, so it can only have grown
    # since generation; a caption the document now lacks fails closed below).
    includes_generator = section_summary(_materials_rows(project_id))["gear"][
        "includes_generator"
    ]
    verification = _get_one("verifications", project_id)
    default_amounts = (
        proposal_amounts(_verify_originals(project_id), verification)
        if verification and verification.get("committed_at")
        else None
    )
    drafts = {
        d["id"]: d
        for d in (
            sb.table("proposal_drafts").select("*").eq("project_id", project_id).execute()
        ).data
        or []
    }
    latest_draft = max(drafts.values(), key=lambda d: d["created_at"], default=None)

    results = []
    for row in rows:
        if row["status"] == "sent":
            results.append(_result(row, "skipped", None))
            continue
        if row["status"] == "superseded":
            results.append(_result(row, "skipped", "GC is no longer on this project"))
            continue
        if row["status"] == "sending":
            row = _reclaim_stuck_sending(row, subject)
            if row["status"] in ("sent", "sending"):
                results.append(_result(row, row["status"], row.get("error")))
                continue
        if (
            row["status"] == "failed"
            and (row.get("error") or "").startswith(OUTCOME_UNKNOWN_PREFIX)
            and not force
        ):
            results.append(_result(row, "failed", row["error"]))
            continue

        live_gc = live_gcs.get(row["gc_id"]) or {}
        recipients: list[str] = []
        resolve_error: ProposalSendError | None = None
        try:
            recipients = resolve_recipients(live_gc, (contacts or {}).get(row["id"]))
        except ProposalSendError as exc:
            resolve_error = exc

        # The claim stamps the exact recipient list onto the row BEFORE the
        # network call — if we crash mid-send, _reclaim_stuck_sending can match
        # it against email_log.to_addrs to prove (or disprove) delivery.
        claimed = (
            sb.table("proposal_sends")
            .update({"status": "sending", "gc_email": join_recipients(recipients) or None})
            .eq("id", row["id"])
            .in_("status", ["generated", "failed"])
            .execute()
        ).data
        if not claimed:
            results.append(_result(row, "skipped", "claimed by another request"))
            continue
        row = claimed[0]

        try:
            if resolve_error is not None:
                raise resolve_error
            file_row = None
            if row.get("file_id"):
                file_row = (
                    sb.table("project_files").select("*").eq("id", row["file_id"])
                    .single().execute()
                ).data
            if not file_row:
                raise ProposalSendError("Generated document is missing — regenerate.")
            docx_bytes = storage.download_file(file_row["storage_path"])

            draft = drafts.get(row.get("draft_id"))
            if latest_draft is not None and draft is not None and draft["id"] != latest_draft["id"]:
                raise ProposalSendError("A newer draft exists — regenerate documents.")
            assert_send_isolation(
                row=row,
                file_row=file_row,
                docx_bytes=docx_bytes,
                recipients=recipients,
                live_gc=live_gc,
                project=project,
                draft=draft,
                other_gc_names=tuple(n for n in all_names if n != row["gc_name"]),
                expected_amounts=(
                    resolve_gc_amounts(default_amounts, live_gc)
                    if default_amounts is not None and live_gc
                    else None
                ),
                includes_generator=includes_generator,
            )

            # Convert the EXACT validated docx bytes to an immutable PDF so the
            # GC cannot alter our numbers/scope. Done after isolation passes and
            # BEFORE the network send: a conversion failure means nothing went
            # out, so it is cleanly retryable (never "outcome unknown"). There is
            # deliberately NO fallback to the malleable docx.
            try:
                pdf_bytes = office_preview.convert_for_send(docx_bytes, file_row["filename"])
            except office_preview.ConversionError as exc:
                raise ProposalSendError(
                    f"Could not convert the proposal to PDF — retry. ({exc})"
                ) from exc
            # send_mail inlines attachments (no upload-session path), so the
            # proposal and every extra file must fit the limit together.
            if len(pdf_bytes) + extras_bytes >= graph_email._INLINE_ATTACHMENT_LIMIT:
                raise ProposalSendError(
                    "Proposal PDF plus the extra attachments are too large to "
                    "email. Remove some files in Modify Files."
                    if extra_attachments
                    else "Proposal PDF is too large to email — contact support."
                )
            # Belt-and-suspenders leak re-scan on the rendered PDF itself: the
            # docx isolation scan can't see content that only the PDF renders.
            _pdf_stamp = stamped_amounts(row)
            validate_pdf_isolation(
                office_preview.extract_pdf_text(pdf_bytes),
                gc_name=row["gc_name"],
                other_gc_names=tuple(n for n in all_names if n != row["gc_name"]),
                amounts=stamp_figures(_pdf_stamp) if _pdf_stamp is not None else (),
                **section_validation_kwargs(_pdf_stamp, includes_generator),
            )
            pdf_name = office_preview.pdf_filename(file_row["filename"])

            body = body_template.replace(GC_NAME_TOKEN, row["gc_name"])
            cc = cc_recipients(recipients)
            try:
                log = graph_email.send_mail(
                    to=recipients,
                    cc=cc,
                    subject=subject,
                    body_html=email_branding.render_proposal_email(body),
                    attachments=[(pdf_name, pdf_bytes), *extra_attachments],
                    inline_images=[
                        (
                            email_branding.LOGO_CONTENT_ID,
                            email_branding.LOGO_FILENAME,
                            email_branding.logo_bytes(),
                            "image/jpeg",
                        )
                    ],
                    project_id=project_id,
                    sent_by=user_id,
                )
            except _OUTCOME_UNKNOWN_EXC as exc:
                raise ProposalSendError(
                    f"{OUTCOME_UNKNOWN_PREFIX} ({type(exc).__name__}) — verify in Sent Items "
                    "before retrying."
                ) from exc

            row = (
                sb.table("proposal_sends")
                .update(
                    {
                        "status": "sent",
                        "sent_at": datetime.now(timezone.utc).isoformat(),
                        "sent_by": user_id,
                        "email_log_id": log["id"],
                        "error": None,
                    }
                )
                .eq("id", row["id"])
                .execute()
            ).data[0]
            record_send_event(
                row, kind="initial", via="email", recipients=recipients,
                email_log_id=log["id"], user_id=user_id,
            )
            # email_log.to_addrs is To-only by contract, so the audit trail is
            # where the CC is recorded per send.
            audit(user_id, "proposal.send", "proposal_send", row["id"],
                  {"gc_id": row["gc_id"], "to": join_recipients(recipients),
                   "cc": join_recipients(cc) or None,
                   "file_id": row["file_id"],
                   "extra_attachments": [n for n, _ in extra_attachments] or None})
            results.append(_result(row, "sent", None))
        except Exception as exc:  # noqa: BLE001 — isolate failures per GC
            message = str(exc)[:500]
            sb.table("proposal_sends").update({"status": "failed", "error": message}).eq(
                "id", row["id"]
            ).execute()
            audit(user_id, "proposal.send_failed", "proposal_send", row["id"],
                  {"gc_id": row["gc_id"], "error": message})
            logger.exception("proposal send failed for gc %s", row["gc_id"])
            results.append(_result(row, "failed", message))
        time.sleep(1)  # Exchange throttling courtesy (rfq_sending precedent)

    # The stage never flips here — sending is per-GC and open-ended; the PA
    # ends the stage explicitly via complete_send_out ("Done sending").
    # A successful send clears any prior failure notice (dismiss before
    # re-notifying, so a fresh failure for the remaining GCs survives).
    if any(r["status"] == "sent" for r in results):
        dismiss_notifications(project_id=project_id, types=["proposal_send_failed"])
    failed = [r for r in results if r["status"] == "failed"]
    if failed:
        from app.core.roles import Role

        notify_role(Role.ESTIMATING_ADMIN, project_id, "proposal_send_failed",
                    f"{len(failed)} proposal send(s) failed — retry from the Send Out panel")

    audit(user_id, "project.send_out", "project", project_id,
          {"sent": sum(1 for r in results if r["status"] == "sent"),
           "failed": sum(1 for r in results if r["status"] == "failed"),
           "head": head})
    return {"results": results, "stage": project["current_stage"]}


# ── re-send (same document, again) ─────────────────────────────────────────


def assert_resend_isolation(
    *,
    row: dict,
    file_row: dict,
    docx_bytes: bytes,
    recipients: list[str],
    live_gc: dict,
    project: dict,
    scope_lines: tuple[str, ...],
    other_gc_names: tuple[str, ...],
    includes_generator: bool = False,
) -> None:
    """Isolation gate for re-emailing a document that already went out.

    Deliberately NOT assert_send_isolation. That function also enforces
    freshness — the draft must be the latest, the lines hash must match the live
    draft, the stamped figures must equal today's per-GC amounts — because a
    first send must never ship a stale document. A re-send is the opposite case:
    the document is history, drift since it was delivered is expected, and
    re-validating against live pricing would block exactly the recovery this
    exists for. What still holds absolutely is the isolation contract — these
    bytes belong to this GC, name no other GC, and go only to this GC's live
    contacts — so every one of those checks is kept."""
    gc_id = row["gc_id"]
    if not live_gc or live_gc.get("id") != gc_id:
        raise ProposalSendError("GC is no longer on this project.")
    if file_row.get("gc_id") != gc_id:
        raise ProposalSendError("ISOLATION: file does not belong to this GC.")
    if file_row.get("project_id") != row["project_id"]:
        raise ProposalSendError("ISOLATION: file belongs to a different project.")
    if file_row.get("category") != "proposal":
        raise ProposalSendError("ISOLATION: file is not a generated proposal.")

    if not recipients:
        raise ProposalSendError(
            f"{row['gc_name']} has no contact with an email on file (or none was selected).",
            status_code=400,
        )
    live_emails = {c["email"] for c in (live_gc.get("contacts") or []) if c.get("email")}
    if not set(recipients) <= live_emails:
        raise ProposalSendError(
            f"{row['gc_name']}'s recipients are no longer on file — reopen the dialog."
        )

    expected = build_filename(project["number"], row["gc_name"])
    if file_row.get("filename") != expected:
        raise ProposalSendError("ISOLATION: filename does not match this GC.")

    stamped = stamped_amounts(row)
    validate_output(
        docx_bytes,
        gc_name=row["gc_name"],
        scope_lines=scope_lines,
        other_gc_names=other_gc_names,
        amounts=stamp_figures(stamped) if stamped is not None else (),
        **section_validation_kwargs(stamped, includes_generator),
    )


def _reclaim_stuck_event(event: dict, subject: str) -> dict:
    """A re-send event stuck at 'sending' means we crashed mid-send. It holds
    the partial-unique lock, so it must be resolved or no re-send to that GC
    could ever run again. Same proof rule as _reclaim_stuck_sending: email_log
    equality on the exact recipient string, never a guess."""
    sb = get_supabase()
    stamp_text = event.get("updated_at") or event.get("created_at") or ""
    try:
        stamp = datetime.fromisoformat(stamp_text.replace("Z", "+00:00"))
    except ValueError:
        stamp = datetime.now(timezone.utc)
    if datetime.now(timezone.utc) - stamp < timedelta(minutes=SENDING_STALE_MINUTES):
        return event  # genuinely in flight

    logs = (
        sb.table("email_log")
        .select("id, status, to_addrs, subject, created_at")
        .eq("project_id", event["project_id"])
        .eq("subject", subject)
        .eq("status", "sent")
        .order("created_at", desc=True)
        .limit(20)
        .execute()
    ).data or []
    proof = next(
        (log for log in logs if event.get("recipients") and log["to_addrs"] == event["recipients"]),
        None,
    )
    if proof:
        fields = {"status": "sent", "sent_at": proof["created_at"],
                  "email_log_id": proof["id"], "error": None}
    else:
        fields = {"status": "failed",
                  "error": f"{OUTCOME_UNKNOWN_PREFIX} — re-send interrupted; verify in Sent "
                           "Items before retrying."}
    return sb.table("proposal_send_events").update(fields).eq("id", event["id"]).execute().data[0]


def resend_proposals(
    project_id: str,
    user_id: str,
    proposal_ids: list[str],
    email_body: str | None = None,
    contacts: dict[str, list[str]] | None = None,
    extra_file_ids: list[str] | None = None,
) -> dict:
    """Email an already-sent proposal to its GC again — the bounced-address /
    "the GC never got it" recovery path.

    The attachment is the document already on file, converted to PDF exactly as
    the original send did. Nothing is regenerated and nothing on proposal_sends
    moves: sent_at / sent_by / email_log_id keep pointing at the submission,
    because a re-send is another copy of the same bid, not a new one. The
    delivery lands in proposal_send_events, whose 'sending' row is also the
    per-GC lock that stops two re-sends racing."""
    sb = get_supabase()
    project = sb.table("projects").select("*").eq("id", project_id).single().execute().data
    if not project:
        raise ProposalSendError("Project not found", status_code=404)

    head = send_window_head(project_id)

    rows = (
        sb.table("proposal_sends").select("*").eq("project_id", project_id)
        .in_("id", proposal_ids).execute()
    ).data or []
    if len(rows) != len(set(proposal_ids)):
        raise ProposalSendError(
            "Proposals changed since you opened the dialog — review again.", status_code=404
        )

    subject, default_body = build_cover_email(project)
    body_template = email_body or default_body
    # Same pre-claim validation as a first send; the extras a re-send carries
    # are today's picks, not a replay of the original send's extras.
    extra_attachments = _prepare_extra_attachments(sb, project_id, extra_file_ids)
    extras_bytes = sum(len(b) for _, b in extra_attachments)
    live_gcs = {gc["id"]: gc for gc in _project_gcs(project_id)}
    all_names = _all_project_gc_names(project_id)
    drafts = {
        d["id"]: d
        for d in (
            sb.table("proposal_drafts").select("*").eq("project_id", project_id).execute()
        ).data
        or []
    }
    from app.routers.pricing import _materials_rows, section_summary

    includes_generator = section_summary(_materials_rows(project_id))["gear"][
        "includes_generator"
    ]
    events = send_events_by_proposal(project_id)

    results = []
    for row in rows:
        if row["status"] != "sent":
            results.append(
                _result(row, "skipped", "This proposal has not been sent yet — send it first.")
            )
            continue

        inflight = next(
            (e for e in events.get(row["id"], []) if e["status"] == "sending"), None
        )
        if inflight is not None:
            inflight = _reclaim_stuck_event(inflight, subject)
            if inflight["status"] == "sending":
                results.append(_result(row, "skipped", "a re-send is already in progress"))
                continue

        live_gc = live_gcs.get(row["gc_id"]) or {}
        try:
            recipients = resolve_recipients(live_gc, (contacts or {}).get(row["id"]))
        except ProposalSendError as exc:
            results.append(_result(row, "failed", str(exc)))
            continue
        if not recipients:
            results.append(
                _result(row, "failed",
                        f"{row['gc_name']} has no contact with an email on file.")
            )
            continue

        # Claim: the 'sending' event row IS the lock (partial unique index).
        try:
            event = (
                sb.table("proposal_send_events")
                .insert(
                    {
                        "proposal_send_id": row["id"],
                        "project_id": project_id,
                        "gc_id": row["gc_id"],
                        "kind": "resend",
                        "via": "email",
                        "file_id": row.get("file_id"),
                        "recipients": join_recipients(recipients),
                        "status": "sending",
                        "sent_by": user_id,
                    }
                )
                .execute()
            ).data[0]
        except Exception:  # noqa: BLE001 — the unique index rejecting a racer
            logger.exception("could not claim re-send for proposal %s", row["id"])
            results.append(_result(row, "skipped", "a re-send is already in progress"))
            continue

        try:
            file_row = None
            if row.get("file_id"):
                file_row = (
                    sb.table("project_files").select("*").eq("id", row["file_id"])
                    .single().execute()
                ).data
            if not file_row:
                raise ProposalSendError(
                    "The document that was sent is no longer on file — it cannot be re-sent."
                )
            docx_bytes = storage.download_file(file_row["storage_path"])
            draft = drafts.get(row.get("draft_id")) or {}
            assert_resend_isolation(
                row=row,
                file_row=file_row,
                docx_bytes=docx_bytes,
                recipients=recipients,
                live_gc=live_gc,
                project=project,
                # The lines this document was built from, not today's draft.
                scope_lines=tuple(draft.get("lines_json") or ()),
                other_gc_names=tuple(n for n in all_names if n != row["gc_name"]),
                includes_generator=includes_generator,
            )

            try:
                pdf_bytes = office_preview.convert_for_send(docx_bytes, file_row["filename"])
            except office_preview.ConversionError as exc:
                raise ProposalSendError(
                    f"Could not convert the proposal to PDF — retry. ({exc})"
                ) from exc
            if len(pdf_bytes) + extras_bytes >= graph_email._INLINE_ATTACHMENT_LIMIT:
                raise ProposalSendError(
                    "Proposal PDF plus the extra attachments are too large to "
                    "email. Remove some files in Modify Files."
                    if extra_attachments
                    else "Proposal PDF is too large to email — contact support."
                )
            _pdf_stamp = stamped_amounts(row)
            validate_pdf_isolation(
                office_preview.extract_pdf_text(pdf_bytes),
                gc_name=row["gc_name"],
                other_gc_names=tuple(n for n in all_names if n != row["gc_name"]),
                amounts=stamp_figures(_pdf_stamp) if _pdf_stamp is not None else (),
                **section_validation_kwargs(_pdf_stamp, includes_generator),
            )

            body = body_template.replace(GC_NAME_TOKEN, row["gc_name"])
            cc = cc_recipients(recipients)
            try:
                log = graph_email.send_mail(
                    to=recipients,
                    cc=cc,
                    subject=subject,
                    body_html=email_branding.render_proposal_email(body),
                    attachments=[
                        (office_preview.pdf_filename(file_row["filename"]), pdf_bytes),
                        *extra_attachments,
                    ],
                    inline_images=[
                        (
                            email_branding.LOGO_CONTENT_ID,
                            email_branding.LOGO_FILENAME,
                            email_branding.logo_bytes(),
                            "image/jpeg",
                        )
                    ],
                    project_id=project_id,
                    sent_by=user_id,
                )
            except _OUTCOME_UNKNOWN_EXC as exc:
                raise ProposalSendError(
                    f"{OUTCOME_UNKNOWN_PREFIX} ({type(exc).__name__}) — verify in Sent Items "
                    "before re-sending again."
                ) from exc

            record_send_event(
                row, kind="resend", via="email", recipients=recipients,
                email_log_id=log["id"], user_id=user_id, event_id=event["id"],
            )
            audit(user_id, "proposal.resend", "proposal_send", row["id"],
                  {"gc_id": row["gc_id"], "to": join_recipients(recipients),
                   "cc": join_recipients(cc) or None, "file_id": row["file_id"],
                   "event_id": event["id"],
                   "extra_attachments": [n for n, _ in extra_attachments] or None})
            results.append(_result(row, "sent", None))
        except Exception as exc:  # noqa: BLE001 — isolate failures per GC
            message = str(exc)[:500]
            # Only the event fails. proposal_sends stays 'sent': the bid was
            # submitted and a failed second copy does not undo that.
            sb.table("proposal_send_events").update(
                {"status": "failed", "error": message}
            ).eq("id", event["id"]).execute()
            audit(user_id, "proposal.resend_failed", "proposal_send", row["id"],
                  {"gc_id": row["gc_id"], "error": message})
            logger.exception("proposal re-send failed for gc %s", row["gc_id"])
            results.append(_result(row, "failed", message))
        time.sleep(1)  # Exchange throttling courtesy

    # Names, not just a count: this audit row is the activity-feed entry that
    # tells someone reading the project later who was sent the bid a second time.
    resent = sorted(r["gc_name"] for r in results if r["status"] == "sent")
    audit(user_id, "project.proposal_resend", "project", project_id,
          {"resent_gcs": resent, "resent": len(resent),
           "failed": sum(1 for r in results if r["status"] == "failed"),
           "head": head})
    return {"results": results, "stage": project["current_stage"]}


# ── mark as submitted (third-party application, no email) ──────────────────


def assert_mark_ready(
    *,
    row: dict,
    draft: dict | None,
    latest_draft_id: str | None,
    expected_amounts: dict[str, Decimal] | None,
) -> None:
    """Staleness gate before recording an external submission. Nothing is
    emailed here, so no byte-level isolation — but a row whose document no
    longer matches the live draft/amounts would make the 'what we bid them'
    record a lie, so it fails closed exactly like a send would."""
    if not row.get("file_id"):
        raise ProposalSendError("Generated document is missing — regenerate.")
    if draft is None or draft.get("id") != row.get("draft_id"):
        raise ProposalSendError("Proposal draft changed — regenerate documents.")
    if latest_draft_id is not None and draft["id"] != latest_draft_id:
        raise ProposalSendError("A newer draft exists — regenerate documents.")
    if not draft.get("approved_at"):
        raise ProposalSendError("Proposal lines are no longer approved — regenerate.")
    if lines_hash(list(draft.get("lines_json") or [])) != row.get("lines_hash"):
        raise ProposalSendError(
            "Proposal lines changed since documents were generated — regenerate."
        )
    stamped = stamped_amounts(row)
    if stamped is not None and expected_amounts is not None:
        if any(stamped.get(k) != expected_amounts.get(k) for k in AMOUNT_KEYS):
            raise ProposalSendError(
                "Amounts changed since this document was generated — regenerate documents."
            )


def mark_submitted(project_id: str, user_id: str, proposal_ids: list[str]) -> dict:
    """Record proposals as submitted WITHOUT emailing anyone — the bid went out
    through a third-party application (GC portal etc.). Reaches the same
    terminal state as a send (status 'sent', sent_at/sent_by stamped) so "Done
    sending", amount locks, and the outcome grid all treat it as a submitted
    bid — but sent_via='external' with no email_log row is the durable record
    that we never emailed it."""
    sb = get_supabase()
    project = (
        sb.table("projects").select("id, current_stage").eq("id", project_id)
        .single().execute()
    ).data
    if not project:
        raise ProposalSendError("Project not found", status_code=404)

    head = send_window_head(project_id)

    rows = (
        sb.table("proposal_sends").select("*").eq("project_id", project_id)
        .in_("id", proposal_ids).execute()
    ).data or []
    if len(rows) != len(set(proposal_ids)):
        raise ProposalSendError(
            "Proposals were regenerated since you opened the confirm dialog — review again.",
            status_code=404,
        )

    drafts = {
        d["id"]: d
        for d in (
            sb.table("proposal_drafts").select("*").eq("project_id", project_id).execute()
        ).data
        or []
    }
    latest_draft = max(drafts.values(), key=lambda d: d["created_at"], default=None)
    live_gcs = {gc["id"]: gc for gc in _project_gcs(project_id)}
    from app.routers.pricing import _get_one, _verify_originals

    verification = _get_one("verifications", project_id)
    default_amounts = (
        proposal_amounts(_verify_originals(project_id), verification)
        if verification and verification.get("committed_at")
        else None
    )

    results = []
    for row in rows:
        if row["status"] == "sent":
            results.append(_result(row, "skipped", None))
            continue
        if row["status"] == "superseded":
            results.append(_result(row, "skipped", "GC is no longer on this project"))
            continue
        if row["status"] == "sending":
            results.append(_result(row, "skipped", "an email send is in progress for this GC"))
            continue
        try:
            live_gc = live_gcs.get(row["gc_id"])
            assert_mark_ready(
                row=row,
                draft=drafts.get(row.get("draft_id")),
                latest_draft_id=latest_draft["id"] if latest_draft else None,
                expected_amounts=(
                    resolve_gc_amounts(default_amounts, live_gc)
                    if default_amounts is not None and live_gc
                    else None
                ),
            )
        except ProposalSendError as exc:
            results.append(_result(row, "failed", str(exc)))
            continue
        claimed = (
            sb.table("proposal_sends")
            .update(
                {
                    "status": "sent",
                    "sent_via": "external",
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                    "sent_by": user_id,
                    # No recipients and no email — their absence IS the record.
                    "gc_email": None,
                    "email_log_id": None,
                    "error": None,
                }
            )
            .eq("id", row["id"])
            .in_("status", ["generated", "failed"])
            .execute()
        ).data
        if not claimed:
            results.append(_result(row, "skipped", "claimed by another request"))
            continue
        row = claimed[0]
        record_send_event(
            row, kind="initial", via="external", recipients=None,
            email_log_id=None, user_id=user_id,
        )
        audit(user_id, "proposal.mark_submitted", "proposal_send", row["id"],
              {"gc_id": row["gc_id"], "file_id": row["file_id"]})
        results.append(_result(row, "marked", None))

    # A row previously failed-by-email is resolved once it's marked submitted.
    if any(r["status"] == "marked" for r in results):
        dismiss_notifications(project_id=project_id, types=["proposal_send_failed"])

    audit(user_id, "project.mark_submitted", "project", project_id,
          {"marked": sum(1 for r in results if r["status"] == "marked"),
           "failed": sum(1 for r in results if r["status"] == "failed"),
           "head": head})
    return {"results": results, "stage": project["current_stage"]}


def _result(row: dict, status: str, error: str | None) -> dict:
    return {
        "proposal_id": row["id"],
        "gc_id": row["gc_id"],
        "gc_name": row["gc_name"],
        "status": status,
        "error": error,
    }

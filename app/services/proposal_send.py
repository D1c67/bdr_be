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


def proposal_amounts(originals: dict, verification: dict) -> dict[str, Decimal]:
    """Material/Labor/TOTAL for the green table. Committed overrides win per
    key, falling back to the upstream figure (same semantics the verify step
    and pricing summary use)."""
    from app.routers.pricing import VERIFY_NUMBERS, _num

    final: dict[str, Decimal] = {}
    for key in VERIFY_NUMBERS:
        v = _num(verification, key)
        if v is None:
            v = originals.get(key)
        final[key] = v if v is not None else Decimal(0)
    material = final["materials_amount"] + final["materials_markup_amount"]
    labor = final["labor_amount"] + final["labor_markup_amount"]
    return {"material": material, "labor": labor, "total": material + labor}


def resolve_gc_amounts(defaults: dict[str, Decimal], gc: dict) -> dict[str, Decimal]:
    """One GC's Material/Labor/TOTAL: G3 sometimes bids different numbers to
    different GCs, so a per-GC override (project_gcs) wins per figure over the
    committed-pricing default. The total always re-sums — never stored."""
    material = gc.get("material_override")
    labor = gc.get("labor_override")
    if material is None:
        material = defaults["material"]
    if labor is None:
        labor = defaults["labor"]
    return {"material": material, "labor": labor, "total": material + labor}


def stamped_amounts(row: dict) -> tuple[Decimal, Decimal] | None:
    """(material, labor) rendered into this row's document at generation time;
    None for rows generated before per-GC amounts existed (nothing to check)."""
    material, labor = row.get("material_amount"), row.get("labor_amount")
    if material is None or labor is None:
        return None
    return Decimal(str(material)), Decimal(str(labor))


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
            "proposal_material_amount, proposal_labor_amount,"
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


def amounts_overview(project_id: str) -> dict:
    """The Send Out numbers editor's data: the committed-pricing default plus
    each GC's override and resolved Material/Labor/TOTAL. Decimals go over the
    wire as strings (pricing_summary convention)."""
    from app.routers.pricing import _get_one, _verify_originals

    verification = _get_one("verifications", project_id)
    committed = bool(verification and verification.get("committed_at"))
    defaults = (
        proposal_amounts(_verify_originals(project_id), verification) if committed else None
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
        resolved = resolve_gc_amounts(defaults, gc) if defaults is not None else None
        gcs.append(
            {
                "gc_id": gc["id"],
                "gc_name": gc["name"],
                "material_override": _s(gc["material_override"]),
                "labor_override": _s(gc["labor_override"]),
                "material": _s(resolved["material"]) if resolved else None,
                "labor": _s(resolved["labor"]) if resolved else None,
                "total": _s(resolved["total"]) if resolved else None,
                "locked": gc["id"] in locked,
            }
        )
    return {
        "committed": committed,
        "default": (
            {
                "material": str(defaults["material"]),
                "labor": str(defaults["labor"]),
                "total": str(defaults["total"]),
            }
            if defaults is not None
            else None
        ),
        "gcs": gcs,
    }


def set_gc_amounts(
    project_id: str,
    gc_id: str,
    material: Decimal | None,
    labor: Decimal | None,
    user_id: str,
) -> dict:
    """Set (or clear, with None) one GC's proposal amount overrides. Allowed
    until that GC's proposal is sent/sending. An already-generated document
    goes stale — send fails closed on the stamp mismatch until regenerated."""
    sb = get_supabase()
    project = (
        sb.table("projects").select("id, current_stage").eq("id", project_id)
        .single().execute()
    ).data
    if not project:
        raise ProposalSendError("Project not found", status_code=404)
    if project["current_stage"] != "send_out":
        raise ProposalSendError("Project is not at the Send Out stage.")
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
    sb.table("project_gcs").update(
        {
            "proposal_material_amount": str(material) if material is not None else None,
            "proposal_labor_amount": str(labor) if labor is not None else None,
        }
    ).eq("project_id", project_id).eq("gc_id", gc_id).execute()
    audit(
        user_id,
        "proposal.amounts_set",
        "project",
        project_id,
        {
            "gc_id": gc_id,
            "material_amount": str(material) if material is not None else None,
            "labor_amount": str(labor) if labor is not None else None,
        },
    )
    return amounts_overview(project_id)


def retire_unsent_proposals(project_id: str, gc_id: str) -> None:
    """A GC removed from the project keeps its sent history; never-sent rows
    are retired so the Send Out panel stops offering them (the same sweep
    generate_documents runs for GCs it finds removed)."""
    get_supabase().table("proposal_sends").update({"status": "superseded"}).eq(
        "project_id", project_id
    ).eq("gc_id", gc_id).in_("status", ["generated", "failed"]).execute()


def send_out_outcome(gcs: list[dict], sent_gc_ids: set[str]) -> tuple[list[str], list[str]]:
    """(sent, skipped) GC names for the completion record. Skipped = on the
    project but never sent a proposal — the 'decided not to bid to them'
    signal the stage-event note preserves."""
    sent = sorted(g["name"] for g in gcs if g["id"] in sent_gc_ids)
    skipped = sorted(g["name"] for g in gcs if g["id"] not in sent_gc_ids)
    return sent, skipped


def complete_send_out(project_id: str, user_id: str) -> dict:
    """The PA's explicit "Done sending" — the only way Send Out ends. Requires
    at least one sent proposal (a bid was actually submitted to someone);
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
    if project["current_stage"] != "send_out":
        raise ProposalSendError("Project is not at the Send Out stage.")
    rows = (
        sb.table("proposal_sends").select("gc_id, status").eq("project_id", project_id)
        .execute()
    ).data or []
    if any(r["status"] == "sending" for r in rows):
        raise ProposalSendError(
            "A proposal send is in progress or unresolved — wait or retry it first."
        )
    sent_gc_ids = {r["gc_id"] for r in rows if r["status"] == "sent"}
    if not sent_gc_ids:
        raise ProposalSendError(
            "No proposal has been sent yet — send at least one before marking the bid submitted.",
            status_code=400,
        )

    sent, skipped = send_out_outcome(_project_gcs(project_id), sent_gc_ids)
    note = "Done sending"
    if sent:
        note += " — sent to: " + ", ".join(sent)
    if skipped:
        note += "; skipped (no bid): " + ", ".join(skipped)
    workflow.transition_project(project_id, "submitted", user_id, note)
    for role in (Role.PM, Role.EXECUTIVE):
        notify_role(
            role, project_id, "submitted",
            f"Bid submitted — proposals sent for {project['name']}",
        )
    audit(user_id, "project.send_out_complete", "project", project_id,
          {"sent_gcs": sent, "skipped_gcs": skipped})
    return {"stage": "submitted", "sent": sent, "skipped": skipped}


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


def build_base_context(project: dict, draft: dict, amounts: dict[str, Decimal]) -> ProposalContext:
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
        labor_time=LABOR_TIME_TEXT[project["labor_time"]],
        wage_text=WAGE_TEXT[project["wage_type"]],
        material_amount=format_money(amounts["material"]),
        labor_amount=format_money(amounts["labor"]),
        total_amount=format_money(amounts["total"]),
        scope_lines=tuple(draft["lines_json"]),
    )


def generate_documents(project_id: str, draft_id: str, user_id: str) -> list[dict]:
    """Render + store one proposal per GC on the project. All-or-nothing: any
    render failure aborts the whole batch so the review UI never shows a
    partial GC↔file mapping. Rows already 'sent' are left untouched."""
    from app.routers.pricing import _get_one, _verify_originals

    sb = get_supabase()
    project = sb.table("projects").select("*").eq("id", project_id).single().execute().data
    if not project:
        raise ProposalSendError("Project not found", status_code=404)
    if project["current_stage"] != "send_out":
        raise ProposalSendError("Project is not at the Send Out stage.")
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
    base_ctx = build_base_context(project, draft, amounts)
    template = load_template_bytes()
    digest = lines_hash(list(draft["lines_json"]))
    all_names = _all_project_gc_names(project_id)

    # Render + validate EVERYTHING before touching storage or the DB.
    rendered: list[tuple[dict, str, bytes, dict[str, Decimal]]] = []
    for gc in targets:
        gc_amounts = resolve_gc_amounts(amounts, gc)
        ctx = replace(
            base_ctx,
            gc_name=gc["name"],
            material_amount=format_money(gc_amounts["material"]),
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
                amounts=(ctx.material_amount, ctx.labor_amount, ctx.total_amount),
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
            # check + the per-GC audit record of what we bid them.
            "material_amount": str(gc_amounts["material"]),
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
          {"draft_id": draft_id, "gcs": len(created)})
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
) -> None:
    """Defense-in-depth before the ONE network call that can leak a document.
    Pure: raises ProposalSendError with the reason; callers pass live rows
    (`row` is the freshly claimed proposal_sends row, so its gc_email is the
    recipient list this very send wrote)."""
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
    # the stamped figures. Rows stamped before this feature have no stamp to
    # prove — they keep the pre-feature behavior.
    stamped = stamped_amounts(row)
    if stamped is not None and expected_amounts is not None:
        if stamped != (expected_amounts["material"], expected_amounts["labor"]):
            raise ProposalSendError(
                "Amounts changed since this document was generated — regenerate documents."
            )

    validate_output(
        docx_bytes,
        gc_name=row["gc_name"],
        scope_lines=tuple(draft["lines_json"]),
        other_gc_names=other_gc_names,
        amounts=(
            (
                format_money(stamped[0]),
                format_money(stamped[1]),
                format_money(stamped[0] + stamped[1]),
            )
            if stamped is not None
            else ()
        ),
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


def send_proposals(
    project_id: str,
    user_id: str,
    proposal_ids: list[str],
    email_body: str | None = None,
    force: bool = False,
    contacts: dict[str, list[str]] | None = None,
) -> dict:
    sb = get_supabase()
    project = sb.table("projects").select("*").eq("id", project_id).single().execute().data
    if not project:
        raise ProposalSendError("Project not found", status_code=404)
    if project["current_stage"] != "send_out":
        raise ProposalSendError("Project is not at the Send Out stage.")

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
    live_gcs = {gc["id"]: gc for gc in _project_gcs(project_id)}
    all_names = _all_project_gc_names(project_id)
    # Live default amounts for the staleness check — pricing is committed by
    # the time send_out is reachable, but stay defensive (None = skip check).
    from app.routers.pricing import _get_one, _verify_originals

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
            if len(pdf_bytes) >= graph_email._INLINE_ATTACHMENT_LIMIT:
                raise ProposalSendError(
                    "Proposal PDF is too large to email — contact support."
                )
            # Belt-and-suspenders leak re-scan on the rendered PDF itself: the
            # docx isolation scan can't see content that only the PDF renders.
            _pdf_stamp = stamped_amounts(row)
            validate_pdf_isolation(
                office_preview.extract_pdf_text(pdf_bytes),
                gc_name=row["gc_name"],
                other_gc_names=tuple(n for n in all_names if n != row["gc_name"]),
                amounts=(
                    (
                        format_money(_pdf_stamp[0]),
                        format_money(_pdf_stamp[1]),
                        format_money(_pdf_stamp[0] + _pdf_stamp[1]),
                    )
                    if _pdf_stamp is not None
                    else ()
                ),
            )
            pdf_name = office_preview.pdf_filename(file_row["filename"])

            body = body_template.replace(GC_NAME_TOKEN, row["gc_name"])
            try:
                log = graph_email.send_mail(
                    to=recipients,
                    subject=subject,
                    body_html=email_branding.render_proposal_email(body),
                    attachments=[(pdf_name, pdf_bytes)],
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
            audit(user_id, "proposal.send", "proposal_send", row["id"],
                  {"gc_id": row["gc_id"], "to": join_recipients(recipients),
                   "file_id": row["file_id"]})
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

        notify_role(Role.PA, project_id, "proposal_send_failed",
                    f"{len(failed)} proposal send(s) failed — retry from the Send Out panel")

    audit(user_id, "project.send_out", "project", project_id,
          {"sent": sum(1 for r in results if r["status"] == "sent"),
           "failed": sum(1 for r in results if r["status"] == "failed")})
    return {"results": results, "stage": project["current_stage"]}


def _result(row: dict, status: str, error: str | None) -> dict:
    return {
        "proposal_id": row["id"],
        "gc_id": row["gc_id"],
        "gc_name": row["gc_name"],
        "status": status,
        "error": error,
    }

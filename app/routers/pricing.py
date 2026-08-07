"""Pricing pipeline: labor review (7), markup (8), executive verify/commit (9).
Send-out (10) lives in routers/proposals.py — per-GC proposal generation+email."""

from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import CurrentUser, get_current_user, require_role, require_writer
from app.core.roles import INTERNAL_ROLES, VERIFY_ROLES, Role
from app.core.supabase_client import get_supabase
from app.models.schemas import LaborReviewIn, MarkupIn, VerifyOverrideIn
from app.services import vendor_selection, workflow
from app.services.notifications import audit, notify_role

router = APIRouter(prefix="/projects/{project_id}", tags=["pricing"])


def _internal(user: CurrentUser) -> None:
    if user.role not in INTERNAL_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")


def _get_one(table: str, project_id: str):
    rows = get_supabase().table(table).select("*").eq("project_id", project_id).execute().data or []
    return rows[0] if rows else None


def _general_estimate(project_id: str) -> dict | None:
    """The per-project general-material (wiring) figure pulled from the estimate,
    with its tax attestation (it carries the same tax question as vendor quotes)."""
    rows = (
        get_supabase()
        .table("general_material_estimates")
        .select("amount, source, status, tax_included, tax_rate")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    return rows[0] if rows else None


def pick_material_amount(selected: dict | None) -> tuple[Decimal | None, str]:
    """Pure: the price basis for one category — the tax-inclusive total of its
    SELECTED quote, and where that quote came from.

    There is no fallback chain. A category is priced by the quote a human picked on
    Select Vendors or it is not priced at all: the lowest quote received is a display
    detail, not a decision, and a hand-entered figure is just another candidate quote
    (origin 'manual') competing on equal footing. Returning None here is what keeps
    the Select Vendors step open (see services/vendor_selection)."""
    if selected is None:
        return None, "none"
    return tax_info(selected)["total"], selected.get("origin") or "vendor"


# Clark County (Las Vegas) sales tax — the default rate applied when a vendor's
# quote did not already include it.
DEFAULT_TAX_RATE = Decimal("8.375")

# Pricing sections: the named buckets material categories roll into for markup,
# verify, per-GC pricing and the proposal box. 'materials' is the residual (and
# always present); the breakouts exist on a project only while it has an RFQ in
# a category mapped to them (material_categories.pricing_section).
SECTION_KEYS = ("materials", "gear", "underground", "low_voltage")


def apply_tax(
    amount: Decimal | None, tax_included: bool | None, tax_rate: Decimal | None
) -> tuple[Decimal | None, Decimal]:
    """Pure: the tax-inclusive cost of one vendor quote and the tax added.
    When the vendor already priced tax in (or there's no amount) the quote
    stands and the tax added is 0; otherwise the rate (a percent) is applied
    and rounded to cents — the real cost G3 incurs on the materials. A NULL
    tax_included (unanswered) is treated as "not yet included" so the figure is
    never understated before the estimator answers."""
    if amount is None:
        return None, Decimal("0.00")
    if tax_included:  # vendor already included tax in the quote
        return amount, Decimal("0.00")
    rate = tax_rate if tax_rate is not None else DEFAULT_TAX_RATE
    tax = (amount * rate / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return amount + tax, tax


def tax_info(row: dict) -> dict:
    """Pure: the tax breakdown of one priced row (a vendor quote or the General
    Material estimate — anything with amount/tax_included/tax_rate) — pre-tax
    amount, tax added at its rate, and the tax-inclusive total that pricing
    compares and carries."""
    pre = Decimal(str(row["amount"]))
    rate = Decimal(str(row["tax_rate"])) if row.get("tax_rate") is not None else DEFAULT_TAX_RATE
    total, tax = apply_tax(pre, row.get("tax_included"), rate)
    return {
        "pre_tax": pre,
        "total": total,
        "tax": tax,
        "rate": rate,
        "included": row.get("tax_included"),
    }


def taxed_amount(row: dict) -> Decimal:
    """Pure: the tax-inclusive amount of one priced row — the comparison basis."""
    return tax_info(row)["total"]


def _materials_rows(project_id: str) -> list[dict]:
    """Per-RFQ materials price for the project. Every category's price is the
    tax-inclusive total of the quote selected on Select Vendors — there is no
    fallback to the lowest quote and no hand-entered override that outranks it.
    General Material is no different: its estimate figure is a candidate quote
    (origin 'estimate') that has to be picked like any other."""
    sb = get_supabase()
    rfqs = (
        sb.table("rfqs")
        .select(
            "id, material_category_id,"
            " material_categories(name, is_general, pricing_section, sort_order)"
        )
        .eq("project_id", project_id)
        .execute()
    ).data or []
    rfq_ids = [r["id"] for r in rfqs]
    # ONLY the selected quotes. Selection is the only thing that prices a category,
    # so there is no lowest-received to collect and nothing to compare.
    quotes = (
        sb.table("quotes")
        .select("rfq_id, amount, is_selected, tax_included, tax_rate, origin")
        .in_("rfq_id", rfq_ids)
        .eq("is_selected", True)
        .execute()
    ).data if rfq_ids else []
    selected: dict[str, dict] = {q["rfq_id"]: q for q in (quotes or [])}

    gen = _general_estimate(project_id)
    gen_amount = Decimal(str(gen["amount"])) if gen and gen.get("amount") is not None else None
    gen_source = gen.get("source") if gen else None

    rows: list[dict] = []
    saw_general = False
    for r in rfqs:
        cat = r.get("material_categories") or {}
        is_general = bool(cat.get("is_general"))
        saw_general = saw_general or is_general
        # Every category — General Material included — is priced by the quote a
        # human selected. General's estimate figure is itself a candidate quote
        # (origin 'estimate'), so it needs no special case here.
        win = selected.get(r["id"])
        amount, source = pick_material_amount(win)
        # The winning quote's tax breakdown, for the row detail.
        info = tax_info(win) if win else None
        rows.append(
            {
                "material_category_id": r["material_category_id"],
                "category_name": cat.get("name"),
                "is_general": is_general,
                # The section this category's figure rolls into (markup, verify,
                # per-GC pricing, proposal box). Missing mapping = residual.
                "pricing_section": cat.get("pricing_section") or "materials",
                "category_sort_order": cat.get("sort_order"),
                # amount is tax-inclusive — the true cost that feeds the total,
                # markup, verify and the bid.
                "amount": str(amount) if amount is not None else None,
                "pre_tax_amount": str(info["pre_tax"]) if info else (
                    str(amount) if amount is not None else None
                ),
                "tax_included": info["included"] if info else None,
                "tax_rate": str(info["rate"]) if info else str(DEFAULT_TAX_RATE),
                "tax_amount": str(info["tax"]) if info else "0.00",
                "source": source,
            }
        )

    # General Material is priced from the estimate, so its figure counts even when
    # no General Material RFQ has been created yet (it never goes out for quotes).
    if not saw_general and gen_amount is not None:
        general_cat = (
            sb.table("material_categories")
            .select("id, name")
            .eq("is_general", True)
            .limit(1)
            .execute()
        ).data or []
        info = tax_info(gen)
        rows.append(
            {
                "material_category_id": general_cat[0]["id"] if general_cat else None,
                "category_name": general_cat[0]["name"] if general_cat else "General Material",
                "is_general": True,
                # The synthesized General row is always residual materials.
                "pricing_section": "materials",
                "category_sort_order": None,
                "amount": str(info["total"]),
                "pre_tax_amount": str(info["pre_tax"]),
                "tax_included": info["included"],
                "tax_rate": str(info["rate"]),
                "tax_amount": str(info["tax"]),
                "source": gen_source or "manual",
            }
        )
    return rows


def _materials_total(project_id: str) -> Decimal:
    """Materials price basis summed across the project's RFQs (see _materials_rows).
    The sum over ALL rows: identical to the sum of the section amounts."""
    return sum(
        (Decimal(r["amount"]) for r in _materials_rows(project_id) if r["amount"] is not None),
        Decimal(0),
    )


def section_summary(rows: list[dict]) -> dict[str, dict]:
    """Pure: the section decomposition of the materials rows (_materials_rows).
    A section is present iff the project has a row mapped to it ('materials' is
    always present, the residual bucket). amount is the sum of that section's
    priced rows: Decimal(0) when present but unpriced, None when absent, so the
    sections partition the materials total exactly (no row counted twice, none
    dropped). The gear entry also says whether a generator category is on the
    project (drives the proposal's "*Includes Generator/s" caption)."""
    out: dict[str, dict] = {}
    for key in SECTION_KEYS:
        mine = [r for r in rows if (r.get("pricing_section") or "materials") == key]
        present = key == "materials" or bool(mine)
        amount = (
            sum(
                (Decimal(r["amount"]) for r in mine if r.get("amount") is not None),
                Decimal(0),
            )
            if present
            else None
        )
        mine.sort(key=lambda r: (r.get("category_sort_order") is None,
                                 r.get("category_sort_order") or 0))
        entry: dict = {
            "present": present,
            "amount": amount,
            "categories": [r["category_name"] for r in mine if r.get("category_name")],
        }
        if key == "gear":
            entry["includes_generator"] = any(
                "generator" in (r.get("category_name") or "").lower() for r in mine
            )
        out[key] = entry
    return out


def _num(row, key) -> Decimal | None:
    return Decimal(str(row[key])) if row and row.get(key) is not None else None


def _verify_originals(project_id: str) -> dict[str, Decimal | None]:
    """The upstream figures the verify step starts from: labor (step 7), the
    computed per-section materials partition (selected/lowest quotes), and the
    markups (step 8). Used to pre-fill the verify form and to record the delta
    on commit. materials_amount is the RESIDUAL materials figure; each breakout
    section's amount is None when the section is not on the project, as are its
    markup originals (a markup for a section that doesn't exist is meaningless)."""
    labor = _get_one("labor_reviews", project_id)
    markup = _get_one("markups", project_id)
    sections = section_summary(_materials_rows(project_id))

    def _sec_markup(key: str, col: str) -> Decimal | None:
        return _num(markup, col) if sections[key]["present"] else None

    return {
        "labor_amount": _num(labor, "labor_amount"),
        "materials_amount": sections["materials"]["amount"],
        "gear_amount": sections["gear"]["amount"],
        "underground_amount": sections["underground"]["amount"],
        "low_voltage_amount": sections["low_voltage"]["amount"],
        "labor_markup_amount": _num(markup, "labor_markup_amount"),
        "materials_markup_amount": _num(markup, "materials_markup_amount"),
        "gear_markup_amount": _sec_markup("gear", "gear_markup_amount"),
        "underground_markup_amount": _sec_markup("underground", "underground_markup_amount"),
        "low_voltage_markup_amount": _sec_markup("low_voltage", "low_voltage_markup_amount"),
    }


def pricing_summary_numbers(originals: dict, verification: dict | None) -> dict:
    """Pure: the four headline figures for the project summary box. Each stays
    None until its step produces a value. Bid price exists only once the
    Executive has committed, and then reads ONLY the snapshot: the commit stores
    every resolved number, a NULL legacy figure means 0, and a NULL section
    figure means the section was not part of the committed decomposition (the
    legacy pre-sections snapshot, where materials_amount carries everything),
    so legacy committed projects total exactly as before, with no double count."""
    markup_parts = [
        v
        for v in (
            originals.get("labor_markup_amount"),
            originals.get("materials_markup_amount"),
            originals.get("gear_markup_amount"),
            originals.get("underground_markup_amount"),
            originals.get("low_voltage_markup_amount"),
        )
        if v is not None
    ]

    bid_price = None
    if verification and verification.get("committed_at"):
        bid_price = Decimal(0)
        for key in VERIFY_NUMBERS:
            final = _num(verification, key)
            if final is None and key not in VERIFY_SECTION_NUMBERS:
                final = Decimal(0)
            if final is not None:
                bid_price += final

    def _s(v: Decimal | None) -> str | None:
        return str(v) if v is not None else None

    return {
        "materials_amount": _s(originals.get("materials_amount")),
        "labor_amount": _s(originals.get("labor_amount")),
        "markup_amount": _s(sum(markup_parts, Decimal(0))) if markup_parts else None,
        "bid_price": _s(bid_price),
    }


@router.get("/pricing-summary")
def get_pricing_summary(project_id: str, user: CurrentUser = Depends(get_current_user)):
    """The headline pricing figures for the always-visible project summary box."""
    _internal(user)
    materials = [
        Decimal(r["amount"]) for r in _materials_rows(project_id) if r["amount"] is not None
    ]
    labor = _get_one("labor_reviews", project_id)
    markup = _get_one("markups", project_id)
    originals = {
        "labor_amount": _num(labor, "labor_amount"),
        # Unlike _materials_total, no priced category means "not there yet"
        # (None), not $0 — the summary box shows blank until quotes land.
        # Deliberately the TOTAL across every category (sections included):
        # the summary box keeps its aggregate semantics.
        "materials_amount": sum(materials, Decimal(0)) if materials else None,
        "labor_markup_amount": _num(markup, "labor_markup_amount"),
        "materials_markup_amount": _num(markup, "materials_markup_amount"),
        "gear_markup_amount": _num(markup, "gear_markup_amount"),
        "underground_markup_amount": _num(markup, "underground_markup_amount"),
        "low_voltage_markup_amount": _num(markup, "low_voltage_markup_amount"),
    }
    return pricing_summary_numbers(originals, _get_one("verifications", project_id))


# ── Price basis (labor + per-section materials prices feeding markup) ──────


def _sections_wire(sections: dict[str, dict]) -> dict[str, dict]:
    """The wire shape of the section decomposition (price-basis / amounts
    overview): presence + the category names behind each section (raw DB
    strings, listed in the captions), plus the generator flag on gear."""
    out: dict[str, dict] = {}
    for key in SECTION_KEYS:
        s = sections[key]
        entry: dict = {"present": s["present"], "categories": s["categories"]}
        if key == "gear":
            entry["includes_generator"] = s["includes_generator"]
        out[key] = entry
    return out


def _readiness_wire(project_id: str, wire: dict[str, dict]) -> None:
    """Stamp `ready` + `waiting_on` onto an already-built section wire dict.

    Readiness is NOT recomputed here. It comes from
    services/vendor_selection.section_readiness, the same function that decides
    whether Select Vendors may be completed, so the lock the Markup page draws
    and the gate the workflow enforces can never drift apart.

    Two edges the readiness map deliberately leaves to the caller:
      • a section that is not on the project has no markup box to unlock, so it
        reports ready=false with nothing outstanding,
      • a section that IS on the project but has no categories feeding it (the
        residual materials bucket on a project with no RFQs yet) is absent from
        the map and has nothing left to decide, so it reports ready=true.
    """
    readiness = vendor_selection.section_readiness(project_id)
    for key in SECTION_KEYS:
        entry = wire[key]
        state = readiness.get(key) if entry["present"] else None
        entry["ready"] = bool(state["ready"]) if state else bool(entry["present"])
        entry["waiting_on"] = list(state["waiting_on"]) if state else []


@router.get("/price-basis")
def get_price_basis(project_id: str, user: CurrentUser = Depends(get_current_user)):
    """The prices assigned upstream: labor (step 7) and the per-section
    materials partition (selected quotes). materials_amount is the RESIDUAL
    materials figure; a breakout section's amount is null while the section is
    not on the project (no RFQ in a category mapped to it).

    Each section also carries whether its markup box may be worked yet: `ready`
    once every category rolling into it has a winning quote selected, with
    `waiting_on` naming the categories still undecided. `labor_ready` is the
    labor markup's equivalent: a labor figure has been recorded.

    Both are UI AFFORDANCES ONLY. PUT /markup is deliberately left ungated on
    the server: a verify bounce, a post-verify price edit and the per-GC pricing
    flow all have to be able to rewrite a markup whatever the lanes currently
    say, and Markup opens (soft prereq) long before Select Vendors completes.
    """
    _internal(user)
    labor = _get_one("labor_reviews", project_id)
    sections = section_summary(_materials_rows(project_id))
    wire = _sections_wire(sections)
    _readiness_wire(project_id, wire)
    labor_amount = labor.get("labor_amount") if labor else None

    def _s(v: Decimal | None) -> str | None:
        return str(v) if v is not None else None

    return {
        "labor_amount": str(labor_amount) if labor_amount is not None else None,
        # The labor arm rejoins at Markup, so the labor markup box has a
        # readiness of its own: it opens once a labor figure exists.
        "labor_ready": labor_amount is not None,
        "materials_amount": str(sections["materials"]["amount"]),
        "gear_amount": _s(sections["gear"]["amount"]),
        "underground_amount": _s(sections["underground"]["amount"]),
        "low_voltage_amount": _s(sections["low_voltage"]["amount"]),
        "sections": wire,
    }


@router.get("/materials-breakdown")
def get_materials_breakdown(project_id: str, user: CurrentUser = Depends(get_current_user)):
    """Per-category materials prices feeding the total, so the PM can see every
    number (vendor quotes vs the estimate-derived general-material figure).
    Rows carry their pricing_section; the sections envelope adds the per-section
    subtotals the breakdown table groups by."""
    _internal(user)
    rows = _materials_rows(project_id)
    gen = _general_estimate(project_id)
    sections = section_summary(rows)
    wire_sections = _sections_wire(sections)
    for key in SECTION_KEYS:
        amount = sections[key]["amount"]
        wire_sections[key]["subtotal"] = str(amount) if amount is not None else None
    return {
        "rows": rows,
        "total": str(_materials_total(project_id)),
        "general_status": gen.get("status") if gen else None,
        "sections": wire_sections,
    }


# ── Labor review (step 7) ─────────────────────────────────────────────────


@router.get("/labor")
def get_labor(project_id: str, user: CurrentUser = Depends(get_current_user)):
    _internal(user)
    return _get_one("labor_reviews", project_id)


@router.put("/labor")
def set_labor(
    project_id: str, body: LaborReviewIn, user: CurrentUser = Depends(require_writer)
):
    row = (
        get_supabase()
        .table("labor_reviews")
        .upsert(
            {"project_id": project_id, "reviewed_by": user.id, "updated_at": "now()", **body.model_dump(mode="json")},
            on_conflict="project_id",
        )
        .execute()
    ).data[0]
    audit(user.id, "labor.review", "project", project_id, {"verified": body.verified})
    workflow.maybe_reopen_verify_after_edit(project_id, user.id, "Labor numbers edited")
    return row


# ── Markup (step 8) ───────────────────────────────────────────────────────


@router.get("/markup")
def get_markup(project_id: str, user: CurrentUser = Depends(get_current_user)):
    _internal(user)
    return _get_one("markups", project_id)


@router.put("/markup")
def set_markup(
    project_id: str, body: MarkupIn, user: CurrentUser = Depends(require_writer)
):
    row = (
        get_supabase()
        .table("markups")
        .upsert(
            {"project_id": project_id, "set_by": user.id, "updated_at": "now()", **body.model_dump(mode="json")},
            on_conflict="project_id",
        )
        .execute()
    ).data[0]
    audit(user.id, "markup.set", "project", project_id, None)
    workflow.maybe_reopen_verify_after_edit(project_id, user.id, "Markup edited")
    return row


# ── Executive verification / commit (step 9) ──────────────────────────────


@router.get("/verify")
def get_verify(project_id: str, user: CurrentUser = Depends(get_current_user)):
    _internal(user)
    return _get_one("verifications", project_id)


# The ten figures the verify step resolves, snapshots and audits. Order matters
# for the audit delta and the FE row order. materials_amount is the RESIDUAL
# materials figure; the three breakout amounts complete the partition.
VERIFY_NUMBERS = (
    "labor_amount",
    "materials_amount",
    "gear_amount",
    "underground_amount",
    "low_voltage_amount",
    "labor_markup_amount",
    "materials_markup_amount",
    "gear_markup_amount",
    "underground_markup_amount",
    "low_voltage_markup_amount",
)

# The section-scoped subset: on a COMMITTED snapshot a NULL here means "this
# section was not part of the committed decomposition" (absent section, or the
# legacy pre-sections snapshot) and resolves to None, never 0. The four legacy
# keys keep resolving NULL to 0.
VERIFY_SECTION_NUMBERS = (
    "gear_amount",
    "underground_amount",
    "low_voltage_amount",
    "gear_markup_amount",
    "underground_markup_amount",
    "low_voltage_markup_amount",
)

# verify-number key -> the section whose presence gates it at commit time.
_SECTION_OF_NUMBER = {
    "gear_amount": "gear",
    "gear_markup_amount": "gear",
    "underground_amount": "underground",
    "underground_markup_amount": "underground",
    "low_voltage_amount": "low_voltage",
    "low_voltage_markup_amount": "low_voltage",
}


def verify_delta_pairs(originals: dict, body: VerifyOverrideIn) -> dict:
    """Pure: original → final for each verify number, so the change is auditable."""
    out: dict[str, dict] = {}
    for key in VERIFY_NUMBERS:
        orig = originals.get(key)
        final = getattr(body, key)
        out[key] = {
            "from": str(orig) if orig is not None else None,
            "to": str(final) if final is not None else None,
        }
    return out


def _deltas(body: VerifyOverrideIn, project_id: str) -> dict:
    return verify_delta_pairs(_verify_originals(project_id), body)


@router.put("/verify")
def edit_verify(
    project_id: str,
    body: VerifyOverrideIn,
    user: CurrentUser = Depends(require_role(*VERIFY_ROLES)),
):
    """Save the (uncommitted) verify-step numbers. Only the Executive (or IT Admin
    override) may touch the verify figures; the snapshot becomes immutable once
    committed."""
    existing = _get_one("verifications", project_id)
    if existing and existing.get("committed_at"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Pricing already committed — cannot edit")
    row = (
        get_supabase()
        .table("verifications")
        .upsert(
            {"project_id": project_id, "updated_at": "now()", **body.model_dump(mode="json")},
            on_conflict="project_id",
        )
        .execute()
    ).data[0]
    audit(user.id, "pricing.verify_edit", "project", project_id, _deltas(body, project_id))
    return row


@router.post("/verify")
def commit_verify(
    project_id: str,
    body: VerifyOverrideIn | None = None,
    user: CurrentUser = Depends(require_role(*VERIFY_ROLES)),
):
    """The Executive finalizes and commits pricing — required before send-out.
    The committed snapshot stores ALL TEN resolved figures (override wins per
    key, else the live upstream figure) so downstream readers never fall back to
    figures that can move after the commit. Sections not on the project are
    stored NULL (never 0), the marker readers resolve to "not part of the
    decomposition". The original→final delta is recorded in the audit log."""
    body = body or VerifyOverrideIn()
    originals = _verify_originals(project_id)
    sections = section_summary(_materials_rows(project_id))
    snapshot: dict[str, str | None] = {}
    for key in VERIFY_NUMBERS:
        section = _SECTION_OF_NUMBER.get(key)
        if section and not sections[section]["present"]:
            snapshot[key] = None
            continue
        final = getattr(body, key)
        if final is None:
            final = originals.get(key)
        snapshot[key] = str(final) if final is not None else None
    row = (
        get_supabase()
        .table("verifications")
        .upsert(
            {
                "project_id": project_id,
                "verified_by": user.id,
                "committed_at": "now()",
                "updated_at": "now()",
                "notes": body.notes,
                **snapshot,
            },
            on_conflict="project_id",
        )
        .execute()
    ).data[0]
    audit(user.id, "pricing.commit", "project", project_id, _deltas(body, project_id))
    # Advance off Verify. Normally verify → send_out so the team can dispatch the
    # bid; but if a post-verify pricing edit bounced the project back here, resume
    # at the stage it was on before the edit (send_out / submitted / bid_outcome).
    proj = (
        get_supabase()
        .table("projects")
        .select("reverify_return_stage")
        .eq("id", project_id)
        .single()
        .execute()
    ).data
    state = workflow.load_category_state(project_id)
    send_head = state.get("send_out", {}).get("current_task")
    # Only advance + notify when the commit actually moved the send_out head off Verify;
    # a redundant re-commit (already advanced) just re-stamps the snapshot silently.
    if send_head == "verify":
        return_stage = proj.get("reverify_return_stage") if proj else None
        if return_stage:
            workflow.return_from_reverify(project_id, return_stage, user.id)
            notify_role(Role.ESTIMATING_ADMIN, project_id, "verified", "Pricing re-committed by Executive")
        else:
            workflow.advance_category(project_id, "send_out", user.id, "Pricing committed")
            notify_role(Role.ESTIMATING_ADMIN, project_id, "verified", "Pricing committed by Executive — ready to send out")
    return row



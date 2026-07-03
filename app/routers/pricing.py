"""Pricing pipeline: labor review (7), markup (8), executive verify/commit (9).
Send-out (10) lives in routers/proposals.py — per-GC proposal generation+email."""

from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import CurrentUser, get_current_user, require_role, require_writer
from app.core.roles import INTERNAL_ROLES, VERIFY_ROLES, Role
from app.core.supabase_client import get_supabase
from app.models.schemas import LaborReviewIn, MarkupIn, VerifyOverrideIn
from app.services import workflow
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


def pick_material_amount(
    custom: Decimal | None, selected: Decimal | None, lowest: Decimal | None
) -> tuple[Decimal | None, str]:
    """Pure: the price basis for one quoted category — the PE's custom price
    beats the explicitly selected quote, which beats the lowest received."""
    if custom is not None:
        return custom, "manual"
    if selected is not None:
        return selected, "quote"
    if lowest is not None:
        return lowest, "quote"
    return None, "none"


# Clark County (Las Vegas) sales tax — the default rate applied when a vendor's
# quote did not already include it.
DEFAULT_TAX_RATE = Decimal("8.375")


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
    """Per-RFQ materials price for the project. For every category the price is
    the PE's custom price (else the selected quote, else the lowest received).
    The exception is General Material, which is priced from the estimate's
    wiring figure (or a manual override) instead of vendor quotes."""
    sb = get_supabase()
    rfqs = (
        sb.table("rfqs")
        .select("id, material_category_id, custom_amount, material_categories(name, is_general)")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    rfq_ids = [r["id"] for r in rfqs]
    quotes = (
        sb.table("quotes")
        .select("rfq_id, amount, is_selected, tax_included, tax_rate")
        .in_("rfq_id", rfq_ids)
        .execute()
    ).data if rfq_ids else []
    quotes = quotes or []

    # Lowest received and explicitly-selected quote per RFQ — compared by their
    # tax-INCLUSIVE totals (each quote carries its own tax answer); selection
    # wins. The winning quote's tax breakdown is kept for the row detail.
    lowest: dict[str, dict] = {}
    selected: dict[str, dict] = {}
    for q in quotes:
        info = tax_info(q)
        rid = q["rfq_id"]
        if rid not in lowest or info["total"] < lowest[rid]["total"]:
            lowest[rid] = info
        if q.get("is_selected"):
            selected[rid] = info

    gen = _general_estimate(project_id)
    gen_amount = Decimal(str(gen["amount"])) if gen and gen.get("amount") is not None else None
    gen_source = gen.get("source") if gen else None

    rows: list[dict] = []
    saw_general = False
    for r in rfqs:
        cat = r.get("material_categories") or {}
        is_general = bool(cat.get("is_general"))
        info = None  # the winning figure's tax breakdown, when something priced the row
        if is_general:
            saw_general = True
            # The estimate figure carries its own tax answer (0053) — taxed
            # exactly like a vendor quote so the total is the true cost.
            if gen_amount is not None:
                info = tax_info(gen)
                amount = info["total"]
                source = gen_source
            else:
                amount = None
                source = "none"
        else:
            rid = r["id"]
            custom = r.get("custom_amount")
            sel, low = selected.get(rid), lowest.get(rid)
            # Precedence over tax-INCLUSIVE figures; a custom price is the PE's
            # hand-entered true cost and is carried as-is (no tax added on top).
            amount, source = pick_material_amount(
                Decimal(str(custom)) if custom is not None else None,
                sel["total"] if sel else None,
                low["total"] if low else None,
            )
            if source == "quote":
                info = sel or low
        rows.append(
            {
                "material_category_id": r["material_category_id"],
                "category_name": cat.get("name"),
                "is_general": is_general,
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
    """Materials price basis summed across the project's RFQs (see _materials_rows)."""
    return sum(
        (Decimal(r["amount"]) for r in _materials_rows(project_id) if r["amount"] is not None),
        Decimal(0),
    )


def _num(row, key) -> Decimal | None:
    return Decimal(str(row[key])) if row and row.get(key) is not None else None


def _verify_originals(project_id: str) -> dict[str, Decimal | None]:
    """The upstream figures the verify step starts from — labor (step 7),
    computed materials (selected/lowest quotes), and the two markups (step 8).
    Used to pre-fill the verify form and to record the delta on commit."""
    labor = _get_one("labor_reviews", project_id)
    markup = _get_one("markups", project_id)

    return {
        "labor_amount": _num(labor, "labor_amount"),
        "materials_amount": _materials_total(project_id),
        "labor_markup_amount": _num(markup, "labor_markup_amount"),
        "materials_markup_amount": _num(markup, "materials_markup_amount"),
    }


def pricing_summary_numbers(originals: dict, verification: dict | None) -> dict:
    """Pure: the four headline figures for the project summary box. Each stays
    None until its step produces a value. Bid price exists only once the
    Executive has committed; the committed override wins over each upstream
    figure, falling back to it where the snapshot left a number null."""
    markup_parts = [
        v
        for v in (originals.get("labor_markup_amount"), originals.get("materials_markup_amount"))
        if v is not None
    ]

    bid_price = None
    if verification and verification.get("committed_at"):
        finals = []
        for key in VERIFY_NUMBERS:
            final = _num(verification, key)
            if final is None:
                final = originals.get(key)
            if final is not None:
                finals.append(final)
        if finals:
            bid_price = sum(finals, Decimal(0))

    def _s(v: Decimal | None) -> str | None:
        return str(v) if v is not None else None

    return {
        "materials_amount": _s(originals.get("materials_amount")),
        "labor_amount": _s(originals.get("labor_amount")),
        "markup_amount": _s(sum(markup_parts, Decimal(0))) if markup_parts else None,
        "bid_price": _s(bid_price),
    }


@router.get("/pricing-summary")
async def get_pricing_summary(project_id: str, user: CurrentUser = Depends(get_current_user)):
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
        "materials_amount": sum(materials, Decimal(0)) if materials else None,
        "labor_markup_amount": _num(markup, "labor_markup_amount"),
        "materials_markup_amount": _num(markup, "materials_markup_amount"),
    }
    return pricing_summary_numbers(originals, _get_one("verifications", project_id))


# ── Price basis (labor + materials prices feeding the markup step) ─────────


@router.get("/price-basis")
async def get_price_basis(project_id: str, user: CurrentUser = Depends(get_current_user)):
    """The prices assigned upstream: labor (step 7) and materials (selected quotes)."""
    _internal(user)
    labor = _get_one("labor_reviews", project_id)
    return {
        "labor_amount": str(labor["labor_amount"]) if labor and labor.get("labor_amount") is not None else None,
        "materials_amount": str(_materials_total(project_id)),
    }


@router.get("/materials-breakdown")
async def get_materials_breakdown(project_id: str, user: CurrentUser = Depends(get_current_user)):
    """Per-category materials prices feeding the total, so the PM can see every
    number (vendor quotes vs the estimate-derived general-material figure)."""
    _internal(user)
    rows = _materials_rows(project_id)
    gen = _general_estimate(project_id)
    return {
        "rows": rows,
        "total": str(_materials_total(project_id)),
        "general_status": gen.get("status") if gen else None,
    }


# ── Labor review (step 7) ─────────────────────────────────────────────────


@router.get("/labor")
async def get_labor(project_id: str, user: CurrentUser = Depends(get_current_user)):
    _internal(user)
    return _get_one("labor_reviews", project_id)


@router.put("/labor")
async def set_labor(
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
async def get_markup(project_id: str, user: CurrentUser = Depends(get_current_user)):
    _internal(user)
    return _get_one("markups", project_id)


@router.put("/markup")
async def set_markup(
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
async def get_verify(project_id: str, user: CurrentUser = Depends(get_current_user)):
    _internal(user)
    return _get_one("verifications", project_id)


VERIFY_NUMBERS = ("labor_amount", "materials_amount", "labor_markup_amount", "materials_markup_amount")


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
async def edit_verify(
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
async def commit_verify(
    project_id: str,
    body: VerifyOverrideIn | None = None,
    user: CurrentUser = Depends(require_role(*VERIFY_ROLES)),
):
    """The Executive finalizes and commits pricing — required before send-out.
    The committed snapshot stores the final figures; the original→final delta is
    recorded in the audit log for statistics."""
    body = body or VerifyOverrideIn()
    row = (
        get_supabase()
        .table("verifications")
        .upsert(
            {
                "project_id": project_id,
                "verified_by": user.id,
                "committed_at": "now()",
                "updated_at": "now()",
                **body.model_dump(mode="json"),
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
        .select("current_stage, reverify_return_stage")
        .eq("id", project_id)
        .single()
        .execute()
    ).data
    # Only advance + notify when the commit actually moved the project off Verify;
    # a redundant re-commit (already advanced) just re-stamps the snapshot silently.
    if proj and proj["current_stage"] == "verify":
        return_stage = proj.get("reverify_return_stage")
        if return_stage:
            workflow.return_from_reverify(project_id, return_stage, user.id)
            notify_role(Role.ESTIMATING_ADMIN, project_id, "verified", "Pricing re-committed by Executive")
        else:
            workflow.transition_project(project_id, "send_out", user.id, "Pricing committed")
            notify_role(Role.ESTIMATING_ADMIN, project_id, "verified", "Pricing committed by Executive — ready to send out")
    return row



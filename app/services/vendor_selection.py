"""Which categories have a winning quote behind them, and which pricing sections
are therefore ready to mark up.

ONE source of truth for two gates that must never disagree:

  • leaving Select Vendors (routers/workflow.advance) — every category needs a winner,
  • unlocking a Markup box (routers/pricing.price_basis) — a section's markup opens
    once every category rolling into that section has one.

THERE IS NO PRICE PRECEDENCE. A category's price is the amount on its selected quote,
full stop. Nothing is inferred: not the lowest quote, not a hand-entered override.
Every candidate number is a `quotes` row — whether it arrived from a vendor's email,
was typed in by hand, or was pulled off the estimate for General Material — and
choosing among them is exactly what the Select Vendors step is for. A category with
no selected quote has no price, which is what blocks the step from completing.
"""

from app.core.supabase_client import get_supabase

# Categories with no explicit mapping roll into the residual materials section
# (mirrors pricing._materials_rows).
DEFAULT_SECTION = "materials"


def category_price_state(project_id: str) -> list[dict]:
    """Per category on the project: whether a winning quote has been selected."""
    sb = get_supabase()
    rfqs = (
        sb.table("rfqs")
        .select(
            "id, material_category_id,"
            " material_categories(name, is_general, pricing_section)"
        )
        .eq("project_id", project_id)
        .execute()
    ).data or []

    rfq_ids = [r["id"] for r in rfqs]
    selected: dict[str, dict] = {}
    if rfq_ids:
        rows = (
            sb.table("quotes")
            .select("rfq_id, amount, origin")
            .in_("rfq_id", rfq_ids)
            .eq("is_selected", True)
            .execute()
        ).data or []
        selected = {r["rfq_id"]: r for r in rows}

    out: list[dict] = []
    for r in rfqs:
        cat = r.get("material_categories") or {}
        win = selected.get(r["id"])
        out.append(
            {
                "rfq_id": r["id"],
                "material_category_id": r["material_category_id"],
                "name": cat.get("name") or "a category",
                "is_general": bool(cat.get("is_general")),
                "pricing_section": cat.get("pricing_section") or DEFAULT_SECTION,
                "has_price": win is not None,
                "source": (win or {}).get("origin"),
            }
        )
    return out


def categories_without_a_price(project_id: str) -> list[str]:
    """Names of the categories with no winner picked yet. Empty = Select Vendors may
    be completed."""
    return [c["name"] for c in category_price_state(project_id) if not c["has_price"]]


def section_readiness(project_id: str) -> dict[str, dict]:
    """Per pricing section: whether every category feeding it has a winner, and the
    names of the ones still outstanding.

    Keyed by pricing_section. A section with no categories at all is absent from the
    map — callers pair this with the section `present` flags from price-basis, which
    is what decides whether a markup box is shown in the first place.
    """
    out: dict[str, dict] = {}
    for c in category_price_state(project_id):
        sec = out.setdefault(c["pricing_section"], {"ready": True, "waiting_on": []})
        if not c["has_price"]:
            sec["ready"] = False
            sec["waiting_on"].append(c["name"])
    return out

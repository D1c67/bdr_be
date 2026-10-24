"""The production `ExtractHooks` (WS1 part B glue): one callable per
`extract.ExtractHooks` slot, each a thin adapter over the pure modules
(`notes`, `legend`, `schedules`, `tags`, `specs`, `block`). Per-page parse
results are cached on `PageContext.cache` so the role-evidence hook (which
runs before roles are scored) and the later hooks parse each page once.

Order inside `extract_document`: role_evidence -> roles -> notes (phase 3,
per page); schedules -> table_regions (all pages) -> tags -> keynote markers
(phase 4, per page); legend -> panels -> specs -> cross_trade ->
facts_block_tokens (document level). `table_regions` turns the extent of
every parsed schedule and legend table into a `schedule` or `legend` region
so a tag inside a table never counts as a plan symbol (spec 6 WS1). Every
hook is pure and never raises past `extract._safe`.
"""

from __future__ import annotations

from typing import Any

from app.services.pagefacts import block, legend, notes, schedules, specs, tags
from app.services.pagefacts.contracts import PLAN_ROLES, Region
from app.services.pagefacts.extract import DocContext, ExtractHooks, PageContext
from app.services.pagefacts.roles import RoleEvidence

ELECTRICAL_CATEGORY = "electrical_drawings"
TITLE_REACH_IN = 3.0  # a schedule title (and panel header fields) sits this far above the columns


def _title_block_x(regions: list[Region]) -> float:
    tb = next((r for r in regions if r.kind == "title_block"), None)
    return tb.bbox[0] if tb is not None and tb.bbox[0] > 0.5 else 1.0


def _ensure_notes(ctx: PageContext) -> list[dict[str, Any]]:
    if "notes" not in ctx.cache:
        ctx.cache["notes"] = notes.note_blocks(ctx.words, ctx.regions)
    return ctx.cache["notes"]


def _ensure_legend(ctx: PageContext) -> legend.PageLegend:
    if "legend" not in ctx.cache:
        ctx.cache["legend"] = legend.parse_page(ctx.words, ctx.regions, ctx.page)
    return ctx.cache["legend"]


def _ensure_schedules(ctx: PageContext) -> list[dict[str, Any]]:
    if "schedules" not in ctx.cache:
        keynotes = notes.keynote_texts(_ensure_notes(ctx))
        rev = ctx.facts.revision
        ctx.cache["schedules"] = schedules.parse_page(
            ctx.words,
            ctx.page,
            keynotes=keynotes,
            limit_x=_title_block_x(ctx.regions),
            revision=rev.number if rev is not None else None,
        )
    return ctx.cache["schedules"]


def role_evidence(ctx: PageContext) -> RoleEvidence:
    """Parsed counts for the role scores; None keeps part A's proxy."""
    if ctx.words.text_class not in ("cad_text", "sparse", "scanned_ocr"):
        return RoleEvidence()
    pl = _ensure_legend(ctx)
    sched = _ensure_schedules(ctx)
    items = notes.notes_item_count(_ensure_notes(ctx))
    entries = len(pl.entries) + len(pl.abbreviations)
    confident = schedules.confident_count(sched)
    return RoleEvidence(
        legend_entries=entries or None,
        legend_modifiers=len(pl.modifiers) or None,
        confident_schedules=confident if sched else None,
        notes_items=items or None,
    )


def page_notes(ctx: PageContext) -> list[dict[str, Any]]:
    return _ensure_notes(ctx)


def page_schedules(ctx: PageContext, doc: DocContext) -> tuple[list[dict[str, Any]], list[str]]:
    sched = _ensure_schedules(ctx)
    seq = int(doc.cache.get("schedule_seq", 1))
    doc.cache["schedule_seq"] = schedules.assign_ids(sched, seq)
    doc.cache.setdefault("schedules", []).extend(sched)
    return sched, [s["schedule_id"] for s in sched]


def _schedule_extent(ctx: PageContext, sched: dict[str, Any]) -> list[float]:
    """The schedule's bbox grown up to its title line (the panel header
    fields such as FED FROM: 100 A/3P @ ILL sit between the title and the
    column header, and a panel name there is not a plan tag)."""
    box = list(sched["bbox"])
    title = (sched.get("title") or "").strip().upper()
    if not title:
        return box
    reach = TITLE_REACH_IN / (ctx.words.page_h_in or 30.0)
    above = [
        ln
        for ln in ctx.words.lines
        if ln.t.strip().upper() == title
        and box[0] <= ln.cx <= box[2]
        and 0 <= box[1] - ln.b[1] <= reach
    ]
    if above:
        box[1] = round(min(ln.b[1] for ln in above), 4)
    return box


def page_table_regions(ctx: PageContext, doc: DocContext) -> list[Region]:
    """One `schedule` region per parsed schedule table (its full extent up to
    the title line, labeled with the table title) and one `legend` region per
    parsed legend table area. Heading-grown regions only reach the heading's
    column, so without these a luminaire row or a panel header inside a
    table would be counted as a plan tag."""
    out: list[Region] = [
        Region(kind="schedule", bbox=_schedule_extent(ctx, s), label=s.get("title"))
        for s in _ensure_schedules(ctx)
        if s.get("bbox")
    ]
    if ctx.words.text_class in ("cad_text", "sparse", "scanned_ocr"):
        out.extend(Region(kind="legend", bbox=list(a)) for a in _ensure_legend(ctx).areas)
    return out


def _document_legend_inputs(doc: DocContext) -> tuple[dict[int, legend.PageLegend], dict[int, Any]]:
    page_legends = {p: ctx.cache["legend"] for p, ctx in doc.pages.items() if "legend" in ctx.cache}
    plan_pages = {
        p: (ctx.words, ctx.regions)
        for p, ctx in doc.pages.items()
        if set(ctx.facts.roles) & PLAN_ROLES
    }
    return page_legends, plan_pages


def _ensure_document_legend(doc: DocContext) -> dict[str, Any]:
    if "legend_doc" not in doc.cache:
        page_legends, plan_pages = _document_legend_inputs(doc)
        doc.cache["legend_doc"] = legend.build_document_legend(page_legends, plan_pages)
    return doc.cache["legend_doc"]


def page_tags(ctx: PageContext, doc: DocContext) -> dict[str, Any]:
    all_sched = doc.cache.get("schedules") or []
    names = set(schedules.panel_names(all_sched))
    for other in doc.pages.values():
        if "panel_labels" not in other.cache:
            other.cache["panel_labels"] = tags.panel_labels_on_page(other.words)
        names.update(lab["panel"] for lab in other.cache["panel_labels"])
    doc_legend = _ensure_document_legend(doc)
    result, consumed = tags.page_tags(
        ctx.words,
        ctx.regions,
        panel_names=names,
        luminaire_tags=schedules.luminaire_tags(all_sched),
        subscripts=legend.subscript_map(doc_legend),
        status_forms=legend.declared_status_forms(doc_legend),
        notes=_ensure_notes(ctx),
    )
    ctx.cache["consumed"] = consumed
    return result


def page_keynote_markers(ctx: PageContext, doc: DocContext) -> list[dict[str, Any]]:
    if not (set(ctx.facts.roles) & PLAN_ROLES):
        return []
    return notes.keynote_markers(
        ctx.words, ctx.regions, _ensure_notes(ctx), consumed=ctx.cache.get("consumed")
    )


def document_legend(doc: DocContext) -> dict[str, Any]:
    if doc.kind != "drawing":
        return {"entries": [], "modifiers": [], "abbreviations": []}
    return _ensure_document_legend(doc)


def document_panels(
    doc: DocContext,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    all_sched = doc.cache.get("schedules") or []
    panels = schedules.panels_summary(all_sched)
    locations: list[dict[str, Any]] = []
    for p, ctx in sorted(doc.pages.items()):
        if not (set(ctx.facts.roles) & PLAN_ROLES):
            continue
        for lab in (ctx.facts.tags or {}).get("panel_labels") or []:
            locations.append(
                {
                    "panel": lab["panel"],
                    "page": p,
                    "x": lab["x"],
                    "y": lab["y"],
                    "existing": lab.get("existing", False),
                    "source": "plan_label",
                }
            )
    return panels, locations, schedules.fixture_types_summary(all_sched)


def document_specs(doc: DocContext) -> dict[str, Any] | None:
    if doc.kind != "text" or not doc.pages:
        return None
    return specs.build_spec_facts({p: ctx.words for p, ctx in doc.pages.items()})


def document_cross_trade(doc: DocContext) -> list[dict[str, Any]]:
    check = doc.cache.get("category_check")
    detected = getattr(check, "detected", None) if check is not None else None
    whole = doc.kind == "text" or (detected is not None and detected != ELECTRICAL_CATEGORY)
    return notes.cross_trade_mentions(
        {p: ctx.words for p, ctx in doc.pages.items()},
        notes_by_page={p: _ensure_notes(ctx) for p, ctx in doc.pages.items()},
        whole_document=whole,
    )


def page_block_tokens(ctx: PageContext, doc: DocContext) -> int:
    from app.core.config import get_settings

    try:
        cap = int(get_settings().plans_reader_facts_block_max_tokens)
    except Exception:  # noqa: BLE001 - settings are optional offline
        cap = block.DEFAULT_BLOCK_TOKENS
    return block.page_block_tokens(
        ctx.facts, {"schedules": doc.cache.get("schedules") or []}, max_tokens=cap
    )


def build() -> ExtractHooks:
    return ExtractHooks(
        role_evidence=role_evidence,
        notes=page_notes,
        keynote_markers=page_keynote_markers,
        schedules=page_schedules,
        table_regions=page_table_regions,
        tags=page_tags,
        legend=document_legend,
        panels=document_panels,
        specs=document_specs,
        cross_trade=document_cross_trade,
        facts_block_tokens=page_block_tokens,
    )

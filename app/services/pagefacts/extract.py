"""Document extraction orchestrator (WS1 step 8, part A).

`extract_document(pdf_bytes, label=...)` runs, per page, the text layer
(`textlayer`), regions, scales and roles, and, per document, title-block
slot learning, sheet numbers and titles, disciplines and the category check.
Part B modules (notes, legend, schedules, tags, specs, the facts block token
estimate) plug in through `ExtractHooks`; `part_b_hooks()` builds the
production set and is the default, `NO_HOOKS` gives part A alone (empty
part B fields).

Time budgets come from settings (`plans_reader_facts_page_budget_s`,
`plans_reader_facts_doc_budget_s`) unless given: a page that overruns its
budget is kept and noted; once the document budget is spent the remaining
pages are `text_class: skipped_budget` and extraction still succeeds (facts
are an enhancement; a run never fails because of them).

Pure: bytes in, `DocFacts` out. PDFium is touched only through `textlayer`
and every touch takes `pdf_split._PDFIUM_LOCK` per page, so a long document
never starves the renderers in other worker threads.
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.services import pdf_split
from app.services.pagefacts import category as category_mod
from app.services.pagefacts import regions as regions_mod
from app.services.pagefacts import roles as roles_mod
from app.services.pagefacts import scales as scales_mod
from app.services.pagefacts import textlayer
from app.services.pagefacts import titleblock as titleblock_mod
from app.services.pagefacts.contracts import (
    EXTRACTOR_VERSION,
    Census,
    DocFacts,
    PageFacts,
    PageWords,
    Region,
    TitleBlockSlot,
    View,
    empty_tags,
)

TEXT_KINDS_BY_LABEL: dict[str, str] = {
    "specifications": "text",
    "rfp": "text",
    "addenda": "text",
    "other": "text",
}


def kind_for_label(label: str | None) -> str:
    """`drawing` for every `*_drawings` label and for unlabeled documents
    (they go to the splitter, which only handles drawings), `text` for the
    file categories."""
    if label is None:
        return "drawing"
    if label.endswith("_drawings"):
        return "drawing"
    return TEXT_KINDS_BY_LABEL.get(label, "text")


@dataclass(slots=True)
class PageContext:
    page: int
    words: PageWords
    facts: PageFacts
    regions: list[Region] = field(default_factory=list)
    views: list[View] = field(default_factory=list)
    cache: dict[str, Any] = field(default_factory=dict)  # part B scratch (parsed once per page)


@dataclass(slots=True)
class DocContext:
    label: str | None
    name: str | None
    kind: str
    pages: dict[int, PageContext] = field(default_factory=dict)
    slots: list[TitleBlockSlot] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    splitter_titles: dict[int, str] = field(default_factory=dict)
    cache: dict[str, Any] = field(default_factory=dict)  # part B scratch (document level)

    def sheet_numbers(self) -> dict[int, str | None]:
        return {p: ctx.facts.sheet_number for p, ctx in self.pages.items()}


@dataclass(slots=True)
class ExtractHooks:
    """Part B plug-ins. Each receives the contexts and returns the facts it
    owns; None means "not installed" and the field stays empty.

    - `role_evidence(page)` -> `roles.RoleEvidence` (parsed legend, schedule
      and notes counts for the role scores).
    - `notes(page)` -> `PageFacts.notes`; `keynote_markers(page, doc)` runs
      after `tags` so attached circuit numbers are never markers.
    - `schedules(page, doc)` -> (`DocFacts.schedules` entries for this page,
      `PageFacts.schedule_ids`).
    - `table_regions(page, doc)` -> extra `Region`s for the extents of every
      parsed schedule and legend table; they join `PageFacts.regions` right
      after `schedules`, so tags, keynote markers and `used_on_pages` never
      count a word inside a parsed table as a plan symbol.
    - `tags(page, doc)` -> `PageFacts.tags`.
    - `legend(doc)` -> `DocFacts.legend`; `panels(doc)` -> (`panels`,
      `panel_locations`, `fixture_types`); `specs(doc)` -> `DocFacts.spec`;
      `cross_trade(doc)` -> `DocFacts.cross_trade`.
    - `facts_block_tokens(page, doc)` -> int.
    """

    role_evidence: Callable[[PageContext], roles_mod.RoleEvidence] | None = None
    notes: Callable[[PageContext], list[dict[str, Any]]] | None = None
    keynote_markers: Callable[[PageContext, DocContext], list[dict[str, Any]]] | None = None
    schedules: (
        Callable[[PageContext, DocContext], tuple[list[dict[str, Any]], list[str]]] | None
    ) = None
    table_regions: Callable[[PageContext, DocContext], list[Region]] | None = None
    tags: Callable[[PageContext, DocContext], dict[str, Any]] | None = None
    legend: Callable[[DocContext], dict[str, Any]] | None = None
    panels: (
        Callable[
            [DocContext], tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]
        ]
        | None
    ) = None
    specs: Callable[[DocContext], dict[str, Any] | None] | None = None
    cross_trade: Callable[[DocContext], list[dict[str, Any]]] | None = None
    facts_block_tokens: Callable[[PageContext, DocContext], int] | None = None


NO_HOOKS = ExtractHooks()


def part_b_hooks() -> ExtractHooks:
    """The production hook set: notes, legend, schedules, tags, keynote
    markers, specs, cross-trade mentions and the facts block tokens."""
    from app.services.pagefacts import hooks as hooks_mod

    return hooks_mod.build()


def sha256_of(pdf_bytes: bytes) -> str:
    return hashlib.sha256(pdf_bytes).hexdigest()


def _budgets(page_budget_s: float | None, doc_budget_s: float | None) -> tuple[float, float]:
    if page_budget_s is None or doc_budget_s is None:
        from app.core.config import get_settings

        s = get_settings()
        page_budget_s = (
            page_budget_s
            if page_budget_s is not None
            else float(s.plans_reader_facts_page_budget_s)
        )
        doc_budget_s = (
            doc_budget_s if doc_budget_s is not None else float(s.plans_reader_facts_doc_budget_s)
        )
    return float(page_budget_s), float(doc_budget_s)


def _skipped_page(page_no: int, w_in: float, h_in: float, rotation: int) -> PageFacts:
    return PageFacts(
        page=page_no,
        w_in=w_in,
        h_in=h_in,
        rotation=rotation,
        text_class="skipped_budget",
        census=Census(hazards=textlayer.empty_hazards()),
        problems=["skipped: document time budget spent"],
    )


def add_table_regions(ctx: PageContext, extra: list[Region]) -> None:
    """Append parsed-table regions to the page (before the plan area entry,
    which stays last); a bbox already present is not repeated. `ctx.regions`
    and `ctx.facts.regions` are the same list."""
    if not extra:
        return
    regions = ctx.regions
    seen = {(r.kind, tuple(r.bbox)) for r in regions}
    at = next((i for i, r in enumerate(regions) if r.kind == "plan_area"), len(regions))
    for r in extra:
        key = (r.kind, tuple(r.bbox))
        if key in seen:
            continue
        seen.add(key)
        regions.insert(at, r)
        at += 1
    ctx.facts.regions = regions


def _safe(
    hook: Callable[..., Any] | None, default: Any, problems: list[str], what: str, *args: Any
) -> Any:
    """Run a part B hook; a failing hook records a problem and yields its
    default so one parser can never sink the document."""
    if hook is None:
        return default
    try:
        out = hook(*args)
    except Exception as exc:  # noqa: BLE001 - hooks are plug-ins
        problems.append(f"{what}: {type(exc).__name__}: {exc}"[:300])
        return default
    return default if out is None else out


def extract_words(
    pdf_bytes: bytes, *, pages: list[int] | None = None, objects: bool = True
) -> dict[int, PageWords]:
    """`PageWords` per 1-based page (every page when `pages` is None)."""
    out: dict[int, PageWords] = {}
    with textlayer.open_document(pdf_bytes) as doc:
        with pdf_split._PDFIUM_LOCK:
            total = len(doc)
        wanted = pages or list(range(1, total + 1))
        for p in wanted:
            if not 1 <= p <= total:
                raise ValueError(f"Page {p} is outside the document (1-{total}).")
            with pdf_split._PDFIUM_LOCK:
                page = doc[p - 1]
                try:
                    out[p] = textlayer.extract_page(page, page_number=p, objects=objects)
                finally:
                    page.close()
    return out


def extract_document(
    pdf_bytes: bytes,
    *,
    label: str | None = None,
    name: str | None = None,
    kind: str | None = None,
    splitter_titles: dict[int, str] | None = None,
    hooks: ExtractHooks | None = None,
    page_budget_s: float | None = None,
    doc_budget_s: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> DocFacts:
    """Facts for one document. `label` is the run label of the document
    (`electrical_drawings`, `specifications`, ...), `splitter_titles` maps a
    1-based page to the splitter's page title for the splitter fallback."""
    hooks = hooks if hooks is not None else part_b_hooks()
    kind = kind or kind_for_label(label)
    page_budget, doc_budget = _budgets(page_budget_s, doc_budget_s)
    started = clock()
    sha = sha256_of(pdf_bytes)
    doc_ctx = DocContext(
        label=label, name=name, kind=kind, splitter_titles=dict(splitter_titles or {})
    )
    page_facts: dict[int, PageFacts] = {}
    page_times: dict[int, float] = {}
    budget_hit = False

    # Phase 1: the text layer per page, under the time budgets.
    with textlayer.open_document(pdf_bytes) as doc:
        with pdf_split._PDFIUM_LOCK:
            total = len(doc)
        for p in range(1, total + 1):
            t0 = clock()
            with pdf_split._PDFIUM_LOCK:
                page = doc[p - 1]
                try:
                    if t0 - started > doc_budget:
                        frame = textlayer.page_frame(page)
                        page_facts[p] = _skipped_page(
                            p, round(frame.w_in, 4), round(frame.h_in, 4), frame.rotation
                        )
                        budget_hit = True
                        continue
                    try:
                        pw = textlayer.extract_page(page, page_number=p)
                    except Exception as exc:  # noqa: BLE001 - a broken page never sinks the document
                        frame = textlayer.page_frame(page)
                        facts = _skipped_page(
                            p, round(frame.w_in, 4), round(frame.h_in, 4), frame.rotation
                        )
                        facts.text_class = "empty"
                        facts.problems = [f"Page {p}: text layer unreadable ({type(exc).__name__})"]
                        page_facts[p] = facts
                        doc_ctx.problems.append(facts.problems[0])
                        continue
                finally:
                    page.close()
            dt = clock() - t0
            page_times[p] = dt
            facts = PageFacts(
                page=p,
                w_in=pw.page_w_in,
                h_in=pw.page_h_in,
                rotation=pw.rotation,
                text_class=pw.text_class,
                census=pw.census,
            )
            if dt > page_budget:
                budget_hit = True
                facts.problems.append(
                    f"Page {p}: text layer took {dt:.1f} s (budget {page_budget:.0f} s)"
                )
            doc_ctx.pages[p] = PageContext(page=p, words=pw, facts=facts)
            page_facts[p] = facts

    # Phase 2: document-level title-block slots (drawings only).
    slots: list[TitleBlockSlot] = []
    if kind == "drawing" and doc_ctx.pages:
        slots = titleblock_mod.learn_slots({p: ctx.words for p, ctx in doc_ctx.pages.items()})
    doc_ctx.slots = slots

    # Phase 3: per page facts.
    for p, ctx in doc_ctx.pages.items():
        t0 = clock()
        pw, facts = ctx.words, ctx.facts
        slot_xy: tuple[float, float] | None = None
        if kind == "drawing":
            number, source, cand = titleblock_mod.sheet_number_for_page(
                pw,
                slots,
                splitter_title=doc_ctx.splitter_titles.get(p),
                single_page=len(doc_ctx.pages) == 1,
            )
            facts.sheet_number, facts.sheet_number_source = number, source
            if cand is not None:
                slot_xy = (cand.cx, cand.cy)
            elif slots:
                slot_xy = (slots[0].x, slots[0].y)
        ctx.regions = regions_mod.find_regions(pw, slot_xy=slot_xy)
        facts.regions = ctx.regions
        if kind == "drawing":
            tb = next((r for r in ctx.regions if r.kind == "title_block"), None)
            title = titleblock_mod.sheet_title(pw, slot_xy, tb, sheet_number=facts.sheet_number)
            if title:
                facts.sheet_title, facts.title_source = (
                    title,
                    facts.sheet_number_source if facts.sheet_number_source != "none" else "label",
                )
            elif doc_ctx.splitter_titles.get(p):
                facts.sheet_title, facts.title_source = doc_ctx.splitter_titles[p], "splitter"
            facts.revision = titleblock_mod.revision(pw, ctx.regions)
            views, primary, conflict, notes = scales_mod.find_views(pw, ctx.regions)
            ctx.views = views
            facts.views, facts.primary_ft_per_in, facts.scale_conflict, facts.scale_notes = (
                views,
                primary,
                conflict,
                notes,
            )
            evidence = _safe(
                hooks.role_evidence, None, doc_ctx.problems, f"Page {p} role evidence", ctx
            )
            facts.roles, facts.page_role, facts.role_scores = roles_mod.score_roles(
                facts.sheet_title, pw, ctx.regions, views, evidence=evidence
            )
            if facts.sheet_number is None and pw.text_class in ("cad_text", "sparse"):
                doc_ctx.problems.append(f"Page {p}: no sheet number found")
        else:
            facts.roles, facts.page_role, facts.role_scores = (
                ["spec_text"],
                "spec_text",
                {"spec_text": 3},
            )
        facts.notes = _safe(hooks.notes, [], doc_ctx.problems, f"Page {p} notes", ctx)
        facts.timing_ms = int(round((page_times.get(p, 0.0) + (clock() - t0)) * 1000))

    # Phase 4: disciplines and the category check (drawings only).
    schedules: list[dict[str, Any]] = []
    if kind == "drawing":
        for p, ctx in doc_ctx.pages.items():
            sched, ids = _safe(
                hooks.schedules, ([], []), doc_ctx.problems, f"Page {p} schedules", ctx, doc_ctx
            )
            schedules.extend(sched)
            ctx.facts.schedule_ids = list(ids)
            add_table_regions(
                ctx,
                _safe(
                    hooks.table_regions,
                    [],
                    doc_ctx.problems,
                    f"Page {p} table regions",
                    ctx,
                    doc_ctx,
                ),
            )
        prefixes = Counter(
            pfx
            for pfx in (
                category_mod.prefix_of(ctx.facts.sheet_number) for ctx in doc_ctx.pages.values()
            )
            if pfx
        )
        pairs: list[tuple[str | None, str]] = []
        for ctx in doc_ctx.pages.values():
            disc, cat = category_mod.discipline_of(ctx.facts.sheet_number, doc_prefixes=prefixes)
            ctx.facts.discipline = disc
            ctx.facts.discipline_category = cat if cat != category_mod.UNKNOWN else None
            if ctx.facts.sheet_number:
                pairs.append((disc, cat))
        check = category_mod.category_check(label, pairs, name=name)
        doc_ctx.cache["category_check"] = check
        for p, ctx in doc_ctx.pages.items():
            ctx.facts.tags = _safe(
                hooks.tags, empty_tags(), doc_ctx.problems, f"Page {p} tags", ctx, doc_ctx
            )
            ctx.facts.keynote_markers = _safe(
                hooks.keynote_markers,
                [],
                doc_ctx.problems,
                f"Page {p} keynote markers",
                ctx,
                doc_ctx,
            )
    else:
        check = None

    legend = _safe(hooks.legend, None, doc_ctx.problems, "legend", doc_ctx)
    panels, panel_locations, fixture_types = _safe(
        hooks.panels, ([], [], []), doc_ctx.problems, "panels", doc_ctx
    )
    spec = _safe(hooks.specs, None, doc_ctx.problems, "specs", doc_ctx) if kind == "text" else None
    cross_trade = _safe(hooks.cross_trade, [], doc_ctx.problems, "cross trade", doc_ctx)
    for p, ctx in doc_ctx.pages.items():
        ctx.facts.facts_block_tokens = int(
            _safe(
                hooks.facts_block_tokens, 0, doc_ctx.problems, f"Page {p} facts block", ctx, doc_ctx
            )
        )

    times = sorted(page_times.values())
    p90 = times[min(len(times) - 1, int(round(0.9 * (len(times) - 1))))] if times else 0.0
    facts = DocFacts(
        sha256=sha,
        kind=kind,
        page_count=len(page_facts),
        name=name,
        label=label,
        extractor_version=EXTRACTOR_VERSION,
        title_block_slots=slots,
        category_check=check,
        legend=legend
        if legend is not None
        else {"entries": [], "modifiers": [], "abbreviations": []},
        schedules=schedules,
        panels=panels,
        panel_locations=panel_locations,
        fixture_types=fixture_types,
        spec=spec,
        cross_trade=cross_trade,
        problems=doc_ctx.problems,
        stats={
            "pages": len(page_facts),
            "ms_total": int(round((clock() - started) * 1000)),
            "ms_p90_page": int(round(p90 * 1000)),
            "budget_hit": budget_hit,
            "text_classes": dict(Counter(f.text_class for f in page_facts.values())),
        },
        pages=[page_facts[p] for p in sorted(page_facts)],
    )
    return facts


def page_words_of(doc_ctx: DocContext, page: int) -> PageWords | None:
    ctx = doc_ctx.pages.get(page)
    return ctx.words if ctx else None

"""Page facts (WS1): deterministic, cached, per-document extraction of the PDF
text layer into typed facts (positioned words and lines, title-block sheet
numbers and titles, per-view scales, multi-label page roles, the document
category check) that feed the run ledger, the sub prompts, the tool loop,
census and measurement. No model is involved anywhere in this package.

Layout (spec section 6 WS1, v1 cut):
- `contracts`: the dataclasses and JSON shapes every other workstream reads.
- `textlayer`: the page frame transform, census, words, lines, the char stream
  and the SHX annotation harvest (the only module that touches PDFium).
- `regions`, `titleblock`, `scales`, `roles`, `category`: pure functions over
  `PageWords` (part A).
- `extract`: orchestrates part A per page with time budgets; part B modules
  (notes, legend, schedules, tags, specs) plug in through `ExtractHooks`.
- `store`: cache key, blob layout and an in-memory store (the DB store lands
  in wave 2).

Coordinates everywhere are page fractions of the displayed page (after
/Rotate, inside the CropBox), origin top-left, 4 decimals (spec 5.1).
"""

from app.services.pagefacts.contracts import (
    EXTRACTOR_VERSION,
    FACTS_SCHEMA,
    DocFacts,
    FactsUnavailable,
    Line,
    PageFacts,
    PageWords,
    Word,
)

# Every WS1 setting of spec 5.7 (flags, time budgets and token caps), in
# the order of the table. `plans_reader_runs._settings_snapshot` merges
# `settings_snapshot(s)` so a run records whether and how it was planned
# with page facts.
PAGE_FACTS_SETTINGS: tuple[str, ...] = (
    "plans_reader_page_facts_enabled",
    "plans_reader_page_facts_in_subs",
    "plans_reader_spec_select",
    "plans_reader_facts_relabel",
    "plans_reader_facts_title_vision",
    "plans_reader_facts_page_budget_s",
    "plans_reader_facts_doc_budget_s",
    "plans_reader_facts_block_max_tokens",
    "plans_reader_ledger_legend_max_tokens",
    "plans_reader_ledger_schedule_max_tokens",
    "plans_reader_ledger_scales_max_tokens",
)


def settings_snapshot(s: object) -> dict[str, object]:
    """The page-facts flags and caps of a Settings object (None for a field
    it lacks, so a plain namespace works), plus the extractor version the
    facts cache is keyed by."""
    out: dict[str, object] = {name: getattr(s, name, None) for name in PAGE_FACTS_SETTINGS}
    out["page_facts_extractor_version"] = EXTRACTOR_VERSION
    return out


__all__ = [
    "PAGE_FACTS_SETTINGS",
    "settings_snapshot",
    "EXTRACTOR_VERSION",
    "FACTS_SCHEMA",
    "DocFacts",
    "FactsUnavailable",
    "Line",
    "PageFacts",
    "PageWords",
    "Word",
]

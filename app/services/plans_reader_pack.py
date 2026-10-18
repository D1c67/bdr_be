"""Group packing and the pre-run cost estimate for AI Estimating.

Why this module exists: a run's pages are planned into model calls before
anything is rendered, so the New run page can show the cost up front and the
run can refuse to start over its cap. Each group (a `tiered_plans_agent`, one
plans_reader_groups row and one llm_jobs job) must stay under the group
input budget (default 240,000 tokens, safely below the 272,000 line where
OpenAI bills the whole request at 2x input) including the ledger and the
prompt, and under the 50 MB `input_file` cap (docs/PLANS_READER.md 3.4).

Token estimates come from the patch formula in plans_reader_images for
drawings (exact, since planning mirrors the PDFium render size) and from the
measured 1,100 tokens per page for PDFs sent as `input_file`.

Pure functions only: no database, no network, no settings import. Prices are
parameters so this module does not depend on services/llm_pricing.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

from app.services import plans_reader_images as images

FILE_TOKENS_PER_PAGE = 1_100
DEFAULT_GROUP_BUDGET = 240_000
DEFAULT_PROMPT_TOKENS = 3_000
LONG_CONTEXT_THRESHOLD = 272_000
LONG_CONTEXT_INPUT_MULT = 2.0
LONG_CONTEXT_OUTPUT_MULT = 1.5
# Output estimates (docs/PLANS_READER.md section 1: 766 to 3,224 output
# tokens per sheet on a real count prompt at medium effort). A spec page
# yields far less (products and requirements, not counts). One call can
# never return more than the model's 128,000 output tokens.
OUTPUT_TOKENS_PER_SHEET = 2_500
OUTPUT_TOKENS_PER_FILE_PAGE = 150
MIN_OUTPUT_TOKENS_PER_CALL = 4_000
MAX_OUTPUT_TOKENS_PER_CALL = 128_000

RenderMode = Literal["adaptive", "tiles_260"]
DocKind = Literal["drawing", "file"]


@dataclass
class PagePlan:
    """One drawing page as it will be sent. `tiles` holds pixel boxes at
    `ppi` (None means the whole page as one image)."""

    page: int
    w_in: float
    h_in: float
    ppi: int
    w_px: int
    h_px: int
    est_tokens: int
    tiles: list[images.Box] | None = None


@dataclass
class FileChunk:
    """A page span of a PDF sent as one `input_file`. `bytes` is the real
    chunk size when it came from pdf_chunks, or a pro-rated estimate."""

    start_page: int
    end_page: int
    bytes: int
    est_tokens: int

    @property
    def pages(self) -> int:
        """Number of pages in the span (inclusive)."""
        return self.end_page - self.start_page + 1


@dataclass
class PackDoc:
    """One plans_reader_documents row, planned. Drawings carry `pages`;
    files carry `chunks`, `page_count` and `file_bytes`."""

    document_id: str
    category: str
    kind: DocKind
    pages: list[PagePlan] = field(default_factory=list)
    chunks: list[FileChunk] = field(default_factory=list)
    page_count: int = 0
    file_bytes: int = 0

    @property
    def est_tokens(self) -> int:
        """Estimated input tokens of the whole document (no ledger or prompt)."""
        if self.kind == "file":
            return sum(c.est_tokens for c in self.chunks)
        return sum(p.est_tokens for p in self.pages)


@dataclass
class Group:
    """One model call of one category. `items` match the
    plans_reader_groups.pages contract (one entry per page; tiles as page
    fractions). `file_ranges` lists (document_id, start_page, end_page,
    bytes) for the `input_file` parts, in order, so the caller can cut
    exactly those spans. `images` counts the image parts (a tiled sheet is
    one per tile), ledger images not included."""

    category: str
    group_index: int
    items: list[dict[str, Any]] = field(default_factory=list)
    est_input_tokens: int = 0
    file_bytes: int = 0
    oversize: bool = False
    file_ranges: list[tuple[str, int, int, int]] = field(default_factory=list)
    images: int = 0


# Document planning ------------------------------------------------------------


def plan_page(
    page: int,
    w_in: float,
    h_in: float,
    *,
    render_mode: RenderMode = "adaptive",
    tile_ppi: int = 260,
    max_patches: int = images.MAX_PATCHES_ORIGINAL,
    overlap: float = images.DEFAULT_TILE_OVERLAP,
) -> PagePlan:
    """Plan one drawing page: adaptive is one image at the largest ppi under
    the patch cap; tiles_260 is a grid of tiles at `tile_ppi`."""
    if render_mode == "adaptive":
        ppi = images.native_ppi_for(w_in, h_in, max_patches=max_patches)
        w_px, h_px = images.px_for_inches(w_in, ppi), images.px_for_inches(h_in, ppi)
        return PagePlan(page, w_in, h_in, ppi, w_px, h_px, images.image_tokens(w_px, h_px))
    if render_mode == "tiles_260":
        w_px, h_px = images.px_for_inches(w_in, tile_ppi), images.px_for_inches(h_in, tile_ppi)
        tiles = images.plan_tiles(w_px, h_px, max_patches=max_patches, overlap=overlap)
        est = sum(images.image_tokens(x1 - x0, y1 - y0) for (x0, y0, x1, y1) in tiles)
        return PagePlan(page, w_in, h_in, tile_ppi, w_px, h_px, est, tiles)
    raise ValueError(f"Unknown render mode '{render_mode}'.")


def plan_document(
    document_id: str,
    category: str,
    page_sizes_in: list[tuple[float, float]],
    *,
    render_mode: RenderMode = "adaptive",
    tile_ppi: int = 260,
    max_patches: int = images.MAX_PATCHES_ORIGINAL,
) -> PackDoc:
    """Plan a drawing document from its page sizes in inches (1-based pages
    in order)."""
    if not page_sizes_in:
        raise ValueError("The document has no pages.")
    pages = [
        plan_page(
            i,
            w,
            h,
            render_mode=render_mode,
            tile_ppi=tile_ppi,
            max_patches=max_patches,
        )
        for i, (w, h) in enumerate(page_sizes_in, start=1)
    ]
    return PackDoc(document_id, category, "drawing", pages=pages, page_count=len(pages))


def plan_file_document(
    document_id: str,
    category: str,
    page_count: int,
    file_bytes: int,
    *,
    chunks: list[tuple[int, int, int | bytes]] | None = None,
    max_bytes: int = images.FILE_MAX_BYTES,
    max_pages: int = images.FILE_MAX_PAGES,
    tokens_per_page: int = FILE_TOKENS_PER_PAGE,
) -> PackDoc:
    """Plan a PDF sent as `input_file`. `chunks` are the (start_page,
    end_page, data) tuples plans_reader_images.pdf_chunks returns when the
    file was already cut; the third element may be the chunk's bytes or its
    length. Otherwise spans are estimated by pages and pro-rated bytes under
    the same caps (the cost preview runs before any cutting). Chunk
    boundaries do not change the group count: pack_groups fills each
    group's remaining room from the next chunk."""
    if page_count <= 0:
        raise ValueError("The document has no pages.")
    if chunks is None:
        per_page = file_bytes / page_count
        span = max_pages
        if per_page > 0:
            span = max(1, min(max_pages, math.floor(max_bytes / per_page)))
        chunks = []
        start = 1
        while start <= page_count:
            end = min(page_count, start + span - 1)
            chunks.append((start, end, round(per_page * (end - start + 1))))
            start = end + 1
    planned = [
        FileChunk(
            s,
            e,
            len(b) if isinstance(b, (bytes, bytearray, memoryview)) else int(b),
            (e - s + 1) * tokens_per_page,
        )
        for (s, e, b) in chunks
    ]
    return PackDoc(
        document_id,
        category,
        "file",
        chunks=planned,
        page_count=page_count,
        file_bytes=file_bytes,
    )


def max_file_pages_for_budget(
    budget_tokens: int,
    ledger_tokens: int,
    *,
    prompt_tokens: int = DEFAULT_PROMPT_TOKENS,
    tokens_per_page: int = FILE_TOKENS_PER_PAGE,
) -> int:
    """Most `input_file` pages one group can carry. Pass it as `max_pages`
    to pdf_chunks so real chunks never need a further split when packed."""
    room = budget_tokens - ledger_tokens - prompt_tokens
    return max(1, room // tokens_per_page)


# Packing ----------------------------------------------------------------------


def _image_item(document_id: str, plan: PagePlan) -> dict[str, Any]:
    """plans_reader_groups.pages entry for a drawing page, tiles converted
    from pixel boxes to page fractions so they survive any render ppi."""
    tiles = None
    if plan.tiles is not None:
        tiles = [images.box_to_fractions(b, plan.w_px, plan.h_px) for b in plan.tiles]
    return {
        "document_id": document_id,
        "page": plan.page,
        "ppi": plan.ppi,
        "detail": "original",
        "tiles": tiles,
        "kind": "image",
    }


def _file_items(document_id: str, start: int, end: int) -> list[dict[str, Any]]:
    """plans_reader_groups.pages entries for an `input_file` span, one per
    page. A file page is not rendered, so ppi, detail and tiles are None."""
    return [
        {
            "document_id": document_id,
            "page": p,
            "ppi": None,
            "detail": None,
            "tiles": None,
            "kind": "file",
        }
        for p in range(start, end + 1)
    ]


def _prorate(total: int, pages: int, a: int, b: int) -> int:
    """Share of `total` for pages a..b-1 (0-based offsets) of a `pages`-page
    chunk. Cumulative rounding, so the pieces of a chunk add up to exactly
    its total."""
    return round(total * b / pages) - round(total * a / pages)


def _span_fits(chunk: FileChunk, done: int, k: int, tok_left: int, bytes_left: int) -> bool:
    """Pages done..done+k-1 (0-based offsets) of `chunk` fit in `tok_left`
    tokens and `bytes_left` bytes."""
    return (
        _prorate(chunk.est_tokens, chunk.pages, done, done + k) <= tok_left
        and _prorate(chunk.bytes, chunk.pages, done, done + k) <= bytes_left
    )


def pack_groups(
    docs: list[PackDoc],
    *,
    budget_tokens: int = DEFAULT_GROUP_BUDGET,
    ledger_tokens: int,
    prompt_tokens: int = DEFAULT_PROMPT_TOKENS,
    file_max_bytes: int = images.FILE_MAX_BYTES,
    max_images: int = images.REQUEST_MAX_IMAGES,
    ledger_images: int = 0,
) -> list[Group]:
    """Pack documents into groups, per category, in document order.

    Greedy and order-preserving: pages fill the current group until the
    next one would push it over `budget_tokens` (ledger and prompt
    included), over `file_max_bytes` of `input_file` data, or over
    `max_images - ledger_images` image parts, then a new group starts. A
    drawing page is never split across groups; one whose own tokens (or
    tile count) exceed the room gets a group to itself with
    `oversize=True`. A file chunk that does not fit whole is cut at a page
    boundary only when it is bigger than an empty group: the first piece
    fills the open group's remaining room and the rest carries on, so a
    long spec book takes the fewest groups whatever its chunk boundaries
    (bytes and tokens pro-rated per page; the caller cuts each
    `file_ranges` span itself). A chunk (or remainder) that fits a fresh
    group whole starts one instead of being spread across two. A chunk already
    over `file_max_bytes` raises ValueError. Categories appear in order of
    first appearance; `group_index` restarts at 0 per category."""
    overhead = ledger_tokens + prompt_tokens
    room = budget_tokens - overhead
    if room <= 0:
        raise ValueError("The ledger and prompt leave no room in the group budget.")
    image_cap = max_images - ledger_images
    if image_cap < 1:
        raise ValueError("The ledger images leave no room for page images in a request.")

    by_cat: dict[str, list[PackDoc]] = {}
    for d in docs:
        by_cat.setdefault(d.category, []).append(d)

    groups: list[Group] = []
    for category, cat_docs in by_cat.items():
        current: Group | None = None
        page_tokens = 0
        index = 0

        def close() -> None:
            """Finish the open group (if it holds anything) and start fresh."""
            nonlocal current, page_tokens, index
            if current is not None and current.items:
                current.est_input_tokens = overhead + page_tokens
                groups.append(current)
                index += 1
            current = None
            page_tokens = 0

        def ensure() -> Group:
            """The open group, created on first use."""
            nonlocal current
            if current is None:
                current = Group(category, index)
            return current

        for doc in cat_docs:
            if doc.kind == "drawing":
                for plan in doc.pages:
                    n_img = len(plan.tiles) if plan.tiles else 1
                    if plan.est_tokens > room or n_img > image_cap:
                        close()
                        g = ensure()
                        g.items.append(_image_item(doc.document_id, plan))
                        g.images = n_img
                        g.oversize = True
                        page_tokens = plan.est_tokens
                        close()
                        continue
                    if current is not None and (
                        page_tokens + plan.est_tokens > room or current.images + n_img > image_cap
                    ):
                        close()
                    g = ensure()
                    g.items.append(_image_item(doc.document_id, plan))
                    g.images += n_img
                    page_tokens += plan.est_tokens
                continue
            for raw in doc.chunks:
                if raw.bytes > file_max_bytes:
                    mb = file_max_bytes / (1024 * 1024)
                    raise ValueError(
                        f"Pages {raw.start_page}-{raw.end_page} of a document are "
                        f"over {mb:.0f} MB and must be cut smaller first."
                    )
                n_pages = raw.pages
                done = 0  # pages of this chunk already placed
                while done < n_pages:
                    used_bytes = current.file_bytes if current is not None else 0
                    tok_left = room - page_tokens
                    bytes_left = file_max_bytes - used_bytes
                    left = n_pages - done
                    n = left
                    if (
                        current is not None
                        and not _span_fits(raw, done, left, tok_left, bytes_left)
                        and _span_fits(raw, done, left, room, file_max_bytes)
                    ):
                        # The rest fits a fresh group whole: keep it in one
                        # piece rather than spreading a short document.
                        close()
                        continue
                    if not _span_fits(raw, done, n, tok_left, bytes_left):
                        # Bigger than any one group: fill this one. Start from the per-page rate, then settle on the
                        # exact largest count (cumulative rounding can move
                        # it by one page either way).
                        n = left - 1
                        if raw.est_tokens > 0:
                            n = min(n, int(tok_left * n_pages / raw.est_tokens))
                        if raw.bytes > 0:
                            n = min(n, int(bytes_left * n_pages / raw.bytes))
                        n = max(0, n)
                        while n > 0 and not _span_fits(raw, done, n, tok_left, bytes_left):
                            n -= 1
                        while n + 1 < left and _span_fits(raw, done, n + 1, tok_left, bytes_left):
                            n += 1
                    oversize = False
                    if n == 0:
                        if current is not None and current.items:
                            close()
                            continue
                        # An empty group cannot take even one page: the page
                        # alone is over the token room. Send it by itself.
                        n, oversize = 1, True
                    piece_tokens = _prorate(raw.est_tokens, n_pages, done, done + n)
                    piece_bytes = _prorate(raw.bytes, n_pages, done, done + n)
                    s_page = raw.start_page + done
                    e_page = s_page + n - 1
                    g = ensure()
                    g.items.extend(_file_items(doc.document_id, s_page, e_page))
                    g.file_ranges.append((doc.document_id, s_page, e_page, piece_bytes))
                    g.file_bytes += piece_bytes
                    page_tokens += piece_tokens
                    done += n
                    if oversize:
                        g.oversize = True
                        close()
        close()
    return groups


# Cost estimate ----------------------------------------------------------------


def _call_cost(
    input_tokens: int,
    output_tokens: int,
    input_per_m: float,
    output_per_m: float,
    long_context_threshold: int,
) -> float:
    """Dollar cost of one call, with OpenAI's long-context surcharge (2x
    input, 1.5x output on the whole request) above the threshold."""
    in_mult = out_mult = 1.0
    if input_tokens > long_context_threshold:
        in_mult, out_mult = LONG_CONTEXT_INPUT_MULT, LONG_CONTEXT_OUTPUT_MULT
    return (
        input_tokens * input_per_m * in_mult / 1_000_000
        + output_tokens * output_per_m * out_mult / 1_000_000
    )


def group_output_tokens(
    group: Group,
    *,
    per_sheet: int = OUTPUT_TOKENS_PER_SHEET,
    per_file_page: int = OUTPUT_TOKENS_PER_FILE_PAGE,
    minimum: int = MIN_OUTPUT_TOKENS_PER_CALL,
    maximum: int = MAX_OUTPUT_TOKENS_PER_CALL,
) -> int:
    """Estimated output tokens (reasoning included) of one sub agent group:
    `per_sheet` per drawing page plus `per_file_page` per `input_file` page,
    kept between `minimum` and the model's output cap."""
    sheets = sum(1 for i in group.items if i.get("kind") == "image")
    file_pages = len(group.items) - sheets
    return max(minimum, min(maximum, sheets * per_sheet + file_pages * per_file_page))


def estimate_run(
    docs: list[PackDoc],
    *,
    budget_tokens: int = DEFAULT_GROUP_BUDGET,
    ledger_tokens: int,
    input_per_m: float,
    output_per_m: float,
    prompt_tokens: int = DEFAULT_PROMPT_TOKENS,
    file_max_bytes: int = images.FILE_MAX_BYTES,
    ledger_images: int = 0,
    output_tokens_per_group: int | None = None,
    output_tokens_per_sheet: int = OUTPUT_TOKENS_PER_SHEET,
    output_tokens_per_file_page: int = OUTPUT_TOKENS_PER_FILE_PAGE,
    main_overhead_tokens: int = 200_000,
    main_output_tokens: int = 30_000,
    escalation_max_fraction: float = 0.5,
    long_context_threshold: int = LONG_CONTEXT_THRESHOLD,
) -> dict[str, Any]:
    """Pre-run estimate of every call the run makes.

    - Sub agent groups: packed by pack_groups. Output per group is
      `output_tokens_per_sheet` per drawing page plus
      `output_tokens_per_file_page` per spec page (group_output_tokens), or
      a flat `output_tokens_per_group` when given.
    - Roll-up: one text-only call for every category with more than one
      group; input is its groups' outputs plus the prompt, output is the
      sum of those outputs (a merge rarely shrinks much), capped at the
      model's output limit.
    - Main agent: input is the ledger, the prompt and every category output
      (roll-up output or the single group's output), never less than
      `main_overhead_tokens`; output is `main_output_tokens`.
    - Any call over `long_context_threshold` input tokens is priced at 2x
      input and 1.5x output, as OpenAI bills it.

    Escalation is not in `cost_usd`: it only runs when a sub agent flags a
    region. Its ceiling (`escalation_max_fraction` of the first-pass sub
    input, PLANS_READER_ESCALATION_MAX_FRACTION, with output in the same
    proportion) is returned as `escalation_max_input_tokens` /
    `escalation_max_output_tokens`, and `cost_usd_max` adds it, so a cap
    check can use the worst case.

    Returns {groups, input_tokens, output_tokens, cost_usd, cost_usd_max}
    plus a breakdown (sub, rollup, main and escalation tokens, per category,
    oversize groups)."""
    if not 0 <= escalation_max_fraction <= 1:
        raise ValueError("The escalation fraction must be between 0 and 1.")
    groups = pack_groups(
        docs,
        budget_tokens=budget_tokens,
        ledger_tokens=ledger_tokens,
        prompt_tokens=prompt_tokens,
        file_max_bytes=file_max_bytes,
        ledger_images=ledger_images,
    )
    cost = 0.0
    sub_in = sub_out = 0
    per_cat: dict[str, dict[str, int]] = {}
    for g in groups:
        if output_tokens_per_group is not None:
            g_out = output_tokens_per_group
        else:
            g_out = group_output_tokens(
                g, per_sheet=output_tokens_per_sheet, per_file_page=output_tokens_per_file_page
            )
        sub_in += g.est_input_tokens
        sub_out += g_out
        cost += _call_cost(g.est_input_tokens, g_out, input_per_m, output_per_m, long_context_threshold)
        row = per_cat.setdefault(
            g.category, {"groups": 0, "input_tokens": 0, "output_tokens": 0}
        )
        row["groups"] += 1
        row["input_tokens"] += g.est_input_tokens
        row["output_tokens"] += g_out

    rollup_in = rollup_out = 0
    category_outputs = 0
    for row in per_cat.values():
        if row["groups"] > 1:
            r_in = row["output_tokens"] + prompt_tokens
            r_out = min(MAX_OUTPUT_TOKENS_PER_CALL, row["output_tokens"])
            rollup_in += r_in
            rollup_out += r_out
            category_outputs += r_out
            cost += _call_cost(r_in, r_out, input_per_m, output_per_m, long_context_threshold)
        else:
            category_outputs += row["output_tokens"]

    main_in = main_out = 0
    if groups:
        main_in = max(main_overhead_tokens, ledger_tokens + prompt_tokens + category_outputs)
        main_out = main_output_tokens
        cost += _call_cost(main_in, main_out, input_per_m, output_per_m, long_context_threshold)

    # Escalation calls are one sheet each, far under the long-context line.
    esc_in = round(sub_in * escalation_max_fraction)
    esc_out = round(sub_out * escalation_max_fraction)
    esc_cost = (esc_in * input_per_m + esc_out * output_per_m) / 1_000_000

    return {
        "groups": len(groups),
        "input_tokens": sub_in + rollup_in + main_in,
        "output_tokens": sub_out + rollup_out + main_out,
        "cost_usd": round(cost, 2),
        "cost_usd_max": round(cost + esc_cost, 2),
        "sub_input_tokens": sub_in,
        "sub_output_tokens": sub_out,
        "rollup_input_tokens": rollup_in,
        "rollup_output_tokens": rollup_out,
        "main_input_tokens": main_in,
        "main_output_tokens": main_out,
        "escalation_max_input_tokens": esc_in,
        "escalation_max_output_tokens": esc_out,
        "oversize_groups": sum(1 for g in groups if g.oversize),
        "per_category": per_cat,
    }

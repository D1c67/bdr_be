"""Cost previews and state reads for the assistant's AI Estimating tools
(docs/ASSISTANT_PHASE4_PLAN.md 3.5.1, 3.5.4, 3.6.1, 3.6.4).

Filled by WI-2. The public names and signatures below are binding: WI-9a's
routes and WI-11's `plans_tasks.advance` code against them. `app/assistant/`
never queries AE tables; it reads AE state only through `run_state`,
`chat_message_state` and `adopt_page_read_message`.

AE modules are called by name, never edited (plan 3.23):
`plans_reader_images.page_sizes_in`, `native_ppi_for`, `image_tokens`,
`px_for_inches`; `plans_reader_pack.estimate_run`, `plan_page`,
`plan_file_document`, `PackDoc`; `plans_reader_chat.MAX_ROUNDS`,
`redact_messages`; `plans_reader_runs.PROJECT_CATEGORY_LABELS`,
`REFUSED_PROJECT_CATEGORIES`; `llm_pricing.price_for`. The run preview also
reads the public `plans_reader_runs.spine_enabled` and `loop_budget_usd_max`
and the planning constants of `plans_reader_agents` (`prompt_tokens`,
`DRAWING_CATEGORIES`, `LEDGER_MAX_IMAGES`) so its numbers match
`plans_reader_runs.estimate`.

Money in every returned dict is a string with 2 decimals (the phase 2 rule:
no floats in cards). Estimates round half up, holds and actual costs round
up, remaining amounts round down, so a card never shows more room than there
is. Every function is plain `def` over the sync Supabase client.

Problems are `{code, message}` dicts (plus `file_id` for a file problem).
Codes: page reads `run_not_finished`, `document_not_in_run`, `pages_invalid`,
`too_many_pages`, `page_size_unknown`, `long_context`, `unpriced_model`; run
previews `no_files`, `file_refused_category`, `file_unreadable_type`,
`file_unreadable`, `too_many_files`, `too_many_pages`,
`unlabeled_file_too_long`, `too_many_unlabeled_files`, `unpriced_model`,
`split_unpriced`.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import OrderedDict
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.core.config import get_settings
from app.services import plans_reader_images as images
from app.services import plans_reader_pack as pack
from app.services import storage

logger = logging.getLogger(__name__)

_CENT = Decimal("0.01")
_ZERO = Decimal("0")
_MILLION = Decimal(1_000_000)

FINISHED_STATUSES = ("done", "done_with_errors")
# The chat's per-message page cap is never passed, whatever the setting says.
HARD_PAGES_CAP = 12
PAGE_READ_OUTPUT_TOKENS = 8_000
LONG_CONTEXT_TOKENS = 272_000
FILE_IDS_MAX = 200
UNLABELED_MAX_FILES = 10
ERROR_MAX_CHARS = 300
TOO_LARGE_TEXT = "Start a run this large on the AI Estimating page."
READABLE_EXTENSIONS = (".pdf", ".docx", ".xlsx")
LETTER_IN = (8.5, 11.0)

# (page sizes, byte size) by storage path, per process (a preview downloads
# each file once).
_SIZES_CACHE: OrderedDict[str, tuple[list[tuple[float, float]], int]] = OrderedDict()
_SIZES_MAX = 64
_SIZES_LOCK = threading.Lock()


class PreviewRefused(ValueError):
    """A run preview request the service cannot read (the route maps
    `status` and `code`): `file_ids_invalid` 400, `file_not_in_project` 400,
    `project_not_found` 404, `file_not_found` 404."""

    def __init__(self, message: str, *, code: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


# -- Money and small helpers --------------------------------------------------


def _dec(value: Any) -> Decimal:
    if value is None:
        return _ZERO
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return _ZERO
    return d if d.is_finite() else _ZERO


def _money(value: Any, rounding: str = ROUND_HALF_UP) -> str:
    return str(max(_dec(value), _ZERO).quantize(_CENT, rounding=rounding))


def _money_up(value: Any) -> str:
    return _money(value, ROUND_CEILING)


def _money_down(value: Any) -> str:
    return _money(value, ROUND_FLOOR)


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _problem(code: str, message: str, **extra: Any) -> dict:
    return {"code": code, "message": message, **extra}


def _ext(filename: Any) -> str:
    name = str(filename or "").strip().lower()
    dot = name.rfind(".")
    return name[dot:] if dot >= 0 else ""


def _clip(value: Any, n: int = ERROR_MAX_CHARS) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:n] if text else None


def _sb(sb: Any) -> Any:
    if sb is not None:
        return sb
    from app.core import supabase_client

    return supabase_client.get_supabase()


def _pdf_info(path: str) -> tuple[list[tuple[float, float]], int]:
    """((w_in, h_in) per page, byte size) of a stored PDF, cached per path
    and process."""
    with _SIZES_LOCK:
        cached = _SIZES_CACHE.get(path)
        if cached is not None:
            _SIZES_CACHE.move_to_end(path)
            return cached
    pdf = storage.download_file(path)
    info = ([(float(w), float(h)) for (w, h) in images.page_sizes_in(pdf)], len(pdf))
    with _SIZES_LOCK:
        _SIZES_CACHE[path] = info
        while len(_SIZES_CACHE) > _SIZES_MAX:
            _SIZES_CACHE.popitem(last=False)
    return info


def _page_sizes(path: str) -> list[tuple[float, float]]:
    """(w_in, h_in) per page of a stored PDF."""
    return _pdf_info(path)[0]


def clear_caches() -> None:
    """Forget cached page sizes (tests)."""
    with _SIZES_LOCK:
        _SIZES_CACHE.clear()


def _ai_estimating_block(sb: Any) -> dict:
    """The AE daily cap: `{cap_usd, remaining_usd, blocked}`; fails closed."""
    from app.services import ai_spend

    try:
        st = ai_spend.status(sb)
    except Exception:  # noqa: BLE001 - an unreadable cap reads as no room
        logger.exception("assistant_ai_estimate: AI Estimating spend status failed")
        return {"cap_usd": "0.00", "remaining_usd": "0.00", "blocked": True}
    return {
        "cap_usd": _money(st.get("cap_usd")),
        "remaining_usd": _money_down(st.get("remaining_usd")),
        "blocked": bool(st.get("blocked") or st.get("cap_hit")),
    }


def _assistant_block(user_id: str) -> dict:
    """The person's assistant remainders: `{user_remaining_usd,
    global_remaining_usd}`; fails closed (0.00)."""
    try:
        from app.assistant import spend

        st = spend.state(str(user_id))
    except Exception:  # noqa: BLE001 - an unreadable state reads as no room
        logger.exception("assistant_ai_estimate: assistant spend state failed")
        return {"user_remaining_usd": "0.00", "global_remaining_usd": "0.00"}
    return {
        "user_remaining_usd": _money_down(st.user_remaining),
        "global_remaining_usd": _money_down(st.global_remaining),
    }


def _chat_model(s: Any, run: dict | None = None) -> str:
    """The model the AE chat answers with (the page read's price)."""
    try:
        from app.services import llm
        from app.services.plans_reader_chat import FEATURE

        model = llm.active_model(FEATURE, s)
        if model:
            return str(model)
    except Exception:  # noqa: BLE001 - fall back to the run's model
        logger.debug("assistant_ai_estimate: chat model lookup failed", exc_info=True)
    return str((run or {}).get("model") or getattr(s, "openai_plans_model", "") or "")


# -- 3.5.1 Page read estimate --------------------------------------------------


def _own_pages(document: dict) -> dict[int, dict]:
    """{pdf_page: page_meta entry} (prepended pages kept: page N is PDF index
    N - 1 for every document, the chat's numbering)."""
    out: dict[int, dict] = {}
    meta = document.get("page_meta")
    if not isinstance(meta, list):
        return out
    for i, entry in enumerate(meta, start=1):
        if not isinstance(entry, dict):
            continue
        n = _int(entry.get("pdf_page")) or i
        out.setdefault(n, entry)
    return out


def _sheet_of(entry: dict | None) -> tuple[str | None, str | None]:
    if not entry:
        return None, None
    number = entry.get("sheet_number")
    title = entry.get("title")
    if not number and title:
        try:
            from app.services.plans_reader_agents import split_sheet_title

            number, _rest = split_sheet_title(title)
        except Exception:  # noqa: BLE001 - a label only
            number = None
    return (str(number) if number else None), (str(title)[:200] if title else None)


def page_cost(
    page_tokens: list[int], *, model: str, context_tokens: int, rounds: int, output_tokens: int = PAGE_READ_OUTPUT_TOKENS
) -> dict | None:
    """The 3.5.1 formula: `base = sum(page tokens) + context`, billed once at
    the input rate and `rounds - 1` times at the cached rate, plus
    `output_tokens` at the output rate. None for a model without a price.
    Returns `{base_tokens, input_usd, output_usd, estimate (Decimal)}`."""
    from app.services import llm_pricing

    price = llm_pricing.price_for(model)
    if price is None:
        return None
    base = int(sum(page_tokens)) + int(context_tokens)
    rounds = max(1, int(rounds))
    input_usd = Decimal(base) * price.input / _MILLION + Decimal(base * (rounds - 1)) * price.cached_input / _MILLION
    output_usd = Decimal(int(output_tokens)) * price.output / _MILLION
    return {
        "base_tokens": base,
        "input_usd": input_usd,
        "output_usd": output_usd,
        "estimate": input_usd + output_usd,
        "long_context_threshold": int(price.long_context_threshold or LONG_CONTEXT_TOKENS),
    }


def page_read_estimate(
    sb: Any, run: dict, document_id: str, pages: list[int], user_id: str, *, settings: Any = None
) -> dict:
    """Worst-case cost of one page-read card (3.5.1).

    Validates (run `done` or `done_with_errors`, the document belongs to the
    run, pages are unique 1-based `pdf_page` values within `page_count`, at
    most `ASSISTANT_PLANS_PAGES_PER_CARD` and never above 12), sizes each page
    from storage (fallback: the document's sheet size with
    `sizes_approx: true`), prices native-resolution tokens with `base` billed
    once at the input rate and `MAX_ROUNDS - 1` times at the cached rate plus
    8,000 output tokens, and refuses `long_context` when `base` would pass
    272,000 tokens.

    Returns `{run {id, name}, document {id, name}, pages: [{page,
    sheet_number, title, w_in, h_in, ppi, tokens}], input_tokens,
    output_tokens, rounds, estimate_usd, max_usd, model, sizes_approx,
    ai_estimating {cap_usd, remaining_usd, blocked}, assistant
    {user_remaining_usd, global_remaining_usd}, problems: []}`.
    `input_tokens` is `base x rounds` (every input token billed, cached
    re-bills included); `base_tokens` is `base`. Never raises for a business
    reason: every refusal is a problem, and the card refuses on any.
    """
    from app.services.plans_reader_chat import MAX_ROUNDS

    s = settings or get_settings()
    run = run or {}
    run_id = str(run.get("id") or "")
    problems: list[dict] = []
    rounds = int(MAX_ROUNDS)
    model = _chat_model(s, run)
    cap = max(1, min(int(getattr(s, "assistant_plans_pages_per_card", 6) or 6),
                     int(getattr(s, "plans_reader_promax_max_pages_per_message", HARD_PAGES_CAP) or HARD_PAGES_CAP),
                     HARD_PAGES_CAP))
    out: dict[str, Any] = {
        "run": {"id": run_id, "name": run.get("name")},
        "document": {"id": str(document_id or ""), "name": None},
        "pages": [],
        "input_tokens": 0,
        "base_tokens": 0,
        "output_tokens": PAGE_READ_OUTPUT_TOKENS,
        "rounds": rounds,
        "estimate_usd": "0.00",
        "max_usd": "0.00",
        "model": model,
        "sizes_approx": False,
        "max_pages": cap,
        "ai_estimating": _ai_estimating_block(sb),
        "assistant": _assistant_block(user_id),
        "problems": problems,
    }
    if run.get("status") not in FINISHED_STATUSES:
        problems.append(_problem("run_not_finished", "This AI Estimating run has not finished, so its pages cannot be read yet."))
    document = None
    doc_uuid: str | None = str(document_id or "").strip()
    try:
        uuid.UUID(doc_uuid)
    except (TypeError, ValueError, AttributeError):
        doc_uuid = None
    if doc_uuid and run_id:
        rows = (
            sb.table("plans_reader_documents")
            .select("id, run_id, name, page_count, page_meta, storage_path, sheet_width_in, sheet_height_in")
            .eq("id", doc_uuid)
            .eq("run_id", run_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        document = rows[0] if rows else None
    if document is None:
        problems.append(_problem("document_not_in_run", "That document is not part of this run."))
        return out
    out["document"] = {"id": str(document["id"]), "name": document.get("name")}

    wanted: list[int] = []
    raw = list(pages) if isinstance(pages, (list, tuple)) else []
    bad = not raw
    for p in raw:
        n = _int(p)
        if n is None or n < 1 or n in wanted:
            bad = True
            continue
        wanted.append(n)
    count = _int(document.get("page_count")) or len(document.get("page_meta") or []) or 0
    out_of_range = [p for p in wanted if count and p > count]
    if bad or out_of_range or not count:
        problems.append(
            _problem("pages_invalid", f"Pages must be distinct page numbers from 1 to {count or 'the page count'}.")
        )
    if len(raw) > cap:
        problems.append(_problem("too_many_pages", f"A card reads at most {cap} pages. Ask about fewer pages at a time."))
    wanted = [p for p in wanted if not count or p <= count][:cap]

    sizes: list[tuple[float, float]] | None = None
    path = document.get("storage_path")
    if path:
        try:
            sizes = _page_sizes(str(path))
        except Exception:  # noqa: BLE001 - fall back to the stored sheet size
            logger.warning("assistant_ai_estimate: page sizes for %s failed", document.get("id"), exc_info=True)
            sizes = None
    meta = _own_pages(document)
    fallback = (float(_dec(document.get("sheet_width_in"))), float(_dec(document.get("sheet_height_in"))))
    max_patches = int(getattr(s, "plans_reader_native_max_patches", images.MAX_PATCHES_ORIGINAL))
    cap_ppi = int(getattr(s, "plans_reader_tile_ppi", images.DEFAULT_CAP_PPI))
    tokens: list[int] = []
    for p in wanted:
        entry = meta.get(p)
        if sizes is not None and 1 <= p <= len(sizes):
            w, h = sizes[p - 1]
        else:
            out["sizes_approx"] = True
            w = float(_dec((entry or {}).get("w_in"))) or fallback[0]
            h = float(_dec((entry or {}).get("h_in"))) or fallback[1]
        number, title = _sheet_of(entry)
        try:
            ppi = images.native_ppi_for(w, h, max_patches=max_patches, cap_ppi=cap_ppi)
        except ValueError:
            problems.append(_problem("page_size_unknown", f"The size of page {p} is not known, so its cost cannot be estimated."))
            continue
        t = images.image_tokens(images.px_for_inches(w, ppi), images.px_for_inches(h, ppi))
        tokens.append(t)
        out["pages"].append(
            {"page": p, "sheet_number": number, "title": title, "w_in": round(w, 2), "h_in": round(h, 2),
             "ppi": ppi, "tokens": t}
        )

    context = int(getattr(s, "assistant_plans_context_tokens", 24_000) or 0)
    cost = page_cost(tokens, model=model, context_tokens=context, rounds=rounds)
    if cost is None:
        problems.append(_problem("unpriced_model", "The AI Estimating chat model has no price, so a paid read cannot be estimated."))
        base = sum(tokens) + context
        out["base_tokens"] = base
        out["input_tokens"] = base * rounds
        return out
    base = cost["base_tokens"]
    out["base_tokens"] = base
    out["input_tokens"] = base * rounds
    if base > min(cost["long_context_threshold"], LONG_CONTEXT_TOKENS):
        problems.append(
            _problem("long_context", "These pages are too large to read together in one answer. Ask about fewer pages.")
        )
    factor = _dec(getattr(s, "assistant_plans_hold_factor", 1.5)) or Decimal("1.5")
    out["estimate_usd"] = _money(cost["estimate"])
    out["max_usd"] = _money_up(cost["estimate"] * factor)
    return out


# -- 3.6.1 Run preview ----------------------------------------------------------


def unlabeled_split_usd(pages_per_file: list[int], settings: Any = None) -> Decimal | None:
    """The Bid File Splitter allowance for unlabeled files (0 for none; None
    when the splitter's model has no price, which every caller refuses)."""
    if not pages_per_file:
        return _ZERO.quantize(_CENT)
    from app.services import split_handoff

    est = split_handoff.estimate_split_usd([max(1, int(p)) for p in pages_per_file], settings)
    return None if est is None else Decimal(est.amount)


def _project(sb: Any, project_id: str) -> dict:
    pid = str(project_id or "").strip()
    try:
        uuid.UUID(pid)
    except (TypeError, ValueError, AttributeError):
        raise PreviewRefused("That project id is not valid.", code="project_not_found", status=404) from None
    rows = sb.table("projects").select("id, number, name").eq("id", pid).limit(1).execute().data or []
    if not rows:
        raise PreviewRefused("That project no longer exists.", code="project_not_found", status=404)
    r = rows[0]
    return {"id": str(r["id"]), "number": r.get("number"), "name": r.get("name")}


def _preview_files(sb: Any, project_id: str, file_ids: list[str] | None, labels: dict, refused: frozenset) -> list[dict]:
    cols = "id, project_id, filename, category, storage_path, size_bytes, created_at"
    if file_ids is None:
        rows = sb.table("project_files").select(cols).eq("project_id", project_id).order("created_at").execute().data or []
        return [
            r for r in rows
            if str(r.get("category") or "") in labels
            and str(r.get("category") or "") not in refused
            and _ext(r.get("filename")) in READABLE_EXTENSIONS
        ]
    ids: list[str] = []
    for raw in file_ids:
        fid = str(raw or "").strip()
        try:
            uuid.UUID(fid)
        except (TypeError, ValueError, AttributeError):
            raise PreviewRefused("One of the file ids is not valid.", code="file_ids_invalid") from None
        if fid not in ids:
            ids.append(fid)
    if len(ids) > FILE_IDS_MAX:
        raise PreviewRefused(f"Choose at most {FILE_IDS_MAX} files.", code="file_ids_invalid")
    if not ids:
        return []
    rows = sb.table("project_files").select(cols).in_("id", ids).execute().data or []
    by_id = {str(r["id"]): r for r in rows}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise PreviewRefused("One of the chosen files no longer exists.", code="file_not_found", status=404)
    if any(str(by_id[i].get("project_id")) != project_id for i in ids):
        raise PreviewRefused("Every file must come from this project.", code="file_not_in_project")
    return [by_id[i] for i in ids]


def run_preview(
    sb: Any, project_id: str, file_ids: list[str] | None, user_id: str, *, settings: Any = None
) -> dict:
    """Cost preview of an assistant-started AE run without creating it (3.6.1).

    `file_ids` None means every readable file of the categories in
    `PROJECT_CATEGORY_LABELS`. Caps: `ASSISTANT_AE_RUN_MAX_FILES`,
    `ASSISTANT_AE_RUN_MAX_PAGES`, 600 pages per unlabeled file, 10 unlabeled
    files; beyond any cap a problem. The estimate logic is copied from
    `runs.estimate` (not imported from AE private helpers) and must land
    within 5 percent of it.

    Returns `{project, files: [{id, filename, category, label, pages,
    approx}], pages_total, unlabeled_files, cost_usd, ceiling_usd, split_usd,
    hold_usd, model, ai_estimating {cap_usd, remaining_usd, blocked},
    assistant {user_remaining_usd, global_remaining_usd}, over_run_limit,
    problems}`. Additive keys: `cost_usd_max` (the file estimate with its
    escalation worst case, the number `runs.estimate` returns),
    `loop_budget_usd` (the tool-loop budgets the AE ceiling adds when a spine
    flag is on) and per file `problem` (null or a problem code). A file with
    a problem is listed but not priced.

    Raises `PreviewRefused` only for a request it cannot read (bad or foreign
    file ids, a missing project or file).
    """
    from app.services import llm_pricing
    from app.services import plans_reader_agents as agents
    from app.services import plans_reader_runs as runs

    s = settings or get_settings()
    project = _project(sb, project_id)
    labels = runs.PROJECT_CATEGORY_LABELS
    refused = runs.REFUSED_PROJECT_CATEGORIES
    rows = _preview_files(sb, project["id"], file_ids, labels, refused)
    problems: list[dict] = []
    max_files = int(getattr(s, "assistant_ae_run_max_files", 15))
    max_pages = int(getattr(s, "assistant_ae_run_max_pages", 400))
    unlabeled_max_pages = int(getattr(s, "bid_split_max_pages_per_file", 600))
    model = str(getattr(s, "openai_plans_model", "") or "")
    render_mode = "adaptive"

    ledger_tokens = int(s.plans_reader_ledger_max_tokens)
    prompt = agents.prompt_tokens("sub")
    file_pages_cap = min(
        images.FILE_MAX_PAGES,
        pack.max_file_pages_for_budget(int(s.plans_reader_group_input_tokens), ledger_tokens, prompt_tokens=prompt),
    )
    files: list[dict] = []
    docs: list[pack.PackDoc] = []
    unlabeled_pages: list[int] = []
    pages_total = 0
    for r in rows:
        fid = str(r["id"])
        category = str(r.get("category") or "")
        name = r.get("filename")
        entry = {"id": fid, "filename": name, "category": category, "label": None, "pages": 0, "approx": False,
                 "problem": None}
        files.append(entry)
        if category in refused or category not in labels:
            entry["problem"] = "file_refused_category"
            problems.append(_problem("file_refused_category",
                                     f"{name}: files of this kind ({category or 'unknown'}) are not read by AI Estimating.",
                                     file_id=fid))
            continue
        ext = _ext(name)
        if ext not in READABLE_EXTENSIONS:
            entry["problem"] = "file_unreadable_type"
            problems.append(_problem("file_unreadable_type",
                                     f"{name}: AI Estimating reads PDF, Word (.docx) and Excel (.xlsx) files only.",
                                     file_id=fid))
            continue
        label = labels[category]
        entry["label"] = label
        if ext == ".pdf":
            try:
                sizes, size_bytes = _pdf_info(str(r.get("storage_path") or ""))
                if not sizes:
                    raise ValueError("no pages")
            except Exception:  # noqa: BLE001 - one unreadable file is a problem, not a failure
                logger.warning("assistant_ai_estimate: could not read %s", fid, exc_info=True)
                entry["problem"] = "file_unreadable"
                problems.append(_problem("file_unreadable", f"{name}: the PDF could not be read.", file_id=fid))
                continue
        else:
            sizes = [LETTER_IN]
            size_bytes = int(r.get("size_bytes") or 0)
            entry["approx"] = True
        n = len(sizes)
        entry["pages"] = n
        pages_total += n
        doc_category = "general_drawings" if not label or label == "general_drawings" else label
        if not label or label == "general_drawings":
            unlabeled_pages.append(n)
            if n > unlabeled_max_pages:
                problems.append(_problem("unlabeled_file_too_long",
                                         f"{name}: {n} pages is over the splitter's {unlabeled_max_pages}-page limit. "
                                         + TOO_LARGE_TEXT, file_id=fid))
        if doc_category in agents.DRAWING_CATEGORIES:
            plans = [
                pack.plan_page(i, w, h, render_mode=render_mode, tile_ppi=s.plans_reader_tile_ppi,
                               max_patches=s.plans_reader_native_max_patches)
                for i, (w, h) in enumerate(sizes, start=1)
            ]
            docs.append(pack.PackDoc(fid, doc_category, "drawing", pages=plans, page_count=len(plans)))
        else:
            docs.append(
                pack.plan_file_document(fid, doc_category, n, max(1, size_bytes),
                                        max_bytes=s.plans_reader_file_max_bytes, max_pages=file_pages_cap)
            )
    if not rows:
        problems.append(_problem("no_files", "This project has no files AI Estimating can read."))
    readable = [f for f in files if not f["problem"]]
    if len(readable) > max_files:
        problems.append(_problem("too_many_files", f"{len(readable)} files is over the assistant's {max_files}-file limit. "
                                 + TOO_LARGE_TEXT))
    if pages_total > max_pages:
        problems.append(_problem("too_many_pages", f"{pages_total} pages is over the assistant's {max_pages}-page limit. "
                                 + TOO_LARGE_TEXT))
    if len(unlabeled_pages) > UNLABELED_MAX_FILES:
        problems.append(_problem("too_many_unlabeled_files",
                                 f"{len(unlabeled_pages)} files need the splitter (at most {UNLABELED_MAX_FILES}). "
                                 + TOO_LARGE_TEXT))

    price = llm_pricing.price_for(model)
    if price is None:
        problems.append(_problem("unpriced_model", "The AI Estimating model has no price, so the run cannot be estimated."))
        in_m = out_m = 0.0
        threshold = pack.LONG_CONTEXT_THRESHOLD
    else:
        in_m, out_m = float(price.input), float(price.output)
        threshold = int(price.long_context_threshold or pack.LONG_CONTEXT_THRESHOLD)
    est = {"input_tokens": 0, "cost_usd": 0.0, "cost_usd_max": 0.0, "escalation_max_input_tokens": 0}
    if docs:
        try:
            est = pack.estimate_run(
                docs,
                budget_tokens=s.plans_reader_group_input_tokens,
                ledger_tokens=ledger_tokens,
                input_per_m=in_m,
                output_per_m=out_m,
                prompt_tokens=prompt,
                file_max_bytes=s.plans_reader_file_max_bytes,
                ledger_images=agents.LEDGER_MAX_IMAGES,
                escalation_max_fraction=s.plans_reader_escalation_max_fraction,
                long_context_threshold=threshold,
            )
        except ValueError as exc:
            problems.append(_problem("file_unreadable", f"The files could not be planned: {exc}"))
    loop = runs.loop_budget_usd_max(s) if runs.spine_enabled(s) else _ZERO
    cost_max = _dec(est.get("cost_usd_max") or est.get("cost_usd")) + _dec(loop)
    factor = _dec(getattr(s, "plans_reader_run_ceiling_factor", 1.25)) or Decimal("1.25")
    ceiling = (cost_max * factor).quantize(_CENT, rounding=ROUND_CEILING)
    split = unlabeled_split_usd(unlabeled_pages, s)
    if split is None:
        problems.append(_problem("split_unpriced", "The Bid File Splitter model has no price, so the run cannot be estimated."))
        split = _ZERO
    split = split.quantize(_CENT, rounding=ROUND_CEILING)
    limit = int(getattr(s, "plans_reader_max_input_tokens_per_run", 10_000_000))
    over_run_limit = int(est.get("input_tokens") or 0) + int(est.get("escalation_max_input_tokens") or 0) > limit
    return {
        "project": project,
        "files": files,
        "pages_total": pages_total,
        "unlabeled_files": len(unlabeled_pages),
        "cost_usd": _money(est.get("cost_usd")),
        "cost_usd_max": _money(est.get("cost_usd_max")),
        "loop_budget_usd": _money_up(loop),
        "ceiling_usd": _money(ceiling),
        "split_usd": _money(split),
        "hold_usd": _money_up(ceiling + split),
        "model": model,
        "ai_estimating": _ai_estimating_block(sb),
        "assistant": _assistant_block(user_id),
        "over_run_limit": over_run_limit,
        "problems": problems,
    }


# -- 3.6.4 and 3.5.4 state reads -----------------------------------------------


def run_state(sb: Any, run_id: str) -> dict:
    """The AE run fields finalize needs (3.6.4): `{status, cost_usd,
    finished_at, error}` from `plans_reader_runs` (`created_by` never
    returned). `sb` may be None; the service then uses
    `supabase_client.get_supabase()`. Callers under `app/assistant`
    (`plans_tasks.advance`) pass None.

    `cost_usd` rounds up to the cent (the settled actual never under-counts);
    a run that no longer exists reads `{status: "missing", cost_usd: "0.00",
    finished_at: None, error: None}`."""
    db = _sb(sb)
    rows = (
        db.table("plans_reader_runs").select("id, status, cost_usd, finished_at, error").eq("id", str(run_id))
        .limit(1).execute().data
        or []
    )
    if not rows:
        return {"status": "missing", "cost_usd": "0.00", "finished_at": None, "error": None}
    r = rows[0]
    return {
        "status": r.get("status"),
        "cost_usd": _money_up(r.get("cost_usd")),
        "finished_at": r.get("finished_at"),
        "error": _clip(r.get("error")),
    }


def chat_message_state(sb: Any, message_id: str, *, viewer_role: str) -> dict:
    """One AE chat answer for page-read finalize (3.5.4): `{status, content,
    cost_usd, error}`. The row goes through
    `plans_reader_chat.redact_messages([row], viewer_role=viewer_role)` before
    `content` is returned, so a `pricing_restricted` answer never reaches a
    role that cannot see pricing. `sb` may be None; the service then uses
    `supabase_client.get_supabase()`. Callers under `app/assistant`
    (`plans_tasks.advance`) pass None.

    `status` is the AE message status (`pending`, `running`, `done`,
    `failed`) or `missing`; `cost_usd` rounds up to the cent; `error` is
    clipped to 300 characters (blank when the answer is redacted)."""
    from app.services import plans_reader_chat

    db = _sb(sb)
    rows = db.table("plans_reader_chat_messages").select("*").eq("id", str(message_id)).limit(1).execute().data or []
    if not rows:
        return {"status": "missing", "content": "", "cost_usd": "0.00", "error": None}
    row = plans_reader_chat.redact_messages([rows[0]], viewer_role=viewer_role)[0]
    return {
        "status": row.get("status"),
        "content": str(row.get("content") or ""),
        "cost_usd": _money_up(row.get("cost_usd")),
        "error": _clip(row.get("error")),
    }


# ── Page-read thread lookups (3.5.3 resume, 3.5.4 crash adoption) ────────────

PAGE_READ_THREAD_TITLE = "Assistant page reads"


def page_read_content(document_id: str, pages: list[int], question: str) -> str:
    """The server-templated question a page-read card sends (3.5.3)."""
    listed = ", ".join(str(p) for p in pages)
    return (
        f"Read document {document_id} pages {listed} with read_pages and answer this question from what the "
        f"pages show. Cite sheet numbers. Question: {question}"
    )


def page_read_thread(sb: Any, run_id: str, user_id: str) -> dict | None:
    """The person's "Assistant page reads" thread on the run, or None."""
    rows = (
        _sb(sb).table("plans_reader_chat_threads").select("*").eq("run_id", str(run_id))
        .eq("created_by", str(user_id)).eq("title", PAGE_READ_THREAD_TITLE).order("created_at").limit(1)
        .execute().data or []
    )
    return rows[0] if rows else None


def sent_page_read_answer(sb: Any, thread_id: str, user_id: str, since: str | None, content: str) -> dict | None:
    """The answer row an attempt of one card already sent on the thread (a
    crash between the send and the task update), or None: the first answer
    row after a user row carrying the card's exact text, created at or after
    `since` (the task's created_at)."""
    db = _sb(sb)
    asked = (
        db.table("plans_reader_chat_messages").select("id, created_at, content").eq("thread_id", str(thread_id))
        .eq("role", "user").eq("created_by", str(user_id))
    )
    if since:
        asked = asked.gte("created_at", since)
    mine = [r for r in (asked.order("created_at").execute().data or []) if (r.get("content") or "").strip() == content]
    if not mine:
        return None
    rows = (
        db.table("plans_reader_chat_messages").select("id, created_at").eq("thread_id", str(thread_id))
        .eq("role", "assistant").eq("created_by", str(user_id)).gte("created_at", str(mine[0].get("created_at")))
        .order("created_at").limit(1).execute().data or []
    )
    return rows[0] if rows else None


def adopt_page_read_message(sb: Any, user_id: str, task: dict) -> str | None:
    """The AE answer id a page-read task sent but never recorded (3.5.4
    crash adoption for `plans_tasks._advance_pages`), or None. Rebuilds the
    card's text from the task payload (`document_id`, `pages`, `question`);
    a task without a stored question is never adopted. `sb` may be None."""
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    run_id, document_id = payload.get("plans_run_id"), payload.get("document_id")
    question = str(payload.get("question") or "").strip()
    pages = [p for p in payload.get("pages") or [] if isinstance(p, int) and not isinstance(p, bool)]
    if not (run_id and document_id and question and pages):
        return None
    thread = page_read_thread(sb, str(run_id), str(user_id))
    if thread is None:
        return None
    sent = sent_page_read_answer(sb, str(thread["id"]), str(user_id), task.get("created_at"),
                                 page_read_content(str(document_id), pages, question))
    return str(sent["id"]) if sent and sent.get("id") else None

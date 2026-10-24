"""The `plan` replay stage (WS1 step 13; plans_reader_replay STAGE_ORDER):
page facts, relabel, spec selection, ledger and planned requests for a
stored run bundle at $0, from the backup PDFs.

`plan_stage(ctx, artifacts)` extracts the facts of every bundle document
(cached per sha256 and kind inside the process), applies the facts job's
relabel decision, runs the real planner (`plans_reader_runs._plan_reading`
with `sb is None`, the PDFs from the bundle and the facts injected), builds
every planned sub request through `plans_reader_agents._build_sub` with the
page renderer stubbed (the request text and structure are what the checks
read; the pixels are not), and writes:

- `documents` (sha256, facts_status done, category_label, facts_doc, the
  merged page_meta, the relabeled category), `page_meta`, `ledger`;
- `groups`: the planned sub groups (status `planned`) plus the bundle's
  non-sub groups unchanged, so the legacy merge keeps the stored category
  outputs while E24 counts the pages the new plan sends;
- `planned_groups` and `planned_requests`: one row per planned sub request
  with {kind, category, group_index, group_id, has_legend_text,
  pages_without_facts_block, est_input_tokens, ...} (what E16 and E18 read);
- `plan_summary`: counts and ratios (groups, tokens, spec pages sent,
  relabels, facts timing). No dollars.

Flags come from `ctx.settings_overrides` over the process settings, so a
test can run the stage with every page-facts flag off (T-INT-1) or on
(T-INT-2) without touching the environment. Nothing here calls a model.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Mapping
from typing import Any
from unittest import mock

from app.services.pagefacts import extract as extract_mod
from app.services.pagefacts import integration
from app.services.pagefacts import job as job_mod
from app.services.pagefacts.contracts import DocFacts

logger = logging.getLogger(__name__)

PLAN_VERSION = "ws1-plan-1"
REPLAY_PAGE_BUDGET_S = 8.0
REPLAY_DOC_BUDGET_S = 600.0
_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0300050201000f2b2a0b0000000049454e44ae426082"
)
_FACTS_CACHE: dict[tuple[str, str], DocFacts] = {}


class _Query:
    """The few query shapes the planner and the sub builder issue when a
    replay hands them every row (documents, groups, facts): `projects`
    answers the stored project, every other table is empty and every
    write is a no-op."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self._filters: list[tuple[str, Any]] = []

    def select(self, *_a: Any, **_k: Any) -> _Query:
        return self

    def update(self, *_a: Any, **_k: Any) -> _Query:
        self._rows = []
        return self

    def insert(self, *_a: Any, **_k: Any) -> _Query:
        self._rows = []
        return self

    def delete(self) -> _Query:
        self._rows = []
        return self

    def eq(self, col: str, val: Any) -> _Query:
        self._filters.append((col, val))
        return self

    def in_(self, _col: str, _vals: Any) -> _Query:
        return self

    def order(self, *_a: Any, **_k: Any) -> _Query:
        return self

    def limit(self, *_a: Any, **_k: Any) -> _Query:
        return self

    def execute(self) -> Any:
        rows = [r for r in self._rows if all(str(r.get(c)) == str(v) for c, v in self._filters)]
        return type("Resp", (), {"data": rows})()


class ReplayDb:
    def __init__(self, project: dict[str, Any] | None) -> None:
        self._project = dict(project or {})

    def table(self, name: str) -> _Query:
        if name == "projects" and self._project:
            return _Query([self._project])
        return _Query([])


def replay_settings(ctx: Any) -> Any:
    from app.core.config import get_settings

    s = get_settings()
    overrides = dict(getattr(ctx, "settings_overrides", None) or {})
    return s.model_copy(update=overrides) if overrides else s


def _splitter_titles(doc: Mapping[str, Any]) -> dict[int, str]:
    return job_mod._splitter_titles(dict(doc))


def extract_facts(pdf: bytes, doc: Mapping[str, Any]) -> DocFacts:
    """DocFacts of one bundle document (cached per sha256 and kind for the
    process; timing fields are real)."""
    label = doc.get("category_label") or doc.get("category")
    kind = extract_mod.kind_for_label(label)
    sha = extract_mod.sha256_of(pdf)
    key = (sha, kind)
    hit = _FACTS_CACHE.get(key)
    if hit is not None:
        return hit
    facts = extract_mod.extract_document(
        pdf,
        label=label,
        name=doc.get("name"),
        kind=kind,
        splitter_titles=_splitter_titles(doc),
        page_budget_s=REPLAY_PAGE_BUDGET_S,
        doc_budget_s=REPLAY_DOC_BUDGET_S,
    )
    _FACTS_CACHE[key] = facts
    return facts


def clear_facts_cache() -> None:
    _FACTS_CACHE.clear()


def facts_documents(ctx: Any, documents: list[dict[str, Any]], s: Any) -> tuple[list[dict[str, Any]], dict[str, DocFacts], dict[str, Any]]:
    """Documents after the facts job (sha256, facts_status, category_label,
    facts_doc, relabel), the DocFacts by id and extraction timings."""
    out: list[dict[str, Any]] = []
    facts_by_doc: dict[str, DocFacts] = {}
    timing: dict[str, Any] = {"documents": {}, "ms_total": 0}
    relabel_on = bool(getattr(s, "plans_reader_facts_relabel", False))
    for row in documents:
        doc = dict(row)
        if doc.get("status") == "failed":
            out.append(doc)
            continue
        doc_id = str(doc.get("id"))
        started = time.monotonic()
        pdf = ctx.docs.get(doc_id)
        facts = extract_facts(pdf, doc)
        ms = int(round((time.monotonic() - started) * 1000))
        timing["documents"][doc_id[:8]] = {"ms": ms, "pages": facts.page_count, "cached": ms < 5}
        timing["ms_total"] += ms
        check = job_mod.category_check_for(facts, str(doc.get("category") or ""), doc.get("name"))
        relabel = job_mod.relabel_decision(doc, check, enabled=relabel_on)
        doc["sha256"] = facts.sha256
        doc["facts_status"] = "done"
        doc["facts_error"] = None
        doc["category_label"] = doc.get("category_label") or doc.get("category")
        doc["facts_doc"] = job_mod._facts_doc(facts, cache="replay", check=check, relabel=relabel)
        if relabel["applied"]:
            doc["category"] = relabel["to"]
        facts_by_doc[doc_id] = facts
        out.append(doc)
    return out, facts_by_doc, timing


def _stub_render(pdf_bytes: bytes, page_index0: int, ppi: float, box_px: Any = None, *, max_patches: int | None = None) -> tuple[bytes, int, int]:
    """A 1x1 PNG with the planned pixel size: the request structure and
    text are what the checks read; rendering 42x30 sheets is not."""
    from app.services import plans_reader_images as images

    if box_px is not None:
        x0, y0, x1, y1 = box_px
        return _PNG_1X1, max(1, int(x1 - x0)), max(1, int(y1 - y0))
    w_pt, h_pt = images.page_sizes_pt(pdf_bytes)[page_index0]
    return _PNG_1X1, images.px_for_points(w_pt, ppi), images.px_for_points(h_pt, ppi)


def _text_sha(parts: list[dict[str, Any]]) -> str:
    rows = []
    for p in parts:
        if p.get("type") == "input_text":
            rows.append(("text", p.get("text")))
        elif p.get("type") == "input_image":
            rows.append(("image", p.get("detail")))
        else:
            rows.append(("file", p.get("filename")))
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode("utf-8")).hexdigest()


_PAGE_KEY_RE = re.compile(r"^(D\d+ P\d+(?:-P\d+)?)\b")


def _page_key(entry: Mapping[str, Any]) -> str | None:
    """The page a manifest text part is about: `D<n> P<p>` for an image
    page (every tile label of that page gives the same key) or
    `D<n> P<s>-P<e>` for a file span, read from the part's head."""
    m = _PAGE_KEY_RE.match(str(entry.get("head") or ""))
    return m.group(1) if m else None


def facts_block_coverage(manifest: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Which pages of one request are followed by THEIR OWN facts block
    (T-INT-2, E18). Pages are the distinct page keys of the `page_label`
    parts in order (tile labels of one page count once). A page is covered
    only when a facts block whose head names the same page arrives after
    its labels and before the next page's label; a block for another page
    covers nothing, so a middle page without facts is counted even when
    the pages around it have blocks."""
    pages: list[str] = []
    uncovered: list[str] = []
    current: str | None = None
    covered = False

    def close() -> None:
        if current is not None and not covered:
            uncovered.append(current)

    for m in manifest:
        role = m.get("role")
        if role == "page_label":
            key = _page_key(m) or f"label#{len(pages) + 1}"
            if key == current:
                continue
            close()
            current, covered = key, False
            pages.append(key)
        elif role == integration.FACTS_BLOCK_ROLE and current is not None:
            if _page_key(m) == current:
                covered = True
    close()
    return {"pages": len(pages), "uncovered": uncovered}


def build_planned_requests(
    run: dict[str, Any],
    ledger: dict[str, Any],
    group_rows: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    s: Any,
    *,
    pdf_for: Any,
    facts: dict[str, DocFacts] | None,
    project: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """One planned request row per sub group, built through the real sub
    builder with the renderer stubbed. `pages_without_facts_block` counts
    the pages (image pages and file spans, see `facts_block_coverage`) not
    followed by their own facts block; `has_legend_text` says the ledger
    part carries the legend text."""
    from app.services import plans_reader_agents as agents
    from app.services import plans_reader_images as images

    db = ReplayDb(project)
    docs_by_id = {str(d["id"]): d for d in documents}
    run_for_build = dict(run)
    run_for_build["ledger"] = ledger
    legend_text = ((ledger.get("facts") or {}).get("legend_text") or "").strip()
    out: list[dict[str, Any]] = []
    by_category: dict[str, list[dict[str, Any]]] = {}
    for g in group_rows:
        by_category.setdefault(str(g.get("category")), []).append(g)
    with mock.patch.object(images, "render_page_png", _stub_render):
        for g in group_rows:
            builder, _doc_map = agents._build_sub(
                db, run_for_build, g, s,
                docs=docs_by_id, pdf_for=pdf_for, category_groups=by_category[str(g.get("category"))], facts=facts,
            )
            manifest = builder.manifest
            roles = [m.get("role") for m in manifest]
            page_labels = sum(1 for m in manifest if m.get("role") == "page_label")
            coverage = facts_block_coverage(manifest)
            ledger_parts = [p for p, m in zip(builder.parts, manifest) if m.get("role") == "ledger"]
            ledger_text = ledger_parts[0].get("text", "") if ledger_parts else ""
            has_legend = bool(legend_text) and legend_text in ledger_text
            text_tokens = sum(agents._text_tokens(p.get("text", "")) for p in builder.parts if p.get("type") == "input_text")
            out.append(
                {
                    "kind": "sub",
                    "category": g.get("category"),
                    "group_index": int(g.get("group_index") or 0),
                    "group_id": g.get("id"),
                    "has_legend_text": has_legend,
                    "pages_without_facts_block": len(coverage["uncovered"]),
                    "pages_uncovered": coverage["uncovered"][:20],
                    "pages_sent": coverage["pages"],
                    "page_labels": page_labels,
                    "image_pages": sum(1 for i in g.get("pages") or [] if i.get("kind") == "image"),
                    "file_pages": sum(1 for i in g.get("pages") or [] if i.get("kind") == "file"),
                    "est_input_tokens": int(g.get("est_input_tokens") or 0),
                    "text_tokens": text_tokens,
                    "images": builder.images,
                    "file_bytes": builder.file_bytes,
                    "roles": roles,
                    "system_sha256": hashlib.sha256(agents._system_for(g, s).encode("utf-8")).hexdigest(),
                    "text_sha256": _text_sha(builder.parts),
                }
            )
    return out


def plan_stage(ctx: Any, art: dict[str, Any]) -> dict[str, Any]:
    """The replay stage (see the module docstring)."""
    from app.services import plans_reader_runs as runs

    s = replay_settings(ctx)
    stored_run = dict(ctx.bundle.run)
    stored_ledger = dict(art.get("ledger") or {})
    documents, facts_by_doc, timing = facts_documents(ctx, [dict(d) for d in art.get("documents") or []], s)
    live = [d for d in documents if d.get("status") != "failed"]
    run = dict(stored_run)
    run["ledger"] = None
    project = stored_ledger.get("project") if isinstance(stored_ledger.get("project"), Mapping) else None
    db = ReplayDb({"id": run.get("project_id"), **(project or {})}) if project else None
    pdf_for_doc = ctx.docs.get
    ledger, rows, problems = runs._plan_reading(
        db, run, live, s, pdf_bytes=lambda d: pdf_for_doc(str(d["id"])), facts=facts_by_doc if integration.enabled(s) else None,
    )
    if db is None and project:
        ledger["project"] = dict(project)
    planned_groups: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        planned_groups.append({**row, "id": f"planned:{row.get('category')}:{row.get('group_index')}", "status": "planned", "run_id": run.get("id"), "ordinal": i})
    requests = build_planned_requests(
        run, ledger, planned_groups, live, s,
        pdf_for=pdf_for_doc, facts=facts_by_doc if integration.enabled(s) else None, project=project,
    )
    page_meta = []
    for d in documents:
        for m in d.get("page_meta") or []:
            if isinstance(m, Mapping):
                page_meta.append({"document_id": d.get("id"), "category": d.get("category"), **m})
    stored_groups = [dict(g) for g in art.get("groups") or [] if g.get("kind") != "sub"]
    spec_docs = [d for d in live if str(d.get("category")) == "specifications"]
    spec_pages = sum(int(d.get("page_count") or 0) for d in spec_docs)
    spec_sent = sum(len(g.get("pages") or []) for g in planned_groups if g.get("category") == "specifications")
    summary = {
        "version": PLAN_VERSION,
        "flags": {
            "page_facts_enabled": integration.enabled(s),
            "page_facts_in_subs": integration.in_subs(s),
            "spec_select": integration.spec_select_on(s),
            "facts_relabel": integration.relabel_on(s),
        },
        "documents": [
            {
                "id8": str(d.get("id"))[:8],
                "name": str(d.get("name"))[:60],
                "category": d.get("category"),
                "category_label": d.get("category_label"),
                "pages": d.get("page_count"),
                "relabel": ((d.get("facts_doc") or {}).get("relabel") or {}).get("applied"),
                "reduced_reading": (d.get("facts_doc") or {}).get("reduced_reading"),
                "pages_planned": sum(len([i for i in g.get("pages") or [] if str(i.get("document_id")) == str(d.get("id"))]) for g in planned_groups),
            }
            for d in live
        ],
        "planned_groups": len(planned_groups),
        "planned_by_category": {c: sum(1 for g in planned_groups if g.get("category") == c) for c in sorted({str(g.get("category")) for g in planned_groups})},
        "planned_input_tokens": sum(int(g.get("est_input_tokens") or 0) for g in planned_groups),
        "spec_pages": spec_pages,
        "spec_pages_sent": spec_sent,
        "spec_pages_share": round(spec_sent / spec_pages, 4) if spec_pages else None,
        "ledger_est_tokens": ledger.get("est_tokens"),
        "ledger_images": ledger.get("est_images"),
        "legend_entries": len(ledger.get("legend") or []),
        "legend_pages": [p.get("sheet_number") or p.get("title") for p in ledger.get("legend_pages") or []],
        "problems": problems,
        "facts_timing": timing,
    }
    ctx.note(f"plan: {len(planned_groups)} planned groups, {summary['planned_input_tokens']} input tokens, spec pages sent {spec_sent} of {spec_pages}")
    return {
        "documents": documents,
        "page_meta": page_meta,
        "ledger": ledger,
        "groups": planned_groups + stored_groups,
        "planned_groups": planned_groups,
        "planned_requests": requests,
        "plan_summary": summary,
    }


def register() -> Any:
    """Register the stage with plans_reader_replay (idempotent)."""
    from app.services import plans_reader_replay as rp

    return rp.register_stage(
        "plan",
        plan_stage,
        version=PLAN_VERSION,
        reads=("documents", "ledger", "groups"),
        writes=("documents", "page_meta", "ledger", "groups", "planned_groups", "planned_requests", "plan_summary"),
        owner="WS1",
    )


__all__ = [
    "PLAN_VERSION",
    "ReplayDb",
    "replay_settings",
    "extract_facts",
    "clear_facts_cache",
    "facts_documents",
    "build_planned_requests",
    "facts_block_coverage",
    "plan_stage",
    "register",
]

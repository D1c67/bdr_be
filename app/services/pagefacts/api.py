"""The page facts read API (spec 5.3 `pagefacts.api`, WS1 step 11).

Every function reads the facts the `plans_reader_facts` job cached
(`plans_reader_doc_facts`, `plans_reader_page_facts`, the per-page word
blobs in storage) through a sync Supabase client and raises
`FactsUnavailable` when a document has no finished facts: nothing here ever
fabricates a sheet number, a count or a scale. Callers that can live
without facts catch `FactsUnavailable` and fall back to the pre-facts path.

Caches (in-process, per worker): `page_words` keeps the 64 most recently
read pages decoded (the 5.3 "in-process LRU of 64 pages"); document facts
keep a small LRU keyed by (sha256, kind, extractor_version). `clear_caches()`
drops both (tests, and a worker that re-extracts after a version bump).

Run-level lookups (`sheet_index`, `resolve_sheet`, `legend_lookup`,
`schedule_lookup`, `keynote_lookup`, `panel_locations`, `spec_lookup`,
`ledger_facts`) take the run's document rows and number them D1..Dn in the
same order the prompts do (category rank, created_at, id). Citation refs
follow spec 5.2: `t:{doc8}:{page}:{line}`, `s:{doc8}:{sN}:{row}`,
`l:{doc8}:{entry_id}`, `sp:{doc8}:{page}:{section}`.

Sync Supabase SDK only (never inside `async def`); no model; no pricing.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from app.services.pagefacts import block as block_mod
from app.services.pagefacts import ledger as ledger_mod
from app.services.pagefacts import legend as legend_mod
from app.services.pagefacts import schedules as schedules_mod
from app.services.pagefacts import specs as specs_mod
from app.services.pagefacts import store as store_mod
from app.services.pagefacts import tags as tags_mod
from app.services.pagefacts.contracts import (
    EXTRACTOR_VERSION,
    DocFacts,
    FactsUnavailable,
    Line,
    PageFacts,
    PageWords,
    Word,
)
from app.services.pagefacts.extract import kind_for_label
from app.services.plans_reader_schemas import CATEGORIES

logger = logging.getLogger(__name__)

WORDS_LRU_PAGES = 64
DOC_LRU_SIZE = 16
DOC_TABLE = "plans_reader_doc_facts"
PAGE_TABLE = "plans_reader_page_facts"
DOCUMENTS_TABLE = "plans_reader_documents"

_lock = threading.Lock()
_words_lru: OrderedDict[tuple[str, str, int], tuple[list[Word], list[Line]]] = OrderedDict()
_doc_lru: OrderedDict[store_mod.CacheKey, DocFacts] = OrderedDict()


def clear_caches() -> None:
    with _lock:
        _words_lru.clear()
        _doc_lru.clear()


def doc8(document_id: str) -> str:
    return str(document_id or "")[:8]


# Document rows and keys ----------------------------------------------------------


def document_row(sb: Any, document_id: str) -> dict[str, Any]:
    rows = (
        sb.table(DOCUMENTS_TABLE).select("*").eq("id", str(document_id)).limit(1).execute().data or []
    )
    if not rows:
        raise FactsUnavailable(f"document {document_id} does not exist")
    return rows[0]


def document_kind(doc: dict[str, Any]) -> str:
    """`drawing` or `text` for a document row: from the label the user gave
    (`category_label`), else the category it is read under."""
    return kind_for_label(doc.get("category_label") or doc.get("category"))


def cache_key_for(doc: dict[str, Any]) -> store_mod.CacheKey:
    sha = doc.get("sha256")
    if not sha:
        raise FactsUnavailable(f"document {doc.get('id')} has no sha256 (facts never ran)")
    return store_mod.cache_key(str(sha), document_kind(doc), EXTRACTOR_VERSION)


def _require_done(doc: dict[str, Any]) -> None:
    status = doc.get("facts_status")
    if status != "done":
        raise FactsUnavailable(
            f"document {doc.get('id')} facts are {status or 'not started'}"
            + (f": {doc.get('facts_error')}" if doc.get("facts_error") else "")
        )


def _load_doc_facts(sb: Any, key: store_mod.CacheKey) -> DocFacts:
    sha, kind, version = key
    rows = (
        sb.table(DOC_TABLE)
        .select("*")
        .eq("sha256", sha)
        .eq("kind", kind)
        .eq("extractor_version", version)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        raise FactsUnavailable(f"no cached facts for {sha[:8]} ({kind}, {version})")
    pages = (
        sb.table(PAGE_TABLE)
        .select("*")
        .eq("sha256", sha)
        .eq("kind", kind)
        .eq("extractor_version", version)
        .order("page")
        .execute()
        .data
        or []
    )
    payload = dict(rows[0].get("facts") or {})
    payload.setdefault("sha256", sha)
    payload.setdefault("kind", kind)
    payload.setdefault("extractor_version", version)
    payload["pages"] = [p.get("facts") or {} for p in sorted(pages, key=lambda r: int(r.get("page") or 0))]
    return DocFacts.from_dict(payload)


def cached_doc_facts(sb: Any, key: store_mod.CacheKey) -> DocFacts:
    """The DocFacts of one cache key (LRU, then the two tables)."""
    with _lock:
        hit = _doc_lru.get(key)
        if hit is not None:
            _doc_lru.move_to_end(key)
            return hit
    facts = _load_doc_facts(sb, key)
    with _lock:
        _doc_lru[key] = facts
        while len(_doc_lru) > DOC_LRU_SIZE:
            _doc_lru.popitem(last=False)
    return facts


def doc_facts(sb: Any, document_id: str) -> DocFacts:
    """The finished facts of one run document."""
    doc = document_row(sb, document_id)
    return doc_facts_for_row(sb, doc)


def doc_facts_for_row(sb: Any, doc: dict[str, Any]) -> DocFacts:
    _require_done(doc)
    return cached_doc_facts(sb, cache_key_for(doc))


def page_facts(sb: Any, document_id: str, page: int) -> PageFacts:
    facts = doc_facts(sb, document_id)
    found = facts.page(int(page))
    if found is None:
        raise FactsUnavailable(f"document {document_id} has no facts for page {page}")
    return found


def page_words(sb: Any, document_id: str, page: int) -> tuple[list[Word], list[Line]]:
    """The positioned words and lines of one page from the cached blob
    (in-process LRU of 64 pages)."""
    doc = document_row(sb, document_id)
    _require_done(doc)
    sha = str(doc.get("sha256") or "")
    if not sha:
        raise FactsUnavailable(f"document {document_id} has no sha256")
    slot = (sha, EXTRACTOR_VERSION, int(page))
    with _lock:
        hit = _words_lru.get(slot)
        if hit is not None:
            _words_lru.move_to_end(slot)
            return hit
    pw = page_words_blob(sha, int(page))
    out = (pw.words, pw.lines)
    with _lock:
        _words_lru[slot] = out
        while len(_words_lru) > WORDS_LRU_PAGES:
            _words_lru.popitem(last=False)
    return out


def page_words_blob(sha256: str, page: int, version: str = EXTRACTOR_VERSION) -> PageWords:
    """Download and decode one word blob (no cache)."""
    from app.services import storage

    path = store_mod.blob_path(sha256, page, version)
    try:
        data = storage.download_file(path)
    except Exception as exc:  # noqa: BLE001 - storage errors become unavailable, never fabricated words
        raise FactsUnavailable(f"word blob {path} could not be read ({type(exc).__name__})") from exc
    try:
        return store_mod.decode_blob(data)
    except Exception as exc:  # noqa: BLE001
        raise FactsUnavailable(f"word blob {path} is not readable ({type(exc).__name__})") from exc


# Run-level facts ------------------------------------------------------------------


def doc_order(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run document order (the D1..Dn numbering of every prompt): category
    rank in the schema vocabulary, then created_at, then id. The same key
    as plans_reader_agents.run_doc_order, repeated here so this package
    never imports the agents module."""
    rank = {c: i for i, c in enumerate(CATEGORIES)}
    return sorted(
        documents,
        key=lambda d: (rank.get(d.get("category"), len(rank)), str(d.get("created_at") or ""), str(d.get("id") or "")),
    )


@dataclass
class RunFacts:
    """The facts of one run's documents, loaded once: `facts` by document
    id (finished documents only), `refs` the D<n> labels, `missing` the
    reason per document without facts."""

    documents: list[dict[str, Any]] = field(default_factory=list)
    facts: dict[str, DocFacts] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)
    missing: dict[str, str] = field(default_factory=dict)

    def doc(self, document_id: str) -> dict[str, Any]:
        for d in self.documents:
            if str(d.get("id")) == str(document_id):
                return d
        raise FactsUnavailable(f"document {document_id} is not in this run")

    def get(self, document_id: str) -> DocFacts:
        facts = self.facts.get(str(document_id))
        if facts is None:
            raise FactsUnavailable(self.missing.get(str(document_id)) or f"document {document_id} has no facts")
        return facts

    def has(self, document_id: str) -> bool:
        return str(document_id) in self.facts

    def pairs(self) -> list[tuple[dict[str, Any], DocFacts]]:
        return [(d, self.facts[str(d["id"])]) for d in self.documents if str(d.get("id")) in self.facts]

    def by_doc8(self, prefix: str) -> dict[str, Any]:
        hits = [d for d in self.documents if doc8(str(d.get("id"))) == prefix]
        if len(hits) != 1:
            raise FactsUnavailable(f"document ref {prefix} is ambiguous or unknown")
        return hits[0]

    def ref(self, document_id: str) -> str:
        return self.refs.get(str(document_id)) or f"D? ({doc8(str(document_id))})"


def load_run_facts(sb: Any, documents: list[dict[str, Any]]) -> RunFacts:
    """Facts for every finished document of a run (rows from
    plans_reader_documents, any order). Documents without finished facts
    are listed in `missing` with the reason; nothing raises here."""
    ordered = [d for d in doc_order(documents) if d.get("status") != "failed"]
    run = RunFacts(documents=ordered)
    for n, d in enumerate(ordered, start=1):
        doc_id = str(d.get("id"))
        run.refs[doc_id] = f"D{n}"
        try:
            run.facts[doc_id] = doc_facts_for_row(sb, d)
        except FactsUnavailable as exc:
            run.missing[doc_id] = str(exc)
        except Exception as exc:  # noqa: BLE001 - a broken cache row is unavailable, never fatal
            logger.exception("pagefacts: loading facts of document %s failed", doc_id)
            run.missing[doc_id] = f"{type(exc).__name__}: {exc}"[:200]
    return run


def _run(sb: Any, documents: list[dict[str, Any]] | RunFacts) -> RunFacts:
    return documents if isinstance(documents, RunFacts) else load_run_facts(sb, documents)


def _is_drawing(doc: dict[str, Any], facts: DocFacts) -> bool:
    return facts.kind == "drawing" or str(doc.get("category") or "").endswith("_drawings")


def sheet_index(sb: Any, documents: list[dict[str, Any]] | RunFacts) -> list[dict[str, Any]]:
    """`sheet_index` tool rows: one per drawing page with facts."""
    run = _run(sb, documents)
    out: list[dict[str, Any]] = []
    for doc, facts in run.pairs():
        if not _is_drawing(doc, facts):
            continue
        for p in facts.pages:
            out.append(
                {
                    "sheet_number": p.sheet_number,
                    "doc_ref": f"{run.ref(str(doc['id']))} P{p.page}",
                    "document_id": str(doc["id"]),
                    "page": p.page,
                    "title": p.sheet_title,
                    "discipline": p.discipline,
                    "category": doc.get("category"),
                    "roles": list(p.roles),
                    "page_role": p.page_role,
                    "views": [
                        {"view_id": v.view_id, "title": v.title, "scale_text": v.scale_text, "ft_per_in": v.ft_per_in, "kind": v.kind, "region": v.region}
                        for v in p.views
                        if not v.example
                    ],
                    "primary_ft_per_in": p.primary_ft_per_in,
                    "scale_conflict": p.scale_conflict,
                    "w_in": p.w_in,
                    "h_in": p.h_in,
                    "text_layer_quality": p.text_layer_quality,
                }
            )
    if not out:
        raise FactsUnavailable("no drawing document of this run has finished facts")
    return out


def _norm_sheet(text: Any) -> str:
    """Letters and digits only, upper case: `ED2.01`, `ED2-01` and `ed 2 01`
    are one sheet (the first hit wins; the others come back as `also`)."""
    return "".join(ch for ch in str(text or "").upper() if ch.isalnum())


def resolve_sheet(sb: Any, documents: list[dict[str, Any]] | RunFacts, sheet: str) -> dict[str, Any]:
    """{document_id, page, sheet_number} for a sheet number (case and
    separator insensitive: `E-101`, `E101` and `e 101` are one sheet)."""
    run = _run(sb, documents)
    want = _norm_sheet(sheet)
    if not want:
        raise FactsUnavailable("a sheet number is required")
    hits: list[dict[str, Any]] = []
    for doc, facts in run.pairs():
        if not _is_drawing(doc, facts):
            continue
        for p in facts.pages:
            if p.sheet_number and _norm_sheet(p.sheet_number) == want:
                hits.append({"document_id": str(doc["id"]), "page": p.page, "sheet_number": p.sheet_number, "doc_ref": f"{run.ref(str(doc['id']))} P{p.page}"})
    if not hits:
        raise FactsUnavailable(f"no sheet {sheet!r} in the text-layer sheet index")
    return hits[0] if len(hits) == 1 else {**hits[0], "also": hits[1:]}


def legend_lookup(
    sb: Any,
    documents: list[dict[str, Any]] | RunFacts,
    query: str | None = None,
    *,
    effect: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """`legend_lookup` tool result over every drawing document's legend:
    entries with refs `l:{doc8}:{entry_id}`, modifiers with `applies_to`."""
    run = _run(sb, documents)
    entries: list[dict[str, Any]] = []
    modifiers: list[dict[str, Any]] = []
    seen_mod: set[str] = set()
    for doc, facts in run.pairs():
        if not _is_drawing(doc, facts) or not facts.legend:
            continue
        found = legend_mod.lookup(facts.legend, query, effect=effect, limit=limit)
        d8 = doc8(str(doc["id"]))
        for e in found.get("entries") or []:
            entries.append(
                {
                    "ref": f"l:{d8}:{e.get('entry_id')}",
                    "document_id": str(doc["id"]),
                    "page": e.get("page"),
                    "tag": e.get("tag"),
                    "description": e.get("description"),
                    "device_type": e.get("device_type"),
                    "group": e.get("group"),
                    "spec_section": e.get("spec_section"),
                    "used_on_pages": list(e.get("used_on_pages") or []),
                    "alternates": list(e.get("alternates") or []),
                }
            )
        for m in found.get("modifiers") or []:
            key = f"{m.get('key')}|{m.get('text')}"
            if key in seen_mod:
                continue
            seen_mod.add(key)
            modifiers.append({"modifier_id": m.get("modifier_id"), "key": m.get("key"), "text": m.get("text"), "effect": m.get("effect"), "applies_to": m.get("applies_to")})
    if not run.pairs():
        raise FactsUnavailable("no document of this run has finished facts")
    return {"entries": entries[:limit], "modifiers": modifiers[:limit]}


def _schedule_rows(doc_id: str, sched: dict[str, Any]) -> list[dict[str, Any]]:
    d8 = doc8(doc_id)
    sid = sched.get("schedule_id")
    rows: list[dict[str, Any]] = []
    if sched.get("kind") == "panel" and sched.get("panel"):
        for c in sched["panel"].get("circuits") or []:
            key = str(c.get("ckt"))
            rows.append({"ref": f"s:{d8}:{sid}:{key}", "key": key, "cells": list(c.get("raw") or []), "parsed": {k: v for k, v in c.items() if k not in ("raw", "bbox")}, "bbox": c.get("bbox")})
    elif sched.get("kind") == "luminaire":
        for lum in sched.get("luminaires") or []:
            key = str(lum.get("tag"))
            rows.append({"ref": f"s:{d8}:{sid}:{key}", "key": key, "cells": [], "parsed": {k: v for k, v in lum.items() if k != "bbox"}, "bbox": lum.get("bbox")})
    else:
        for i, raw in enumerate(sched.get("rows_raw") or [], start=1):
            rows.append({"ref": f"s:{d8}:{sid}:{i}", "key": str(i), "cells": list(raw), "parsed": None, "bbox": None})
    return rows


def schedule_lookup(
    sb: Any,
    documents: list[dict[str, Any]] | RunFacts,
    *,
    document_id: str | None = None,
    kind: str = "any",
    name: str | None = None,
    row_key: str | None = None,
) -> dict[str, Any]:
    """`get_schedule` tool result: schedules with run-unique ids `doc8:sN`
    and rows with refs `s:{doc8}:{sN}:{row key}` (the circuit number of a
    panel row, the tag of a luminaire row)."""
    run = _run(sb, documents)
    wanted_kind = kind or "any"
    if wanted_kind == "fixture":
        wanted_kind = "luminaire"
    out: list[dict[str, Any]] = []
    for doc, facts in run.pairs():
        if document_id and str(doc["id"]) != str(document_id):
            continue
        if not _is_drawing(doc, facts):
            continue
        found = schedules_mod.lookup(facts.schedules, kind=wanted_kind if wanted_kind != "any" else None, name=name)
        for sched in found:
            rows = _schedule_rows(str(doc["id"]), sched)
            if row_key:
                rows = [r for r in rows if r["key"].upper() == str(row_key).upper()]
            page = next((p for p in facts.pages if p.page == sched.get("page")), None)
            out.append(
                {
                    "schedule_id": f"{doc8(str(doc['id']))}:{sched.get('schedule_id')}",
                    "kind": sched.get("kind"),
                    "name": (sched.get("panel") or {}).get("name") if sched.get("kind") == "panel" else sched.get("title"),
                    "title": sched.get("title"),
                    "document_id": str(doc["id"]),
                    "page": sched.get("page"),
                    "sheet_number": page.sheet_number if page else None,
                    "bbox": sched.get("bbox"),
                    "columns": list(sched.get("columns") or []),
                    "parse_confidence": sched.get("parse_confidence"),
                    "source": sched.get("source") or "text_layer",
                    "rows": rows,
                }
            )
    if not run.pairs():
        raise FactsUnavailable("no document of this run has finished facts")
    return {"schedules": out}


def keynote_lookup(
    sb: Any,
    documents: list[dict[str, Any]] | RunFacts,
    sheet: str,
    number: str | int | None = None,
) -> dict[str, Any]:
    """The keynotes of one sheet (exact text) and the keynote marker
    candidates in its plan area."""
    run = _run(sb, documents)
    where = resolve_sheet(sb, run, sheet)
    facts = run.get(where["document_id"]).page(int(where["page"]))
    if facts is None:
        raise FactsUnavailable(f"no facts for sheet {sheet}")
    items: list[dict[str, Any]] = []
    for blk in facts.notes:
        if blk.get("kind") != "keynotes":
            continue
        for it in blk.get("items") or []:
            if number is not None and str(it.get("number")) != str(number):
                continue
            items.append({"number": it.get("number"), "text": it.get("text"), "bbox": it.get("bbox"), "label": blk.get("label")})
    markers = [m for m in facts.keynote_markers if number is None or str(m.get("number")) == str(number)]
    return {"document_id": where["document_id"], "page": where["page"], "sheet_number": where["sheet_number"], "items": items, "markers": markers}


def _tag_items(facts: PageFacts, *, panel: str | None, circuit: int | None, status: str | None, type_mark: str | None) -> list[dict[str, Any]]:
    tags = facts.tags or {}
    out: list[dict[str, Any]] = []
    if status:
        for key, pts in (tags.get("status") or {}).items():
            if key.upper() == status.strip("()").upper():
                out.extend({"kind": "status", "value": key, "x": x, "y": y} for x, y in pts)
        return out
    if type_mark:
        for key, pts in (tags.get("type_marks") or {}).items():
            if key.upper() == type_mark.upper():
                out.extend({"kind": "type_mark", "value": key, "x": x, "y": y} for x, y in pts)
        return out
    for t in tags.get("circuit") or []:
        if panel and str(t.get("panel") or "").upper() != panel.upper():
            continue
        if circuit is not None and int(circuit) not in (t.get("circuits") or []):
            continue
        out.append({"kind": "circuit", "panel": t.get("panel"), "circuits": list(t.get("circuits") or []), "raw": t.get("raw"), "x": t.get("x"), "y": t.get("y"), "fixture_type": t.get("fixture_type"), "mods": list(t.get("mods") or [])})
    return out


def tag_occurrences(
    sb: Any,
    document_id: str,
    page: int,
    *,
    panel: str | None = None,
    circuit: int | None = None,
    status: str | None = None,
    type_mark: str | None = None,
) -> list[dict[str, Any]]:
    """Text-layer tag occurrences of one page: circuit tags (filtered by
    panel and circuit), status tags `(X)`, or luminaire type marks."""
    facts = page_facts(sb, document_id, page)
    if facts.text_layer_quality != "full":
        raise FactsUnavailable(f"page {page} of {document_id} has no usable text layer ({facts.text_layer_quality})")
    return _tag_items(facts, panel=panel, circuit=circuit, status=status, type_mark=type_mark)


def tag_regions(
    sb: Any,
    document_id: str,
    page: int,
    *,
    pad_in: float = 1.0,
    max_regions: int = 4,
    panel: str | None = None,
    circuit: int | None = None,
    status: str | None = None,
    type_mark: str | None = None,
) -> list[list[float]]:
    """Padded cluster boxes (page fractions) around the matching tags, the
    largest clusters first, at most `max_regions` (escalation and zoom
    targets)."""
    facts = page_facts(sb, document_id, page)
    items = tag_occurrences(sb, document_id, page, panel=panel, circuit=circuit, status=status, type_mark=type_mark)
    points = [[float(i["x"]), float(i["y"])] for i in items if i.get("x") is not None]
    if not points:
        return []
    return tags_mod.tag_regions(points, facts.w_in, facts.h_in, pad_in=pad_in, max_regions=max_regions)


def panel_locations(sb: Any, documents: list[dict[str, Any]] | RunFacts) -> list[dict[str, Any]]:
    run = _run(sb, documents)
    out: list[dict[str, Any]] = []
    for doc, facts in run.pairs():
        if not _is_drawing(doc, facts):
            continue
        for loc in facts.panel_locations:
            page = next((p for p in facts.pages if p.page == loc.get("page")), None)
            out.append({"document_id": str(doc["id"]), "sheet_number": page.sheet_number if page else None, **loc})
    if not run.pairs():
        raise FactsUnavailable("no document of this run has finished facts")
    return out


def views(sb: Any, document_id: str, page: int) -> list[dict[str, Any]]:
    facts = page_facts(sb, document_id, page)
    return [v.to_dict() for v in facts.views]


def spec_lookup(
    sb: Any,
    documents: list[dict[str, Any]] | RunFacts,
    *,
    section: str | None = None,
    keyword: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """`spec_lookup` tool result over every text document's section map:
    hits with refs `sp:{doc8}:{page}:{section}`, the tier (A sent whole, B
    snippets, C title only) and the snippet texts of tier B sections."""
    run = _run(sb, documents)
    hits: list[dict[str, Any]] = []
    for doc, facts in run.pairs():
        spec = facts.spec if isinstance(facts.spec, dict) else None
        if not spec:
            continue
        snippets = (spec.get("selection") or {}).get("tier_b_snippets") or []
        for e in specs_mod.lookup(spec, section=section, keyword=keyword, limit=limit):
            texts = [s.get("text") for s in snippets if s.get("section") == e.get("section")]
            hits.append(
                {
                    "ref": f"sp:{doc8(str(doc['id']))}:{e.get('page_start')}:{e.get('section')}",
                    "document_id": str(doc["id"]),
                    "section": e.get("section"),
                    "title": e.get("title"),
                    "page": e.get("page_start"),
                    "page_end": e.get("page_end"),
                    "tier": e.get("tier"),
                    "sent": "pages" if e.get("tier") == "A" else ("snippets" if e.get("tier") == "B" else "title_only"),
                    "text": e.get("text") or ("\n".join(t for t in texts if t) or None),
                }
            )
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break
    if not any(isinstance(f.spec, dict) and f.spec for _d, f in run.pairs()):
        raise FactsUnavailable("no specification document of this run has finished facts")
    return {"hits": hits[:limit]}


def page_text_block(facts: PageFacts, doc: DocFacts | dict[str, Any] | None = None, *, max_tokens: int = 2500) -> str:
    """The facts block printed after a page image (`block.page_block`)."""
    return block_mod.page_block(facts, doc, max_tokens=max_tokens)


def ledger_facts(sb: Any, documents: list[dict[str, Any]] | RunFacts, *, caps: ledger_mod.LedgerCaps | None = None) -> dict[str, Any]:
    """The `ledger["facts"]` block for a run (see `pagefacts.ledger`)."""
    run = _run(sb, documents)
    if not run.pairs():
        raise FactsUnavailable("no document of this run has finished facts")
    if caps is None:
        from app.core.config import get_settings

        caps = ledger_mod.caps_from_settings(get_settings())
    return ledger_mod.ledger_facts(run.pairs(), caps=caps)


def note_blocks(sb: Any, document_id: str, page: int) -> list[dict[str, Any]]:
    """The note blocks of one page (keynotes, sheet notes, general notes:
    exact text, numbered items)."""
    return list(page_facts(sb, document_id, page).notes)


__all__ = [
    "WORDS_LRU_PAGES",
    "RunFacts",
    "clear_caches",
    "doc8",
    "document_row",
    "document_kind",
    "cache_key_for",
    "cached_doc_facts",
    "doc_facts",
    "doc_facts_for_row",
    "page_facts",
    "page_words",
    "page_words_blob",
    "doc_order",
    "load_run_facts",
    "sheet_index",
    "resolve_sheet",
    "legend_lookup",
    "schedule_lookup",
    "keynote_lookup",
    "tag_occurrences",
    "tag_regions",
    "panel_locations",
    "views",
    "spec_lookup",
    "page_text_block",
    "ledger_facts",
    "note_blocks",
]

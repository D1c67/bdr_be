"""The one adapter through which WS8 evidence reads WS1 page facts
(docs/AI_ESTIMATING_BUILD_SPEC.md section 6 WS8, "adapter with fakes";
section 5.3 Word APIs).

Evidence (anchors, placement, polylines, chips) never imports a page-facts
module directly. It asks an `EvidenceInputs` provider for a `SheetInputs`
(words in the displayed frame, regions, views with their scales, page size
and text layer quality) and for a census, so a change in WS1's shapes touches
this module only.

Providers:

- `FactsStoreInputs`: reads a `pagefacts.store.FactsStore` (the in-memory
  store today, the database store of migration MB behind the same protocol
  in wave 2). Missing facts or words raise `FactsUnavailable`.
- `LocalPdfInputs`: offline (replay with `ctx.sb is None`, golden tests,
  scripts). Runs `pagefacts.extract.extract_document` once per document and
  `extract_words` once per requested page, then serves them through a
  `MemoryFactsStore`. No database, no network, no model.
- `PagefactsApiInputs`: the `pagefacts.api` functions (`page_facts`,
  `page_words`) when that module exists (WS1 step 11, wave 2). Until then
  every read raises `FactsUnavailable`.
- `FakeEvidenceInputs`: canned sheets for tests.
- `UnavailableEvidenceInputs`: the default; every sheet read raises
  `FactsUnavailable` and every census answers the typed unavailable result.

`inputs_for(sb, pdfs=...)` picks one. Callers treat `FactsUnavailable` as
"unknown", never as an empty sheet (text counts are floors; spec 5.1).

Pure apart from PDFium inside `LocalPdfInputs` (through `pagefacts`, which
holds the PDFium lock). Nothing here carries prices.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.services.pagefacts.contracts import (
    TEXT_LAYER_QUALITY_FULL,
    FactsUnavailable,
    PageFacts,
    PageWords,
    Region,
    View,
    Word,
    text_layer_quality_for,
)
from app.services.plans_reader_tools import services, unavailable_result

SOURCE_FACTS_STORE = "facts_store"
SOURCE_LOCAL_PDF = "local_pdf"
SOURCE_API = "pagefacts_api"
SOURCE_FAKE = "fake"

__all__ = [
    "EvidenceInputs",
    "FactsStoreInputs",
    "FactsUnavailable",
    "FakeEvidenceInputs",
    "LocalPdfInputs",
    "PagefactsApiInputs",
    "SheetInputs",
    "UnavailableEvidenceInputs",
    "inputs_for",
    "sheet_inputs_from",
]


@dataclass(slots=True)
class SheetInputs:
    """Everything evidence needs from one drawing page, in the 5.1 frame
    (displayed page fractions, origin top-left). `words` keep WS1's order so
    a word index is a stable run index for `t:` refs. `views` are WS1 views
    (legend examples included; consumers skip `example`). `roles` are WS1's
    multi-label page roles (empty when unknown)."""

    document_id: str
    page: int
    w_in: float
    h_in: float
    rotation: int
    text_class: str
    words: list[Word] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    views: list[View] = field(default_factory=list)
    primary_ft_per_in: float | None = None
    scale_conflict: bool = False
    sheet_number: str | None = None
    page_role: str | None = None
    roles: list[str] = field(default_factory=list)
    source: str = SOURCE_FAKE

    @property
    def text_layer_quality(self) -> str:
        return text_layer_quality_for(self.text_class)

    @property
    def text_layer_usable(self) -> bool:
        return self.text_layer_quality == TEXT_LAYER_QUALITY_FULL

    @property
    def sheet_key(self) -> str:
        return f"{self.document_id[:8]}:{self.page}"


def sheet_inputs_from(
    document_id: str,
    words: PageWords | None,
    facts: PageFacts | None,
    *,
    source: str,
) -> SheetInputs:
    """Assemble a SheetInputs from WS1's per-page words blob and facts row.
    Either may be missing (but not both): words without facts give no
    regions or views; facts without words give no words."""
    if words is None and facts is None:
        raise FactsUnavailable(f"No page facts for {document_id} page.")
    if facts is not None:
        return SheetInputs(
            document_id=document_id,
            page=facts.page,
            w_in=facts.w_in,
            h_in=facts.h_in,
            rotation=facts.rotation,
            text_class=facts.text_class,
            words=list(words.words) if words is not None else [],
            regions=list(facts.regions),
            views=list(facts.views),
            primary_ft_per_in=facts.primary_ft_per_in,
            scale_conflict=facts.scale_conflict,
            sheet_number=facts.sheet_number,
            page_role=facts.page_role,
            roles=list(facts.roles),
            source=source,
        )
    assert words is not None
    return SheetInputs(
        document_id=document_id,
        page=int(words.page or 0),
        w_in=words.page_w_in,
        h_in=words.page_h_in,
        rotation=words.rotation,
        text_class=words.text_class,
        words=list(words.words),
        source=source,
    )


class EvidenceInputs(Protocol):
    """What evidence reads from WS1. `sheet` raises `FactsUnavailable` when
    there are no facts for the page; `census` answers the WS1 census result
    shape (spec 5.2 `census`) or the typed unavailable result."""

    source: str

    def sheet(self, document_id: str, page: int) -> SheetInputs: ...

    def census(
        self,
        document_id: str,
        page: int,
        pattern: dict[str, Any],
        region: list[float] | None = None,
    ) -> dict[str, Any]: ...


def _registry_census(
    document_id: str, page: int, pattern: dict[str, Any], region: list[float] | None
) -> dict[str, Any]:
    """The census through the WS2 service registry (WS1 registers its
    CensusService there; the default is the typed unavailable result)."""
    try:
        return services().census.census(document_id, page, pattern, region)
    except FactsUnavailable as exc:
        return unavailable_result("census", str(exc))


class UnavailableEvidenceInputs:
    """Default provider: no page facts anywhere."""

    source = "unavailable"

    def sheet(self, document_id: str, page: int) -> SheetInputs:
        raise FactsUnavailable(f"Page facts are not available for {document_id} page {page}.")

    def census(
        self,
        document_id: str,
        page: int,
        pattern: dict[str, Any],
        region: list[float] | None = None,
    ) -> dict[str, Any]:
        return unavailable_result("census", "evidence inputs unavailable")


class FakeEvidenceInputs:
    """Canned sheets keyed by (document_id, page) and canned censuses keyed
    by (document_id, page, pattern value)."""

    source = SOURCE_FAKE

    def __init__(
        self,
        sheets: dict[tuple[str, int], SheetInputs] | None = None,
        censuses: dict[tuple[str, int, str], dict[str, Any]] | None = None,
    ) -> None:
        self._sheets = dict(sheets or {})
        self._censuses = dict(censuses or {})
        self.calls: list[tuple[str, str, int]] = []

    def add(self, sheet: SheetInputs) -> None:
        self._sheets[(sheet.document_id, sheet.page)] = sheet

    def sheet(self, document_id: str, page: int) -> SheetInputs:
        self.calls.append(("sheet", document_id, page))
        hit = self._sheets.get((document_id, page))
        if hit is None:
            raise FactsUnavailable(f"No canned sheet for {document_id} page {page}.")
        return hit

    def census(
        self,
        document_id: str,
        page: int,
        pattern: dict[str, Any],
        region: list[float] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("census", document_id, page))
        hit = self._censuses.get((document_id, page, str(pattern.get("value", ""))))
        if hit is None:
            return unavailable_result("census", "no canned census")
        return dict(hit)


class FactsStoreInputs:
    """Reads a `pagefacts.store.FactsStore`. `keys` maps a document id to its
    facts cache key `(sha256, kind, extractor_version)`."""

    source = SOURCE_FACTS_STORE

    def __init__(self, store: Any, keys: dict[str, tuple[str, str, str]]) -> None:
        self._store = store
        self._keys = dict(keys)

    def sheet(self, document_id: str, page: int) -> SheetInputs:
        key = self._keys.get(document_id)
        if key is None:
            raise FactsUnavailable(f"No facts key for document {document_id}.")
        facts = self._store.get(key)
        page_facts = facts.page(page) if facts is not None else None
        words = self._store.words(key, page)
        if page_facts is None and words is None:
            raise FactsUnavailable(f"No page facts for {document_id} page {page}.")
        out = sheet_inputs_from(document_id, words, page_facts, source=self.source)
        out.page = page
        return out

    def census(
        self,
        document_id: str,
        page: int,
        pattern: dict[str, Any],
        region: list[float] | None = None,
    ) -> dict[str, Any]:
        return _registry_census(document_id, page, pattern, region)


class LocalPdfInputs:
    """Offline provider over PDF bytes: `pdfs` maps a document id to its
    bytes, `labels` (optional) to its run label. Extraction is lazy and
    cached per document (facts) and per page (words)."""

    source = SOURCE_LOCAL_PDF

    def __init__(
        self,
        pdfs: dict[str, bytes],
        *,
        labels: dict[str, str] | None = None,
        page_budget_s: float | None = 8.0,
        doc_budget_s: float | None = 600.0,
    ) -> None:
        from app.services.pagefacts.store import MemoryFactsStore

        self._pdfs = dict(pdfs)
        self._labels = dict(labels or {})
        self._budgets = {"page_budget_s": page_budget_s, "doc_budget_s": doc_budget_s}
        self._store = MemoryFactsStore()
        self._keys: dict[str, tuple[str, str, str]] = {}

    def _key(self, document_id: str) -> tuple[str, str, str]:
        from app.services.pagefacts import extract
        from app.services.pagefacts.store import cache_key

        key = self._keys.get(document_id)
        if key is not None:
            return key
        pdf = self._pdfs.get(document_id)
        if pdf is None:
            raise FactsUnavailable(f"No PDF bytes for document {document_id}.")
        try:
            facts = extract.extract_document(
                pdf, label=self._labels.get(document_id, "electrical_drawings"), **self._budgets
            )
        except ValueError as exc:
            raise FactsUnavailable(str(exc)) from exc
        key = cache_key(facts.sha256, facts.kind, facts.extractor_version)
        self._store.put(key, facts)
        self._keys[document_id] = key
        return key

    def sheet(self, document_id: str, page: int) -> SheetInputs:
        from app.services.pagefacts import extract

        key = self._key(document_id)
        if self._store.words(key, page) is None:
            try:
                words = extract.extract_words(self._pdfs[document_id], pages=[page])
            except ValueError as exc:
                raise FactsUnavailable(str(exc)) from exc
            facts = self._store.get(key)
            assert facts is not None
            self._store.put(key, facts, words)
        return FactsStoreInputs(self._store, self._keys).sheet(document_id, page)

    def census(
        self,
        document_id: str,
        page: int,
        pattern: dict[str, Any],
        region: list[float] | None = None,
    ) -> dict[str, Any]:
        return _registry_census(document_id, page, pattern, region)


class PagefactsApiInputs:
    """The WS1 database-backed api (`pagefacts.api.page_facts` and
    `page_words`, spec 5.3), imported lazily so this module works before the
    api lands. Any missing piece raises `FactsUnavailable`."""

    source = SOURCE_API

    def __init__(self, sb: Any) -> None:
        self._sb = sb

    @staticmethod
    def api() -> Any | None:
        try:
            return importlib.import_module("app.services.pagefacts.api")
        except ImportError:
            return None

    def sheet(self, document_id: str, page: int) -> SheetInputs:
        api = self.api()
        if api is None or self._sb is None:
            raise FactsUnavailable("The page facts api is not available.")
        facts = api.page_facts(self._sb, document_id, page)
        if facts is None:
            raise FactsUnavailable(f"No page facts for {document_id} page {page}.")
        words, _lines = api.page_words(self._sb, document_id, page)
        out = sheet_inputs_from(document_id, None, facts, source=self.source)
        out.words = list(words or [])
        return out

    def census(
        self,
        document_id: str,
        page: int,
        pattern: dict[str, Any],
        region: list[float] | None = None,
    ) -> dict[str, Any]:
        return _registry_census(document_id, page, pattern, region)


def inputs_for(
    sb: Any = None,
    *,
    pdfs: dict[str, bytes] | None = None,
    labels: dict[str, str] | None = None,
) -> EvidenceInputs:
    """PDF bytes given: the offline provider. A client and the WS1 api
    present: the api provider. Otherwise the unavailable default."""
    if pdfs:
        return LocalPdfInputs(pdfs, labels=labels)
    if sb is not None and PagefactsApiInputs.api() is not None:
        return PagefactsApiInputs(sb)
    return UnavailableEvidenceInputs()

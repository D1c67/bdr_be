"""Facts cache: key, blob layout and an in-memory store (WS1 step 8).

The cache key is `(sha256, kind, extractor_version)`: the same bytes labeled
`specifications` in run A and `electrical_drawings` in run B are two
entries (the pipelines differ), and bumping `EXTRACTOR_VERSION` invalidates
everything. Per-page words and lines live in gzip JSON blobs at
`plans-reader/_facts/{sha256}/{extractor_version}/p{page:04d}.json.gz`
(30 to 250 KB per 42x30 sheet); the DocFacts and PageFacts rows hold no
words. The database store (migration MB, `plans_reader_doc_facts` and
`plans_reader_page_facts`) lands in wave 2 behind the same `FactsStore`
protocol; `MemoryFactsStore` serves tests, replay and offline eval.
"""

from __future__ import annotations

import gzip
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.services.pagefacts.contracts import EXTRACTOR_VERSION, FACTS_SCHEMA, DocFacts, PageWords

BLOB_PREFIX = "plans-reader/_facts"
CacheKey = tuple[str, str, str]


def cache_key(sha256: str, kind: str, extractor_version: str = EXTRACTOR_VERSION) -> CacheKey:
    if not sha256 or len(sha256) != 64:
        raise ValueError("A facts cache key needs the document's sha256.")
    if kind not in ("drawing", "text"):
        raise ValueError(f"Unknown facts kind {kind!r} (drawing or text).")
    return (sha256.lower(), kind, extractor_version)


def blob_prefix(sha256: str, extractor_version: str = EXTRACTOR_VERSION) -> str:
    return f"{BLOB_PREFIX}/{sha256.lower()}/{extractor_version}"


def blob_path(sha256: str, page: int, extractor_version: str = EXTRACTOR_VERSION) -> str:
    if page < 1:
        raise ValueError("Pages are 1-based.")
    return f"{blob_prefix(sha256, extractor_version)}/p{page:04d}.json.gz"


def encode_blob(words: PageWords) -> bytes:
    payload = words.to_dict()
    payload["schema"] = FACTS_SCHEMA
    return gzip.compress(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        compresslevel=6,
    )


def decode_blob(data: bytes) -> PageWords:
    payload = json.loads(gzip.decompress(data).decode("utf-8"))
    if payload.get("schema") != FACTS_SCHEMA:
        raise ValueError(f"Unexpected facts blob schema {payload.get('schema')!r}.")
    return PageWords.from_dict(payload)


class FactsStore(Protocol):
    def get(self, key: CacheKey) -> DocFacts | None: ...

    def put(
        self, key: CacheKey, facts: DocFacts, words: dict[int, PageWords] | None = None
    ) -> None: ...

    def words(self, key: CacheKey, page: int) -> PageWords | None: ...

    def has(self, key: CacheKey) -> bool: ...


@dataclass
class MemoryFactsStore:
    """Dict-backed store: facts by key, blobs by (key, page) as the gzip
    bytes the storage layer would hold, plus a small LRU of decoded pages
    (the api's "64 pages" cache shape)."""

    lru_pages: int = 64
    _facts: dict[CacheKey, DocFacts] = field(default_factory=dict)
    _blobs: dict[tuple[CacheKey, int], bytes] = field(default_factory=dict)
    _lru: OrderedDict[tuple[CacheKey, int], PageWords] = field(default_factory=OrderedDict)

    def get(self, key: CacheKey) -> DocFacts | None:
        return self._facts.get(key)

    def has(self, key: CacheKey) -> bool:
        return key in self._facts

    def put(
        self, key: CacheKey, facts: DocFacts, words: dict[int, PageWords] | None = None
    ) -> None:
        self._facts[key] = facts
        for page, pw in (words or {}).items():
            self._blobs[(key, page)] = encode_blob(pw)
            self._lru.pop((key, page), None)

    def blob(self, key: CacheKey, page: int) -> bytes | None:
        return self._blobs.get((key, page))

    def blob_paths(self, key: CacheKey) -> list[str]:
        sha, _kind, version = key
        return [blob_path(sha, page, version) for (k, page) in sorted(self._blobs) if k == key]

    def words(self, key: CacheKey, page: int) -> PageWords | None:
        slot = (key, page)
        hit = self._lru.get(slot)
        if hit is not None:
            self._lru.move_to_end(slot)
            return hit
        data = self._blobs.get(slot)
        if data is None:
            return None
        pw = decode_blob(data)
        self._lru[slot] = pw
        while len(self._lru) > self.lru_pages:
            self._lru.popitem(last=False)
        return pw

    def clear(self) -> None:
        self._facts.clear()
        self._blobs.clear()
        self._lru.clear()


def facts_to_rows(facts: DocFacts) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The (doc row, page rows) shape of migration MB from one DocFacts:
    `plans_reader_doc_facts` keyed by (sha256, kind, extractor_version) and
    one `plans_reader_page_facts` row per page with the indexed columns
    beside the facts JSON."""
    doc_row = {
        "sha256": facts.sha256,
        "kind": facts.kind,
        "extractor_version": facts.extractor_version,
        "facts": facts.to_dict(include_pages=False),
        "blob_prefix": blob_prefix(facts.sha256, facts.extractor_version),
        "stats": dict(facts.stats),
    }
    page_rows = [
        {
            "sha256": facts.sha256,
            "kind": facts.kind,
            "extractor_version": facts.extractor_version,
            "page": p.page,
            "sheet_number": p.sheet_number,
            "sheet_title": p.sheet_title,
            "discipline": p.discipline,
            "page_role": p.page_role,
            "roles": list(p.roles),
            "text_class": p.text_class,
            "primary_ft_per_in": p.primary_ft_per_in,
            "facts": p.to_dict(),
        }
        for p in facts.pages
    ]
    return doc_row, page_rows

"""Shared plumbing for the wave 2 page-facts tests: a FakeDB seeded with
cached facts (the 7154 ELEC fixture or synthetic sets), an in-memory
storage for the word blobs, and the synthetic PDFs the job tests extract.

No network, no database, no model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services import storage
from app.services.pagefacts import extract, store
from app.services.pagefacts.contracts import EXTRACTOR_VERSION, DocFacts, PageWords
from tests.pagefacts_fixtures import make_pdf, plan_sheet, sheet
from tests.test_plans_reader_runs import FakeDB

FIXTURE = Path(__file__).parent / "fixtures/plans_reader/run_7154/page_facts_elec.json"


def fixture_facts(key: str = "elec") -> DocFacts:
    """DocFacts of the 7154 ELEC (or MECH) set from the committed fixture."""
    data = json.loads(FIXTURE.read_text())
    for entry in data["documents"]:
        if entry["key"] == key:
            return DocFacts.from_dict(entry["facts"])
    raise KeyError(key)


def install_storage(monkeypatch) -> dict[str, bytes]:
    """storage.download_file / upload_file over a dict."""
    blobs: dict[str, bytes] = {}

    def download(path: str) -> bytes:
        if path not in blobs:
            raise RuntimeError(f"missing object {path}")
        return blobs[path]

    def upload(path: str, content: bytes, content_type: str, *, upsert: bool = False) -> None:
        blobs[path] = bytes(content)

    monkeypatch.setattr(storage, "download_file", download)
    monkeypatch.setattr(storage, "upload_file", upload)
    return blobs


def seed_cache(db: FakeDB, blobs: dict[str, bytes], facts: DocFacts, words: dict[int, PageWords] | None = None) -> None:
    """The doc row, the page rows and the word blobs of one DocFacts."""
    doc_row, page_rows = store.facts_to_rows(facts)
    db.add("plans_reader_doc_facts", **doc_row)
    for row in page_rows:
        db.add("plans_reader_page_facts", **row)
    for page, pw in (words or {}).items():
        blobs[store.blob_path(facts.sha256, page, facts.extractor_version)] = store.encode_blob(pw)


def seed_document(
    db: FakeDB,
    run_id: str,
    facts: DocFacts | None,
    *,
    doc_id: str,
    category: str,
    name: str = "doc.pdf",
    facts_status: str | None = "done",
    category_label: str | None = None,
    page_meta: list[dict[str, Any]] | None = None,
    sha256: str | None = None,
    storage_path: str | None = None,
    facts_doc: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = {
        "id": doc_id,
        "run_id": run_id,
        "file_id": f"file-{doc_id}",
        "category": category,
        "name": name,
        "storage_path": storage_path or f"plans-reader/{run_id}/source/{doc_id}.pdf",
        "page_start": 1,
        "page_end": facts.page_count if facts else 1,
        "page_count": facts.page_count if facts else 1,
        "page_meta": page_meta,
        "status": "reading",
        "sha256": sha256 or (facts.sha256 if facts else None),
        "facts_status": facts_status,
        "facts_error": None,
        "category_label": category_label,
        "facts_doc": facts_doc,
    }
    return db.add("plans_reader_documents", **row)


def synthetic_words(pdf: bytes) -> dict[int, PageWords]:
    return extract.extract_words(pdf)


def m_set_pdf() -> bytes:
    """A two-sheet mechanical set (M1.01, M1.02) with plan tags and notes."""
    return make_pdf([plan_sheet("M1.01", "MECHANICAL PLAN"), plan_sheet("M1.02", "MECHANICAL PIPING PLAN")])


def e_set_pdf() -> bytes:
    """A two-sheet electrical set (E1.01, E1.02)."""
    return make_pdf([plan_sheet("E1.01", "POWER PLAN"), plan_sheet("E1.02", "LIGHTING PLAN")])


def vector_text_pdf() -> bytes:
    """One page with almost no text and many filled paths: text class
    vector_text (text counts unavailable)."""
    return make_pdf([sheet((1.0, 1.0, 7, "X"), paths=300)])


def spec_facts(sha: str = "a" * 64, *, pages: int = 6) -> DocFacts:
    """A text document with a section map and a selection: section 26 05
    19 on pages 3 to 5 (tier A) and a tier B snippet from 01 50 00."""
    from app.services.pagefacts.contracts import PageFacts

    sections = [
        {"section": "01 50 00", "division": "01", "title": "TEMPORARY FACILITIES", "page_start": 1, "page_end": 2, "source": "footer", "tier": "B"},
        {"section": "26 05 19", "division": "26", "title": "LOW-VOLTAGE CONDUCTORS", "page_start": 3, "page_end": 5, "source": "footer", "tier": "A"},
        {"section": "09 91 23", "division": "09", "title": "INTERIOR PAINTING", "page_start": 6, "page_end": 6, "source": "footer", "tier": "C"},
    ]
    snippet = {"section": "01 50 00", "page": 2, "text": "Temporary power for construction is by the electrical contractor.", "keywords": ["ELECTRICAL"]}
    spec = {
        "sections": sections,
        "unmapped": [],
        "toc": [],
        "toc_pages": [],
        "divisions": [{"division": "28", "division_to": None, "title": "ELECTRONIC SAFETY AND SECURITY", "status": "not_used", "note": None, "text": "DIVISION 28 NOT USED", "page": 1}],
        "missing_in_body": [],
        "mapped_page_share": 1.0,
        "has_div": {"26": True, "27": False, "28": False},
        "scanned_pages": [],
        "page_count": pages,
        "selection": {
            "pages": [3, 4, 5],
            "tier_b_snippets": [snippet],
            "est_tokens_before": pages * 1100,
            "est_tokens_after": 3 * 1100 + 30,
            "policy": "spec-select/1",
            "flags": [],
            "pages_share": round(3 / pages, 4),
        },
    }
    page_facts = [
        PageFacts(page=p, w_in=8.5, h_in=11.0, rotation=0, text_class="cad_text", roles=["spec_text"], page_role="spec_text")
        for p in range(1, pages + 1)
    ]
    return DocFacts(sha256=sha, kind="text", page_count=pages, name="SPECS.pdf", label="specifications", extractor_version=EXTRACTOR_VERSION, spec=spec, pages=page_facts)


__all__ = [
    "FIXTURE",
    "FakeDB",
    "fixture_facts",
    "install_storage",
    "seed_cache",
    "seed_document",
    "synthetic_words",
    "m_set_pdf",
    "e_set_pdf",
    "vector_text_pdf",
    "spec_facts",
]

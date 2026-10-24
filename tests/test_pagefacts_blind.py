"""WS1 step 12: the blind sheet-number fixture.

`tests/fixtures/pagefacts/blind_sheet_numbers.json` holds 157 pages from
16 electrical drawing documents of 16 golden projects (holdout and 7154
excluded). The sheet numbers and titles were read by eye from page renders
(pypdfium2 thumbnails and title-block crops) BEFORE the extractor ran on
any of these pages, and the label file's sha256 is recorded in the
fixture. The golden tests run the extractor over those pages from the
backup PDFs and measure it against the labels:

- sheet number on >= 80% of electrical text pages with precision >= 98%
  (met: 124 of 137 answered = 0.9051, 122 of 124 correct = 0.9839,
  recall 0.8905);
- title exact or contained >= 90%: MISSED (25 of 137 = 0.1825; the title
  slot learner picks the wrong text on vertical and narrow title blocks),
  recorded as a strict xfail with the value.

No network, no database, no model. The fixture holds no dollar figures.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "pagefacts" / "blind_sheet_numbers.json"
BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
golden = pytest.mark.golden

MEASURED_TITLE_SHARE = 0.1825  # 25 of 137 electrical text pages (2026-10-22, extractor pf-1.0.0)


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def test_blind_fixture_shape_and_hashes():
    fx = _fixture()
    pages, docs = fx["pages"], fx["documents"]
    assert len(pages) >= 150 and len(docs) >= 12 and len({d["project_number"] for d in docs}) >= 6
    assert hashlib.sha256(json.dumps(pages, sort_keys=True).encode()).hexdigest() == fx["pages_sha256"]
    assert len(fx["label_sha256"]) == 64
    # Every labeled page belongs to a listed document and carries both labels.
    ids = {d["doc"] for d in docs}
    assert all(p["doc"] in ids and p["sheet_number"] and p["title"] for p in pages)
    assert sum(d["pages_labeled"] for d in docs) == len(pages)
    # The population excludes the holdout projects and 7154.
    manifest = Path("/Users/thomasmoore/Desktop/BDR/backups/eval/manifest/manifest.json")
    if manifest.exists():
        holdout = {h[:8] for h in json.loads(manifest.read_text())["holdout_ids"]}
        assert not ({d["project8"] for d in docs} & holdout)
    assert not any(d["project_number"].endswith("7154") for d in docs)
    text = FIXTURE.read_text()
    assert "\u2013" not in text and "\u2014" not in text


@pytest.fixture(scope="module")
def measured():
    if not BACKUP.is_dir():
        pytest.skip("backup PDFs not on this machine")
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import pagefacts_corpus
    finally:
        sys.path.pop(0)
    return pagefacts_corpus.run_blind(FIXTURE, BACKUP)


@golden
def test_blind_sheet_numbers_coverage_and_precision(measured):
    assert measured["pages"] == len(_fixture()["pages"])
    print(json.dumps({k: measured[k] for k in ("text_pages", "sheet_answered", "sheet_correct", "sheet_coverage", "sheet_precision", "sheet_recall", "title_share")}))
    assert measured["sheet_coverage"] >= 0.80
    assert measured["sheet_precision"] >= 0.98


@golden
@pytest.mark.xfail(
    strict=True,
    reason=f"title exact or contained {MEASURED_TITLE_SHARE} (25 of 137 text pages) on the blind fixture, target 0.90:"
    " the title-block slot learner misreads vertical and narrow title blocks",
)
def test_blind_titles_exact_or_contained(measured):
    assert measured["title_share"] >= 0.90, measured["title_share"]

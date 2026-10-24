"""pagefacts.job on a fake client: cache miss and hit by (sha256, kind,
extractor_version), relabel on and off, the same bytes labeled
specifications in run A and electrical_drawings in run B as two cache
entries, the job end advancing every waiting run, the lease renewed and
honoured, failures that never block a run, the queue marks.

Synthetic PDFs (tests/pagefacts_fixtures); no network, no database, no model.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.pagefacts import api, extract, job, store
from tests.pagefacts_db import FakeDB, e_set_pdf, m_set_pdf, seed_document

RUN_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RUN_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
RUN_C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
DOC_M = "11111111-0000-4000-8000-00000000000a"
DOC_M2 = "11111111-0000-4000-8000-00000000000b"
DOC_E = "11111111-0000-4000-8000-00000000000c"
DOC_S = "11111111-0000-4000-8000-00000000000d"


def settings(**over) -> SimpleNamespace:
    base = {
        "plans_reader_page_facts_enabled": True,
        "plans_reader_facts_relabel": True,
        "plans_reader_facts_page_budget_s": 4.0,
        "plans_reader_facts_doc_budget_s": 60.0,
        "plans_reader_facts_block_max_tokens": 2500,
    }
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def env(monkeypatch):
    db = FakeDB()
    api.clear_caches()
    for run_id, status in ((RUN_A, "splitting"), (RUN_B, "splitting"), (RUN_C, "reading")):
        db.add("plans_reader_runs", id=run_id, name=run_id[:4], status=status)
    uploads: dict[str, bytes] = {}
    calls = {"extract": 0, "renew": 0, "advanced": []}

    def extract_counting(data, **kw):
        calls["extract"] += 1
        return extract.extract_document(data, page_budget_s=4.0, doc_budget_s=60.0, **kw)

    def renew() -> bool:
        calls["renew"] += 1
        return True

    def advance(sb, run_id):
        calls["advanced"].append(run_id)

    ns = SimpleNamespace(
        db=db, uploads=uploads, calls=calls,
        kw=dict(extract=extract_counting, renew=renew, advance=advance, upload=lambda p, d: uploads.__setitem__(p, d)),
    )
    yield ns
    api.clear_caches()


def _run(env, doc_id, pdf, s=None, **over):
    kw = dict(env.kw)
    kw.update(over)
    return job.run_document(env.db, doc_id, settings=s or settings(), pdf_bytes=pdf, **kw)


def test_cache_miss_writes_rows_blobs_and_the_document_state(env):
    seed_document(env.db, RUN_A, None, doc_id=DOC_M, category="electrical_drawings", name="MECH.pdf", facts_status="pending", sha256=None)
    out = _run(env, DOC_M, m_set_pdf())
    assert out["status"] == "done" and out["cache"] == "miss" and env.calls["extract"] == 1
    sha = extract.sha256_of(m_set_pdf())
    (doc_row,) = env.db.rows("plans_reader_doc_facts", sha256=sha)
    assert doc_row["kind"] == "drawing" and doc_row["blob_prefix"] == store.blob_prefix(sha)
    assert len(env.db.rows("plans_reader_page_facts", sha256=sha)) == 2
    assert sorted(env.uploads) == [store.blob_path(sha, 1), store.blob_path(sha, 2)]
    assert store.decode_blob(env.uploads[store.blob_path(sha, 1)]).words
    doc = env.db.rows("plans_reader_documents", id=DOC_M)[0]
    assert doc["sha256"] == sha and doc["facts_status"] == "done" and doc["facts_error"] is None
    assert doc["category_label"] == "electrical_drawings"
    assert doc["facts_doc"]["cache"] == "miss" and doc["facts_doc"]["pages_with_sheet_number"] == 2
    assert env.calls["renew"] >= 3


def test_relabel_on_moves_a_mechanical_set_and_marks_reduced_reading(env):
    seed_document(env.db, RUN_A, None, doc_id=DOC_M, category="electrical_drawings", name="MECH.pdf", facts_status="pending", sha256=None)
    out = _run(env, DOC_M, m_set_pdf())
    doc = env.db.rows("plans_reader_documents", id=DOC_M)[0]
    assert out["relabel"]["applied"] is True and out["relabel"]["to"] == "mechanical_drawings"
    assert doc["category"] == "mechanical_drawings" and doc["category_label"] == "electrical_drawings"
    assert doc["facts_doc"]["reduced_reading"] is True and doc["facts_doc"]["category_check"]["action"] == "relabel"
    assert doc["facts_doc"]["relabel"]["share"] == 1.0


def test_relabel_off_records_the_proposal_without_applying_it(env):
    seed_document(env.db, RUN_A, None, doc_id=DOC_M, category="electrical_drawings", name="MECH.pdf", facts_status="pending", sha256=None)
    out = _run(env, DOC_M, m_set_pdf(), settings(plans_reader_facts_relabel=False))
    doc = env.db.rows("plans_reader_documents", id=DOC_M)[0]
    assert out["relabel"]["applied"] is False and "off" in out["relabel"]["reason"]
    assert doc["category"] == "electrical_drawings" and doc["facts_doc"]["reduced_reading"] is False
    assert doc["facts_doc"]["category_check"]["detected"] == "mechanical_drawings"
    # An electrical set under its own label is never relabeled.
    seed_document(env.db, RUN_A, None, doc_id=DOC_E, category="electrical_drawings", name="ELEC.pdf", facts_status="pending", sha256=None)
    out = _run(env, DOC_E, e_set_pdf())
    assert out["relabel"]["applied"] is False and out["relabel"]["reason"] == "category check action none"


def test_cache_hit_skips_extraction_and_recomputes_the_check_for_the_new_label(env):
    seed_document(env.db, RUN_A, None, doc_id=DOC_M, category="electrical_drawings", name="MECH.pdf", facts_status="pending", sha256=None)
    _run(env, DOC_M, m_set_pdf())
    seed_document(env.db, RUN_B, None, doc_id=DOC_M2, category="mechanical_drawings", name="MECH again.pdf", facts_status="pending", sha256=None)
    out = _run(env, DOC_M2, m_set_pdf())
    assert out["cache"] == "hit" and env.calls["extract"] == 1
    doc = env.db.rows("plans_reader_documents", id=DOC_M2)[0]
    assert doc["facts_doc"]["cache"] == "hit" and doc["category"] == "mechanical_drawings"
    assert doc["facts_doc"]["category_check"]["action"] == "none" and out["relabel"]["applied"] is False
    assert len(env.db.rows("plans_reader_doc_facts")) == 1


def test_same_bytes_as_specifications_and_as_drawings_are_two_cache_entries(env):
    pdf = e_set_pdf()
    seed_document(env.db, RUN_A, None, doc_id=DOC_S, category="specifications", name="as specs.pdf", facts_status="pending", sha256=None)
    seed_document(env.db, RUN_B, None, doc_id=DOC_E, category="electrical_drawings", name="as drawings.pdf", facts_status="pending", sha256=None)
    a = _run(env, DOC_S, pdf)
    b = _run(env, DOC_E, pdf)
    assert a["cache"] == "miss" and b["cache"] == "miss" and env.calls["extract"] == 2
    sha = extract.sha256_of(pdf)
    kinds = sorted(r["kind"] for r in env.db.rows("plans_reader_doc_facts", sha256=sha))
    assert kinds == ["drawing", "text"]
    spec_doc = env.db.rows("plans_reader_documents", id=DOC_S)[0]
    assert spec_doc["facts_doc"]["kind"] == "text" and spec_doc["facts_doc"]["category_check"] is None
    assert api.document_kind(spec_doc) == "text" and api.document_kind(env.db.rows("plans_reader_documents", id=DOC_E)[0]) == "drawing"


def test_job_end_advances_every_run_in_splitting_that_holds_the_bytes(env):
    pdf = m_set_pdf()
    sha = extract.sha256_of(pdf)
    seed_document(env.db, RUN_A, None, doc_id=DOC_M, category="electrical_drawings", facts_status="pending", sha256=None)
    seed_document(env.db, RUN_B, None, doc_id=DOC_M2, category="electrical_drawings", facts_status="done", sha256=sha)
    seed_document(env.db, RUN_C, None, doc_id=DOC_E, category="electrical_drawings", facts_status="done", sha256=sha)
    out = _run(env, DOC_M, pdf)
    assert sorted(out["advanced"]) == sorted([RUN_A, RUN_B])  # RUN_C is reading, not waiting
    assert sorted(env.calls["advanced"]) == sorted([RUN_A, RUN_B])


def test_lost_lease_aborts_without_writing_done(env):
    seed_document(env.db, RUN_A, None, doc_id=DOC_M, category="electrical_drawings", facts_status="pending", sha256=None)
    renewals = iter([True, False])
    with pytest.raises(job.FactsLeaseLost):
        _run(env, DOC_M, m_set_pdf(), renew=lambda: next(renewals))
    doc = env.db.rows("plans_reader_documents", id=DOC_M)[0]
    assert doc["facts_status"] == "running" and doc.get("facts_doc") is None and env.calls["advanced"] == []


def test_a_failure_marks_failed_and_still_advances(env):
    seed_document(env.db, RUN_A, None, doc_id=DOC_M, category="electrical_drawings", facts_status="pending", sha256=None)

    def boom(data, **kw):
        raise ValueError("broken PDF")

    out = _run(env, DOC_M, m_set_pdf(), extract=boom)
    doc = env.db.rows("plans_reader_documents", id=DOC_M)[0]
    assert out["status"] == "failed" and doc["facts_status"] == "failed" and "broken PDF" in doc["facts_error"]
    assert doc["sha256"] == extract.sha256_of(m_set_pdf()) and env.calls["advanced"] == [RUN_A]
    assert job.error_message(ValueError("x"), "pagefacts") == job.MSG_FAILED


def test_flag_off_marks_skipped_and_advances(env):
    seed_document(env.db, RUN_A, None, doc_id=DOC_M, category="electrical_drawings", facts_status="pending", sha256=None)
    out = _run(env, DOC_M, m_set_pdf(), settings(plans_reader_page_facts_enabled=False))
    doc = env.db.rows("plans_reader_documents", id=DOC_M)[0]
    assert out["status"] == "skipped" and doc["facts_status"] == "skipped" and env.calls["extract"] == 0
    assert env.calls["advanced"] == [RUN_A]


def test_queue_marks_and_status(env, monkeypatch):
    monkeypatch.setattr("app.core.supabase_client.get_supabase", lambda: env.db)
    advanced = []
    monkeypatch.setattr("app.services.plans_reader_runs.advance", lambda sb, run_id: advanced.append(run_id))
    seed_document(env.db, RUN_A, None, doc_id=DOC_M, category="electrical_drawings", facts_status="running", sha256=None)
    job.mark_from_queue(DOC_M, {"status": "pending", "error": "retry"})
    assert env.db.rows("plans_reader_documents", id=DOC_M)[0]["facts_status"] == "pending"
    assert job.current_status(DOC_M) == "pending"
    job.mark_from_queue(DOC_M, {"status": "failed", "error": "gave up"})
    doc = env.db.rows("plans_reader_documents", id=DOC_M)[0]
    assert doc["facts_status"] == "failed" and doc["facts_error"] == "gave up" and advanced == [RUN_A]
    assert job.current_status(DOC_M) == "done" and job.current_status("nope") is None
    with pytest.raises(ValueError):
        job.execute({})


def test_facts_doc_records_the_splitter_title_fallbacks():
    from app.services.pagefacts.contracts import DocFacts, PageFacts

    pages = [PageFacts(page=i, w_in=42.0, h_in=30.0, rotation=0, text_class="full") for i in (1, 2, 3, 4)]
    pages[0].sheet_title, pages[0].title_source = "POWER PLAN", "title_block"
    pages[1].sheet_title, pages[1].title_source = "E-101 POWER", "splitter"
    pages[3].sheet_title, pages[3].title_source = "E-103 LIGHTING", "splitter"
    facts = DocFacts(sha256="ab" * 32, kind="drawing", page_count=4, pages=pages)
    doc = job._facts_doc(facts, cache="hit", check=None, relabel={"applied": False})
    assert doc["splitter_title_count"] == 2 and doc["splitter_title_pages"] == [2, 4]
    none = job._facts_doc(DocFacts(sha256="cd" * 32, kind="drawing", page_count=1, pages=pages[:1]), cache="miss", check=None, relabel={})
    assert none["splitter_title_count"] == 0 and none["splitter_title_pages"] == []

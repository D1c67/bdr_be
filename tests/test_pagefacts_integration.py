"""WS1 integration (spec 6 WS1 step 10):

T-INT-1: with every page-facts flag off, the planned requests of a run are
byte for byte what the pre-facts code built: the same run planned twice,
once through the real hooks and once with every hook helper stubbed to its
no-op, gives the same ledger text, the same group rows and the same request
parts and system prompt; the gate enqueues no facts job and writes no
facts column.

T-INT-2 (golden, needs the 7154 backup and the stored bundle): with
`plans_reader_page_facts_enabled` on, the planned 7154 requests built at
$0 from the backup PDFs through the `plan` replay stage show the ledger
`legend_pages` containing E0.00 with >= 60 legend entries, the legend text
in every drawing-category sub request, a facts block after every page each
sub request sends, MECH relabeled and read reduced, spec pages sent <= 68
of 170 and planned input <= the stored run's 940k tokens; the E16, E18 and
E24 detectors read the same artifacts.

Also: the splitting gate enqueues one facts job per document and waits;
the sub builder prints the facts block, the legend text and the first-group
snippets when the flags are on (synthetic documents, fake model).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import llm_queue
from app.services import plans_reader_agents as agents
from app.services import plans_reader_runs as runs
from app.services.pagefacts import api, integration
from tests.pagefacts_db import fixture_facts, seed_cache, spec_facts
from tests.test_plans_reader_runs import install_env, make_pdf, set_status, user

EVAL_ROOT = Path("/Users/thomasmoore/Desktop/BDR/backups/eval")
BUNDLE = EVAL_ROOT / "bundles" / "7154" / "ff8fad25"
BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
PROJECT_DIR = BACKUP / "storage/project-files/7dfc64e6-36ef-4c67-9d02-0d7942015011"
ELEC_PDF = PROJECT_DIR / "drawing/c85a4720840d49a288d9d0c7d3c87fed-Reference Material - Architectural - ELEC_RESPONSE_TO_CLV_COMMENTS D1.pdf"
MECH_PDF = PROJECT_DIR / "drawing/9466b0a7b03b42c4bd9bf8feac10c9e0-Reference Material - Architectural - MECH_RESPONSE_TO_CLV_COMMENTS D1.pdf"
golden = pytest.mark.golden
needs_7154 = pytest.mark.skipif(
    not (BUNDLE.is_dir() and ELEC_PDF.exists() and MECH_PDF.exists()), reason="7154 bundle or backup PDFs not on this machine"
)
FLAGS_ON = {
    "plans_reader_page_facts_enabled": True,
    "plans_reader_page_facts_in_subs": True,
    "plans_reader_spec_select": True,
    "plans_reader_facts_relabel": True,
}

HOOKS = (
    "ensure_facts", "load_plan_facts", "page_meta_from_facts", "readable_pages_from_facts", "spec_selection",
    "page_tokens", "add_ledger_facts", "plan_image_refs", "facts_provider", "page_block", "file_span_block",
    "first_group_snippets", "snippet_tokens_for_pack", "sub_system_suffix",
)


@pytest.fixture
def env(monkeypatch):
    ns = install_env(monkeypatch)
    api.clear_caches()
    yield ns
    api.clear_caches()


def _meta(roles_titles):
    return [{"page": i, "pdf_page": i, "prepended": False, "title": t, "page_role": r, "confidence": 0.9} for i, (r, t) in enumerate(roles_titles, start=1)]


def _doc(env, run, *, category, pages, page_meta=None, name=None, w=144, h=144, pdf=None):
    import uuid

    path = f"plans-reader/{run['id']}/source/{uuid.uuid4().hex}.pdf"
    env.store[path] = pdf or make_pdf(pages, w, h)
    f = env.db.add("plans_reader_files", run_id=run["id"], filename=f"{category}.pdf", storage_path=path, label=category, status="labeled")
    return env.db.add(
        "plans_reader_documents", run_id=run["id"], file_id=f["id"], category=category, name=name or category,
        storage_path=path, page_start=1, page_end=pages, page_count=pages, page_meta=page_meta, status="pending",
    )


def _new_run(env, **fields):
    run = runs.create_run(env.db, name="Edikted TI", source="upload", project_id=None, render_mode="adaptive", user=user())
    return set_status(env, run, "splitting", **fields)


def _seed_pair(env):
    """A run with a two-sheet electrical set (legend + plan) and a short spec book."""
    run = _new_run(env)
    draw = _doc(env, run, category="electrical_drawings", pages=2, name="ELEC",
                page_meta=_meta([("legend_symbols", "E-001 LEGEND"), ("plan", "E-101 POWER")]))
    spec = _doc(env, run, category="specifications", pages=3, name="SPECS",
                page_meta=[{"page": i, "pdf_page": i, "prepended": False, "title": None, "page_role": "spec_text", "confidence": 1.0} for i in (1, 2, 3)])
    return run, draw, spec


def _plan_and_build(env, run):
    runs.advance(env.db, run["id"])
    stored = env.db.rows("plans_reader_runs", id=run["id"])[0]
    assert stored["status"] == "reading", stored.get("error")
    groups = sorted(env.db.rows("plans_reader_groups", run_id=run["id"]), key=lambda g: (g["category"], g["group_index"]))
    requests = []
    for g in groups:
        builder, _ = agents._build_sub(env.db, stored, g, env.settings)
        requests.append({
            "category": g["category"],
            "group_index": g["group_index"],
            "pages": [(i["page"], i["kind"], i["ppi"]) for i in g["pages"]],
            "est_input_tokens": g["est_input_tokens"],
            "types": [p["type"] for p in builder.parts],
            "texts": [p["text"] for p in builder.parts if p["type"] == "input_text"],
            "roles": [m.get("role") for m in builder.manifest],
            "system": agents._system_for(g, env.settings),
        })
    return stored, requests


# T-INT-1 frozen baseline: the request fingerprint of the synthetic
# `_seed_pair` run as built by the PRE-FACTS code (git HEAD caaf0cf, the
# tree before this build, run from a `git archive HEAD` copy on
# 2026-10-22 with the same documents and the same fingerprint function;
# deterministic across processes). With every page-facts flag off the
# current code must reproduce it exactly, so a flags-off change anywhere in
# the planner, the ledger or the sub builder fails here even when it runs
# the same way in both arms of the stubbed comparison below.
PRE_FACTS_FINGERPRINT = {
    "ledger_text": "6270ede164637e44",
    "requests": [
        {
            "category": "electrical_drawings", "group_index": 0, "est_input_tokens": 5824,
            "pages": [[1, "image", 260], [2, "image", 260]],
            "types": "5794032a9b454924", "texts": "823657812839c0e6", "roles": "e5b15b1c04b612dc", "system": "7d30aec8602154ed",
        },
        {
            "category": "specifications", "group_index": 0, "est_input_tokens": 8430,
            "pages": [[1, "file", None], [2, "file", None], [3, "file", None]],
            "types": "c925fcc21e497d30", "texts": "27bb431d4d50f5ef", "roles": "a28c1f3d81f2123b", "system": "3107e2a81403b977",
        },
    ],
}


def _h(value):
    import hashlib

    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _fingerprint(stored, requests):
    return {
        "ledger_text": _h(agents.ledger_text(stored["ledger"])),
        "requests": [
            {
                "category": r["category"],
                "group_index": r["group_index"],
                "est_input_tokens": r["est_input_tokens"],
                "pages": [list(p) for p in r["pages"]],
                "types": _h(r["types"]),
                "texts": _h(r["texts"]),
                "roles": _h(r["roles"]),
                "system": _h(r["system"]),
            }
            for r in requests
        ],
    }


def test_t_int_1_flags_off_requests_match_the_frozen_pre_facts_fingerprint(env):
    assert env.settings.plans_reader_page_facts_enabled is False
    run, _d, _s = _seed_pair(env)
    stored, requests = _plan_and_build(env, run)
    assert _fingerprint(stored, requests) == PRE_FACTS_FINGERPRINT


def test_t_int_1_flags_off_requests_are_byte_identical(env, monkeypatch):
    assert env.settings.plans_reader_page_facts_enabled is False
    run_a, _d, _s = _seed_pair(env)
    stored_a, real = _plan_and_build(env, run_a)
    # The same documents again, with every hook helper stubbed to its no-op.
    stubs = {
        "ensure_facts": lambda sb, run, documents, s: True,
        "load_plan_facts": lambda sb, documents, s, facts=None: None,
        "page_meta_from_facts": lambda doc, facts: [],
        "readable_pages_from_facts": lambda doc, facts, pages: list(pages),
        "spec_selection": lambda doc, facts, s: (None, [], None),
        "page_tokens": lambda facts, page, caps: 0,
        "add_ledger_facts": lambda ledger, plan: ledger,
        "plan_image_refs": lambda ledger, refs: refs,
        "facts_provider": lambda sb, ids, s, facts=None: None,
        "page_block": lambda provider, document_id, page, max_tokens: None,
        "file_span_block": lambda provider, document_id, start, end, max_tokens: None,
        "first_group_snippets": lambda ledger, document_id, group, category_groups: None,
        "snippet_tokens_for_pack": lambda plan, ledger, document_id: (None, 0),
        "sub_system_suffix": lambda s: "",
    }
    assert set(stubs) == set(HOOKS)
    for name, fn in stubs.items():
        monkeypatch.setattr(integration, name, fn)
    run_b, _d, _s = _seed_pair(env)
    stored_b, legacy = _plan_and_build(env, run_b)
    assert agents.ledger_text(stored_a["ledger"]) == agents.ledger_text(stored_b["ledger"])
    assert {k: v for k, v in stored_a["ledger"].items() if k not in ("documents", "sheet_index", "legend_pages", "general_notes_pages", "images")} == \
        {k: v for k, v in stored_b["ledger"].items() if k not in ("documents", "sheet_index", "legend_pages", "general_notes_pages", "images")}
    assert "facts" not in stored_a["ledger"] and "legend" not in stored_a["ledger"]
    assert real == legacy
    assert not any(r in ("facts_block", "spec_snippets") for req in real for r in req["roles"])
    assert all(integration.SUB_FACTS_SYSTEM not in req["system"] for req in real)
    assert not any(j["job_type"] == llm_queue.JOB_PLANS_FACTS for j in env.enqueued)
    for d in env.db.rows("plans_reader_documents", run_id=run_a["id"]):
        assert d.get("facts_status") is None and d.get("sha256") is None
        assert all("roles" not in m for m in d["page_meta"])


def test_gate_enqueues_one_facts_job_per_document_and_waits(env, monkeypatch):
    env.settings = env.settings.model_copy(update={"plans_reader_page_facts_enabled": True})
    monkeypatch.setattr(
        llm_queue, "active_job",
        lambda job_type, target_id: next((j for j in env.enqueued if j["job_type"] == job_type and j["target_id"] == target_id), None),
    )
    run, draw, spec = _seed_pair(env)
    runs.advance(env.db, run["id"])
    jobs = [j for j in env.enqueued if j["job_type"] == llm_queue.JOB_PLANS_FACTS]
    assert sorted(j["target_id"] for j in jobs) == sorted([draw["id"], spec["id"]])
    assert jobs[0]["payload"]["run_id"] == run["id"] and jobs[0]["payload"]["kind"] in ("drawing", "text")
    assert env.db.rows("plans_reader_runs", id=run["id"])[0]["status"] == "splitting"
    assert {d["facts_status"] for d in env.db.rows("plans_reader_documents", run_id=run["id"])} == {"pending"}
    # A second advance while pending re-enqueues nothing new (the jobs collapse on the unique index anyway).
    runs.advance(env.db, run["id"])
    assert len([j for j in env.enqueued if j["job_type"] == llm_queue.JOB_PLANS_FACTS]) == 2
    # Settled documents release the gate: failed and skipped never block.
    env.db.table("plans_reader_documents").update({"facts_status": "failed", "facts_error": "x"}).eq("id", draw["id"]).execute()
    env.db.table("plans_reader_documents").update({"facts_status": "skipped"}).eq("id", spec["id"]).execute()
    runs.advance(env.db, run["id"])
    assert env.db.rows("plans_reader_runs", id=run["id"])[0]["status"] == "reading"


def test_flags_on_sub_requests_carry_facts_blocks_legend_text_and_snippets(env, monkeypatch):
    env.settings = env.settings.model_copy(update=FLAGS_ON)
    run = _new_run(env)
    elec = fixture_facts("elec")
    spec = spec_facts()
    draw = _doc(env, run, category="electrical_drawings", pages=10, name="ELEC", pdf=make_pdf(10, 3024, 2160))
    book = _doc(env, run, category="specifications", pages=6, name="SPECS", pdf=make_pdf(6, 612, 792),
                page_meta=[{"page": i, "pdf_page": i, "prepended": False, "title": None, "page_role": "spec_text", "confidence": 1.0} for i in range(1, 7)])
    seed_cache(env.db, env.store, elec, {})
    seed_cache(env.db, env.store, spec, {})
    for doc, facts, label in ((draw, elec, "electrical_drawings"), (book, spec, "specifications")):
        env.db.table("plans_reader_documents").update({"sha256": facts.sha256, "facts_status": "done", "category_label": label}).eq("id", doc["id"]).execute()
    stored, requests = _plan_and_build(env, run)
    ledger = stored["ledger"]
    assert [p["sheet_number"] for p in ledger["legend_pages"]] == ["E0.00"] and len(ledger["legend"]) >= 60
    assert ledger["facts"]["legend_text"] in agents.ledger_text(ledger, audience="drawing")
    assert ledger["facts"]["legend_text"] not in agents.ledger_text(ledger, audience="file")
    meta = env.db.rows("plans_reader_documents", id=draw["id"])[0]["page_meta"]
    assert meta[0]["sheet_number"] == "E0.00" and "legend_symbols" in meta[0]["roles"] and meta[0]["title"].startswith("E0.00 ")
    assert all(m["sheet_number"] and m["title"] and m["page_role"] != "other" for m in meta)
    drawing = [r for r in requests if r["category"] == "electrical_drawings"]
    assert drawing and all(ledger["facts"]["legend_text"] in r["texts"][0] for r in drawing)
    for r in drawing:
        pages = sum(1 for p in r["pages"] if p[1] == "image")
        assert r["roles"].count("facts_block") == pages
        assert integration.SUB_FACTS_SYSTEM in r["system"]
    spec_reqs = [r for r in requests if r["category"] == "specifications"]
    (sr,) = spec_reqs
    assert [p[0] for p in sr["pages"]] == [3, 4, 5]  # tier A pages only
    assert "spec_snippets" in sr["roles"] and sr["roles"].count("facts_block") == 1
    assert any("Temporary power for construction" in t for t in sr["texts"])
    assert ledger["facts"]["legend_text"] not in sr["texts"][0]
    # The planned estimate counts the facts blocks and the snippets.
    assert all(r["est_input_tokens"] > 0 for r in requests)


def _bundle_replay(flags: dict):
    from app.services import eval_bundle
    from app.services import plans_reader_replay as rp
    from app.services.pagefacts import replay as pf_replay

    bundle = eval_bundle.load_bundle(BUNDLE)
    # Other workstreams register their stages at import; keep them intact.
    saved = dict(rp._STAGES)
    pf_replay.register()
    try:
        result = rp.replay(bundle, from_stage="plan", to_stage="plan", out_dir=None, settings_overrides=flags)
    finally:
        rp._STAGES.clear()
        rp._STAGES.update(saved)
    stage = result.stage("plan")
    assert stage.error is None, stage.error
    return bundle, result


@golden
@needs_7154
def test_t_int_2_planned_7154_requests_with_facts_on(tmp_path):
    from app.services import plans_reader_acceptance as acc

    bundle, result = _bundle_replay(FLAGS_ON)
    art = result.artifacts
    summary = art["plan_summary"]
    requests = art["planned_requests"]
    ledger = art["ledger"]
    # E16: the legend sheet and its entries reach the ledger and every drawing sub request.
    assert any("E0.00" in str(p.get("sheet_number") or p.get("title")) for p in ledger["legend_pages"])
    assert len(ledger["legend"]) >= 60
    drawing = [r for r in requests if r["kind"] == "sub" and str(r["category"]).endswith("_drawings")]
    assert drawing and all(r["has_legend_text"] for r in drawing)
    # E18: a facts block after every page, every document done, planned input under the stored run.
    assert all(r["pages_without_facts_block"] == 0 for r in requests if r["kind"] == "sub")
    assert all(d["facts_status"] == "done" for d in art["documents"])
    stored_input = sum(int(c.get("input_tokens") or 0) for c in bundle.calls if c.get("ok"))
    planned_input = sum(int(r["est_input_tokens"]) for r in requests)
    assert planned_input <= min(stored_input, 940_000), (planned_input, stored_input)
    # E24: MECH relabeled and read reduced; spec pages sent at most 68 of 170.
    mech = next(d for d in art["documents"] if "MECH" in str(d.get("name")))
    assert mech["category"] == "mechanical_drawings" and mech["category_label"] == "electrical_drawings"
    assert mech["facts_doc"]["reduced_reading"] is True
    mech_pages = sum(len([i for i in g["pages"] if i["document_id"] == mech["id"]]) for g in art["planned_groups"])
    assert 0 < mech_pages < 8
    assert summary["spec_pages"] == 170 and summary["spec_pages_sent"] <= 68
    # The detectors read the same artifacts.
    inp = acc.AcceptanceInputs(artifacts=art, case_id="7154")
    e16 = acc.check_e16(inp)
    assert e16.measured["parts"]["ledger_legend"] == "pass" and e16.measured["parts"]["legend_in_drawing_requests"] == "pass"
    e18 = acc.check_e18(inp)
    assert e18.measured["parts"]["drawing_pages_labeled"] == "pass"
    assert e18.measured["parts"]["facts_status_done"] == "pass"
    assert e18.measured["parts"]["facts_block_every_page"] == "pass"
    assert e18.measured["parts"]["planned_input_tokens"] == "pass"
    e24 = acc.check_e24(inp)
    assert e24.status == "pass", e24.measured
    (tmp_path / "plan_summary.json").write_text(json.dumps(summary, default=str))
    print(json.dumps({k: summary[k] for k in ("planned_groups", "planned_by_category", "planned_input_tokens", "spec_pages_sent", "legend_entries", "legend_pages")}))


@golden
@needs_7154
def test_t_int_1_golden_flags_off_plan_matches_the_stored_shape():
    bundle, result = _bundle_replay({"plans_reader_page_facts_enabled": False, "plans_reader_facts_relabel": False})
    art = result.artifacts
    requests = art["planned_requests"]
    assert not any("facts_block" in r["roles"] or "spec_snippets" in r["roles"] for r in requests)
    assert not any(r["has_legend_text"] for r in requests)
    assert "facts" not in art["ledger"] and art["ledger"]["legend_pages"] == []
    assert art["plan_summary"]["spec_pages_sent"] == 170
    assert all(d["category"] == bundle.document(d["id"])["category"] for d in art["documents"])
    by_cat = art["plan_summary"]["planned_by_category"]
    assert by_cat["electrical_drawings"] == 3 and by_cat["specifications"] == 1 and by_cat["rfp"] == 1


def _label(head):
    return {"type": "input_text", "role": "page_label", "head": head}


def _block(head):
    return {"type": "input_text", "role": integration.FACTS_BLOCK_ROLE, "head": head}


def test_facts_block_coverage_counts_a_middle_page_without_facts():
    from app.services.pagefacts.replay import facts_block_coverage

    image = {"type": "input_image"}
    manifest = [
        _label("D1 P1"), image, _block("D1 P1 PAGE FACTS ..."),
        _label("D1 P2"), image,  # no facts block for P2
        _label("D1 P3"), image, _block("D1 P3 PAGE FACTS ..."),
    ]
    got = facts_block_coverage(manifest)
    assert got == {"pages": 3, "uncovered": ["D1 P2"]}
    # A block for another page never covers the current one.
    wrong = [_label("D1 P1"), image, _block("D1 P9 PAGE FACTS ..."), _label("D1 P2"), image, _block("D1 P2 x")]
    assert facts_block_coverage(wrong)["uncovered"] == ["D1 P1"]
    # Tile labels of one page count once; the block after the last tile covers the page.
    tiles = [
        _label("D2 P1 tile 1 of 2 covering 0,0,0.5,1"), image,
        _label("D2 P1 tile 2 of 2 covering 0.5,0,1,1"), image, _block("D2 P1 PAGE FACTS"),
        _label("D2 P2-P4 (PDF file below; file page 1 is D2 P2)"), {"type": "input_file"},
    ]
    assert facts_block_coverage(tiles) == {"pages": 2, "uncovered": ["D2 P2-P4"]}
    tiles.append(_block("D2 P2-P4 PAGE FACTS"))
    assert facts_block_coverage(tiles) == {"pages": 2, "uncovered": []}
    # The last page of a request without a block is counted too.
    assert facts_block_coverage([_label("D1 P1"), image])["uncovered"] == ["D1 P1"]


# extract_and_store: the facts job core outside the queue (the import harness) -------------


def test_extract_and_store_writes_the_store_without_queue_lease_advance_or_relabel(monkeypatch):
    from types import SimpleNamespace

    from app.services.pagefacts import extract, store
    from tests.pagefacts_db import FakeDB, install_storage, m_set_pdf, seed_document

    db = FakeDB()
    blobs = install_storage(monkeypatch)
    api.clear_caches()
    run_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    db.add("plans_reader_runs", id=run_id, name="import", status="splitting")
    doc_id = "22222222-0000-4000-8000-000000000001"
    seed_document(db, run_id, None, doc_id=doc_id, category="electrical_drawings", name="MECH.pdf", facts_status=None, sha256=None)

    def no_queue(*_a, **_k):
        raise AssertionError("extract_and_store never touches the queue")

    monkeypatch.setattr(llm_queue, "renew_lease", no_queue)
    monkeypatch.setattr(llm_queue, "enqueue", no_queue)
    advanced: list = []
    monkeypatch.setattr(runs, "advance", lambda sb, rid: advanced.append(rid))
    # The server flags off (facts and relabel): the harness still extracts, never relabels.
    off = SimpleNamespace(plans_reader_page_facts_enabled=False, plans_reader_facts_relabel=True,
                          plans_reader_facts_page_budget_s=4.0, plans_reader_facts_doc_budget_s=60.0)
    out = integration.extract_and_store(db, doc_id, pdf_bytes=m_set_pdf(), settings=off)
    assert out["status"] == "done" and out["cache"] == "miss" and advanced == []
    sha = extract.sha256_of(m_set_pdf())
    doc = db.rows("plans_reader_documents", id=doc_id)[0]
    assert doc["sha256"] == sha and doc["facts_status"] == "done" and doc["category"] == "electrical_drawings"
    assert doc["facts_doc"]["relabel"]["applied"] is False and doc["facts_doc"]["relabel"]["to"] == "mechanical_drawings"
    assert len(db.rows("plans_reader_page_facts", sha256=sha)) == 2 and store.blob_path(sha, 1) in blobs
    assert api.doc_facts(db, doc_id).page_count == 2  # what the derive measure reads
    # pdf_bytes None reads the stored PDF; relabel=True applies the relabel; the cache now hits.
    doc2 = "22222222-0000-4000-8000-000000000002"
    seed_document(db, run_id, None, doc_id=doc2, category="electrical_drawings", name="MECH again.pdf", facts_status=None,
                  sha256=None, storage_path=f"plans-reader/{run_id}/source/mech.pdf")
    blobs[f"plans-reader/{run_id}/source/mech.pdf"] = m_set_pdf()
    out = integration.extract_and_store(db, doc2, settings=off, relabel=True)
    assert out["cache"] == "hit" and out["relabel"]["applied"] is True
    assert db.rows("plans_reader_documents", id=doc2)[0]["category"] == "mechanical_drawings"
    api.clear_caches()

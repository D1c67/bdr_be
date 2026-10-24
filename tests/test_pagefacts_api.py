"""pagefacts.api over a fake client and an in-memory blob store: the page
word LRU, document and page facts, the run-level lookups with the 5.2 refs
(l:, s:, sp:), tag occurrences and regions, the ledger facts caps, and
FactsUnavailable instead of fabricated answers.

Facts come from the committed 7154 ELEC fixture (final schema, no dollars)
plus a synthetic spec document. No network, no database, no model.
"""

from __future__ import annotations

import pytest

from app.services import plans_reader_tools as tools
from app.services.pagefacts import api, block
from app.services.pagefacts import ledger as ledger_mod
from app.services.pagefacts.contracts import FactsUnavailable
from tests.pagefacts_db import (
    FakeDB,
    e_set_pdf,
    fixture_facts,
    install_storage,
    seed_cache,
    seed_document,
    spec_facts,
    synthetic_words,
)

RUN = "11111111-1111-4111-8111-111111111111"
ELEC = "aaaaaaaa-0000-4000-8000-000000000001"
SPEC = "bbbbbbbb-0000-4000-8000-000000000002"
PENDING = "cccccccc-0000-4000-8000-000000000003"


@pytest.fixture
def env(monkeypatch):
    db = FakeDB()
    blobs = install_storage(monkeypatch)
    api.clear_caches()
    elec = fixture_facts("elec")
    words = synthetic_words(e_set_pdf())  # two pages of positioned words under the ELEC sha
    seed_cache(db, blobs, elec, words)
    spec = spec_facts()
    seed_cache(db, blobs, spec)
    seed_document(db, RUN, elec, doc_id=ELEC, category="electrical_drawings", name="ELEC.pdf", category_label="electrical_drawings")
    seed_document(db, RUN, spec, doc_id=SPEC, category="specifications", name="SPECS.pdf", category_label="specifications")
    seed_document(db, RUN, None, doc_id=PENDING, category="electrical_drawings", name="PENDING.pdf", facts_status="pending", sha256=None)
    docs = db.rows("plans_reader_documents", run_id=RUN)
    downloads = {"n": 0}
    real = __import__("app.services.storage", fromlist=["download_file"]).download_file

    def counting(path):
        downloads["n"] += 1
        return real(path)

    monkeypatch.setattr("app.services.storage.download_file", counting)
    yield type("Env", (), {"db": db, "blobs": blobs, "docs": docs, "elec": elec, "spec": spec, "downloads": downloads})()
    api.clear_caches()


def test_page_words_reads_the_blob_once_and_keeps_an_lru(env):
    words, lines = api.page_words(env.db, ELEC, 1)
    assert words and lines and env.downloads["n"] == 1
    assert api.page_words(env.db, ELEC, 1)[0] is words and env.downloads["n"] == 1
    api.page_words(env.db, ELEC, 2)
    assert env.downloads["n"] == 2
    api.clear_caches()
    api.page_words(env.db, ELEC, 1)
    assert env.downloads["n"] == 3
    assert api.WORDS_LRU_PAGES == 64
    with pytest.raises(FactsUnavailable):
        api.page_words(env.db, ELEC, 99)  # no blob for that page
    with pytest.raises(FactsUnavailable):
        api.page_words(env.db, PENDING, 1)  # facts not done
    with pytest.raises(FactsUnavailable):
        api.page_words(env.db, "nope", 1)


def test_doc_and_page_facts_come_from_the_cache_tables(env):
    facts = api.doc_facts(env.db, ELEC)
    assert facts.sha256 == env.elec.sha256 and facts.page_count == 10
    assert [p.sheet_number for p in facts.pages][:3] == ["E0.00", "E0.01", "E0.02"]
    assert api.page_facts(env.db, ELEC, 6).primary_ft_per_in == 16.0
    with pytest.raises(FactsUnavailable):
        api.page_facts(env.db, ELEC, 11)
    with pytest.raises(FactsUnavailable):
        api.doc_facts(env.db, PENDING)
    assert api.doc_facts(env.db, ELEC) is facts  # LRU


def test_sheet_index_and_resolve_sheet(env):
    rows = api.sheet_index(env.db, env.docs)
    assert len(rows) == 10 and rows[0]["doc_ref"] == "D1 P1" and rows[0]["sheet_number"] == "E0.00"
    assert rows[5]["primary_ft_per_in"] == 16.0 and rows[5]["text_layer_quality"] == "full"
    assert "legend_symbols" in rows[0]["roles"]
    hit = api.resolve_sheet(env.db, env.docs, "e0.03")
    assert hit["document_id"] == ELEC and hit["page"] == 4 and hit["doc_ref"] == "D1 P4"
    assert api.resolve_sheet(env.db, env.docs, "ED2-01")["page"] == 7
    with pytest.raises(FactsUnavailable):
        api.resolve_sheet(env.db, env.docs, "E9.99")
    run = api.load_run_facts(env.db, env.docs)
    # Run order: the two drawing sets first (category rank), then the spec book.
    assert run.refs[ELEC] == "D1" and run.refs[PENDING] == "D2" and run.refs[SPEC] == "D3" and PENDING in run.missing


def test_legend_lookup_refs_resolve(env):
    out = api.legend_lookup(env.db, env.docs, "JB")
    assert out["entries"] and all(e["ref"].startswith(f"l:{ELEC[:8]}:lg") for e in out["entries"])
    kind, parts = tools.parse_ref(out["entries"][0]["ref"])
    assert kind == "l" and parts[0] == ELEC[:8]
    everything = api.legend_lookup(env.db, env.docs, None, limit=500)
    assert len(everything["entries"]) >= 60
    tags = {e["tag"] for e in everything["entries"]}
    assert {"JB", "ECONN"} <= tags
    assert any(m["effect"] == "remove" for m in everything["modifiers"])


def test_schedule_lookup_rows_carry_refs(env):
    out = api.schedule_lookup(env.db, env.docs, kind="panel", name="1EELC")
    (sched,) = out["schedules"]
    assert sched["schedule_id"].startswith(f"{ELEC[:8]}:s") and sched["sheet_number"] == "E0.03"
    new = [r for r in sched["rows"] if r["parsed"]["status"] == "new"]
    assert [r["key"] for r in new] == ["1", "3", "5", "7"]
    kind, parts = tools.parse_ref(new[0]["ref"])
    assert kind == "s" and parts == (sched["schedule_id"], "1")
    one = api.schedule_lookup(env.db, env.docs, kind="panel", name="1EELC", row_key="3")
    assert [r["key"] for r in one["schedules"][0]["rows"]] == ["3"]
    lum = api.schedule_lookup(env.db, env.docs, kind="fixture")
    assert {r["key"] for s in lum["schedules"] for r in s["rows"]} >= {"L4", "L5"}
    assert api.schedule_lookup(env.db, env.docs, document_id=SPEC)["schedules"] == []


def test_keynotes_tags_regions_panels_views_and_notes(env):
    facts = env.elec
    with_keynotes = next(p for p in facts.pages if any(b.get("kind") == "keynotes" for b in p.notes))
    out = api.keynote_lookup(env.db, env.docs, with_keynotes.sheet_number)
    assert out["page"] == with_keynotes.page and out["items"] and all("text" in i for i in out["items"])
    occ = api.tag_occurrences(env.db, ELEC, 9)
    assert len(occ) == 74 and all(o["kind"] == "circuit" for o in occ)
    first = next(o for o in occ if o["circuits"])
    narrowed = api.tag_occurrences(env.db, ELEC, 9, panel=first["panel"], circuit=first["circuits"][0])
    assert narrowed and all(first["circuits"][0] in o["circuits"] for o in narrowed)
    regions = api.tag_regions(env.db, ELEC, 9, max_regions=2)
    assert 1 <= len(regions) <= 2 and all(0 <= v <= 1 for r in regions for v in r)
    assert api.tag_regions(env.db, ELEC, 9, status="(X)") == []  # none on the new work plan
    assert len(api.tag_occurrences(env.db, ELEC, 7, status="(X)")) == 34
    locs = api.panel_locations(env.db, env.docs)
    assert isinstance(locs, list)
    views = api.views(env.db, ELEC, 6)
    assert any(v["ft_per_in"] == 16.0 for v in views)
    assert len(api.note_blocks(env.db, ELEC, 1)) == 2


def test_spec_lookup_refs_and_tiers(env):
    out = api.spec_lookup(env.db, env.docs, section="26")
    (hit,) = out["hits"]
    assert hit["ref"] == f"sp:{SPEC[:8]}:3:26 05 19" and hit["tier"] == "A" and hit["sent"] == "pages"
    kind, parts = tools.parse_ref(hit["ref"])
    assert kind == "sp" and parts == (SPEC[:8], "3", "26 05 19")
    kw = api.spec_lookup(env.db, env.docs, keyword="temporary")
    assert kw["hits"][0]["section"] == "01 50 00" and "electrical contractor" in kw["hits"][0]["text"]
    assert kw["hits"][0]["sent"] == "snippets"
    with pytest.raises(FactsUnavailable):
        api.spec_lookup(env.db, [d for d in env.docs if d["id"] == ELEC], section="26")


def test_ledger_facts_blocks_and_caps(env):
    caps = ledger_mod.LedgerCaps(legend_tokens=6000, schedule_tokens=1500, scales_tokens=300, block_tokens=2500)
    facts = api.ledger_facts(env.db, env.docs, caps=caps)
    assert len(facts["legend"]) >= 60 and facts["legend_source"]["sheet_number"] == "E0.00"
    assert facts["legend_text"].startswith("MODIFIERS:") and "LEGEND (exact text" in facts["legend_text"]
    assert block.estimate_tokens(facts["legend_text"]) <= 6000
    assert "PANEL 1EELC" in facts["panels_text"] and "ckt 1" in facts["panels_text"] and "GFCI" in facts["panels_text"]
    assert block.estimate_tokens(facts["panels_text"]) <= 1500
    assert "L5:" in facts["luminaires_text"] and "LINEAR" in facts["luminaires_text"] and "control LOCAL" in facts["luminaires_text"]
    assert 'E1.00 ("ELEC.pdf" page 6' in facts["scales_text"] and "16 ft per in" in facts["scales_text"]
    # Sub requests number documents per group, so the shared ledger never
    # prints a D number: every block names the document and page instead.
    import re as _re

    for key in ("legend_text", "panels_text", "luminaires_text", "scales_text"):
        assert not _re.search(r"\bD\d+ P\d+", facts[key]), key
    assert '(E0.00 ("ELEC.pdf" page 1))' in facts["legend_text"] or 'E0.00 ("ELEC.pdf" page 1)' in facts["legend_text"]
    assert '"ELEC.pdf" page 4' in facts["panels_text"] and '"ELEC.pdf" page 5' in facts["luminaires_text"]
    assert block.estimate_tokens(facts["scales_text"]) <= 300
    assert [p["sheet_number"] for p in facts["legend_pages"]] == ["E0.00"]
    assert facts["spec_snippets"][SPEC]["placement"] == "first_group" and facts["spec_snippets"][SPEC]["tokens"] > 0
    assert facts["spec_note"] is None  # the synthetic spec carries Division 26
    tight = ledger_mod.ledger_facts(api.load_run_facts(env.db, env.docs).pairs(), caps=ledger_mod.LedgerCaps(legend_tokens=300))
    assert "more legend entries not shown" in tight["legend_text"]
    # The cap cuts the ranked entries (the modifiers always print in full).
    assert len(tight["legend_text"]) < len(facts["legend_text"]) and "(100 more" in tight["legend_text"]
    drawing = ledger_mod.facts_text(facts, audience="drawing")
    assert any(line.startswith("MODIFIERS") for line in drawing) and any("PANEL SCHEDULES" in line for line in drawing)
    file_lines = ledger_mod.facts_text(facts, audience="file")
    assert not any("PANEL SCHEDULES" in line for line in file_lines)
    with pytest.raises(ValueError):
        ledger_mod.facts_text(facts, audience="chat")


def test_page_text_block_is_capped(env):
    pf = env.elec.page(9)
    full = api.page_text_block(pf, env.elec, max_tokens=2500)
    assert full.startswith("FACTS FOR PAGE 9 (sheet E2.01") and "CIRCUIT TAGS" in full
    short = api.page_text_block(pf, env.elec, max_tokens=60)
    assert short.endswith("(facts block truncated)") and block.estimate_tokens(short) <= 60


def test_run_level_calls_raise_without_finished_facts(env):
    only_pending = [d for d in env.docs if d["id"] == PENDING]
    for fn in (api.sheet_index, api.panel_locations):
        with pytest.raises(FactsUnavailable):
            fn(env.db, only_pending)
    with pytest.raises(FactsUnavailable):
        api.legend_lookup(env.db, only_pending, "JB")
    with pytest.raises(FactsUnavailable):
        api.ledger_facts(env.db, only_pending, caps=ledger_mod.LedgerCaps())

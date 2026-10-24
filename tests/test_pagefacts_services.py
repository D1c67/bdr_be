"""pagefacts.services: the WS1 adapters of the WS2 Protocols. Census is
`unavailable` (count null) on a vector_text page and for symbol patterns,
text-layer censuses carry `c:` refs with occurrences and clusters, the
text index answers `t:` refs and searches, the schedule and legend indexes
answer `s:` and `l:` refs, every ref parses with plans_reader_tools,
census ids are deterministic, the seven WS1 tools are registered with
strict schemas and the 5.2 kinds and run through a ToolContext bound to one
run, and `register_services` still fills the four registry slots (tests
and scripts only).

No network, no database, no model.
"""

from __future__ import annotations

import pytest

from app.services import plans_reader_tools as tools
from app.services.pagefacts import api, extract, services
from tests.pagefacts_db import (
    FakeDB,
    e_set_pdf,
    fixture_facts,
    install_storage,
    seed_cache,
    seed_document,
    synthetic_words,
    vector_text_pdf,
)

RUN = "22222222-2222-4222-8222-222222222222"
ELEC = "dddddddd-0000-4000-8000-000000000001"
VEC = "eeeeeeee-0000-4000-8000-000000000002"


@pytest.fixture
def env(monkeypatch):
    db = FakeDB()
    blobs = install_storage(monkeypatch)
    api.clear_caches()
    elec = fixture_facts("elec")
    seed_cache(db, blobs, elec, synthetic_words(e_set_pdf()))
    seed_document(db, RUN, elec, doc_id=ELEC, category="electrical_drawings", name="ELEC.pdf")
    vec = extract.extract_document(vector_text_pdf(), label="electrical_drawings", page_budget_s=4.0, doc_budget_s=60.0)
    assert vec.pages[0].text_class == "vector_text"
    seed_cache(db, blobs, vec, synthetic_words(vector_text_pdf()))
    seed_document(db, RUN, vec, doc_id=VEC, category="electrical_drawings", name="VEC.pdf")
    docs = db.rows("plans_reader_documents", run_id=RUN)
    yield type("Env", (), {"db": db, "docs": docs, "elec": elec, "svc": services.build(lambda: db, docs)})()
    api.clear_caches()
    tools.reset_services()


def test_census_is_unavailable_on_a_vector_text_page_and_for_symbols(env):
    out = env.svc["census"].census(VEC, 1, {"kind": "tag", "value": "1HD-21"}, None)
    assert out["method"] == "unavailable" and out["count"] is None and out["occurrences"] == []
    assert "vector_text" in out["note"] and "never 0" in out["note"]
    assert tools.parse_ref(out["ref"])[0] == "c"
    sym = env.svc["census"].census(ELEC, 9, {"kind": "symbol", "value": "duplex"}, None)
    assert sym["method"] == "unavailable" and sym["count"] is None and "vector matcher" in sym["note"]
    missing = env.svc["census"].census("nope", 1, {"kind": "tag", "value": "A-1"}, None)
    assert missing["method"] == "unavailable"


def test_text_layer_census_counts_tags_with_refs_and_clusters(env):
    tags = env.elec.page(9).tags["circuit"]
    best: dict[tuple[str, int], int] = {}
    for t in tags:
        for c in t.get("circuits") or []:
            best[(t["panel"], c)] = best.get((t["panel"], c), 0) + 1
    (panel, ckt), n = max(best.items(), key=lambda kv: kv[1])
    out = env.svc["census"].census(ELEC, 9, {"kind": "tag", "value": f"{panel}-{ckt}"}, None)
    assert out["method"] == "text_layer" and out["count"] == n == len(out["occurrences"])
    kind, parts = tools.parse_ref(out["occurrences"][0]["ref"])
    assert kind == "c" and parts[0] == out["census_id"] and parts[1] == "0"
    assert out["clusters"] and sum(c["n"] for c in out["clusters"]) == n
    assert out["family_specific"] is False
    status = env.svc["census"].census(ELEC, 7, {"kind": "tag", "value": "(X)"}, None)
    assert status["count"] == 34
    boxed = env.svc["census"].census(ELEC, 7, {"kind": "tag", "value": "(X)"}, [0.0, 0.0, 0.5, 1.0])
    assert boxed["family_specific"] is True and 0 < boxed["count"] <= 34
    mark = env.svc["census"].census(ELEC, 10, {"kind": "tag", "value": "L5"}, None)
    assert mark["method"] == "text_layer" and mark["family_specific"] is True and mark["count"] >= 1
    empty = env.svc["census"].census(ELEC, 9, {"kind": "tag", "value": "ZZ-99"}, None)
    assert empty["count"] == 0 and "unknown, not 0" in empty["note"]


def test_text_index_runs_and_search(env):
    runs = env.svc["text"].runs(ELEC, 1)
    assert runs and runs[0]["ref"].startswith(f"t:{ELEC[:8]}:1:")
    kind, parts = tools.parse_ref(runs[0]["ref"])
    assert kind == "t" and parts == (ELEC[:8], "1", "0")
    assert all(r["region"] for r in runs)
    found = env.svc["text"].search("SHEET", document_ids=[ELEC], pages=[1, 2], mode="token", limit=5, offset=0)
    assert found["total"] >= 1 and found["hits"][0]["sheet"] == "E0.00"
    exact = env.svc["text"].search("sheet no.", document_ids=[ELEC], mode="exact", limit=5, offset=0)
    assert exact["total"] >= 1 and exact["hits"][0]["text"].lower() == "sheet no."
    prefix = env.svc["text"].search("she", document_ids=None, pages=None, mode="prefix", limit=1, offset=0)
    assert prefix["omitted"] >= 1 and prefix["next_offset"] == 1
    bad = env.svc["text"].search("x", document_ids=[ELEC], mode="regex", limit=5, offset=0)
    assert bad["error"] == "bad_args"
    assert env.svc["text"].runs("nope", 1)["error"] == "facts_unavailable"


def test_schedule_and_legend_indexes(env):
    out = env.svc["schedules"].schedules(document_id=ELEC, kind="panel", name="1EELC", row_key="7")
    (sched,) = out["schedules"]
    assert [r["key"] for r in sched["rows"]] == ["7"] and tools.parse_ref(sched["rows"][0]["ref"])[0] == "s"
    assert env.svc["schedules"].schedules(document_id=None, kind="luminaire", name=None, row_key=None)["schedules"]
    legend = env.svc["legend"].lookup("ECONN", effect=None)
    assert legend["entries"] and tools.parse_ref(legend["entries"][0]["ref"])[0] == "l"
    assert env.svc["legend"].lookup(None, effect="remove")["modifiers"]


def test_unbound_adapters_need_a_document_scope(env):
    unbound = services.build(lambda: env.db)
    out = unbound["legend"].lookup("JB", effect=None)
    assert out["error"] == "facts_unavailable" and "documents" in out["message"]
    assert unbound["legend"].lookup("JB", effect=None, documents=env.docs)["entries"]
    assert unbound["schedules"].schedules(document_id=ELEC, kind="panel", name=None, row_key=None)["schedules"]
    assert unbound["text"].search("sheet", document_ids=[ELEC], pages=None, mode="token", limit=3, offset=0)["total"] >= 1


def test_register_services_fills_the_registry(env):
    tools.reset_services()
    reg = services.register_services(lambda: env.db, env.docs)
    assert {"text", "census", "schedules", "legend"} <= set(reg.available())
    assert tools.services().census.census(ELEC, 9, {"kind": "symbol", "value": "x"}, None)["method"] == "unavailable"
    bound = services.for_run(env.db, RUN)
    assert bound["legend"].lookup("JB", effect=None)["entries"]


def test_census_ids_are_deterministic_across_adapters_and_distinct_per_query(env):
    a = services.build(lambda: env.db, env.docs)["census"]
    b = services.for_run(env.db, RUN)["census"]
    q = {"kind": "tag", "value": "(X)"}
    first = a.census(ELEC, 7, q, None)
    again = b.census(ELEC, 7, {"kind": "tag", "value": " (x) "}, None)
    assert first["census_id"] == again["census_id"] and first["ref"] == again["ref"]
    assert first["census_id"].startswith(f"{ELEC[:8]}p7h")
    boxed = a.census(ELEC, 7, q, [0.0, 0.0, 0.5, 1.0])
    other_page = a.census(ELEC, 8, q, None)
    other_value = a.census(ELEC, 7, {"kind": "text", "value": "(X)"}, None)
    ids = {first["census_id"], boxed["census_id"], other_page["census_id"], other_value["census_id"]}
    assert len(ids) == 4
    assert services.census_id_for(ELEC, 7, q, [0, 0, 0.5, 1]) == boxed["census_id"]
    # The issuer resolves its own ids (zoom census_ref); unknown ids fall back.
    assert a.lookup(first["census_id"])["count"] == 34
    assert tools.is_unavailable(b.lookup("deadbeefp1h0000000000"))


def test_ws1_tools_are_registered_strict_with_the_5_2_kinds():
    services.register_tools()
    all_kinds = tools.ALL_TOOL_KINDS
    expected = {
        "sheet_index": all_kinds,
        "text_search": all_kinds,
        "census": tools.kinds_from_codes("RC, M, RV, C"),
        "get_schedule": all_kinds,
        "legend_lookup": all_kinds,
        "keynote_lookup": tools.kinds_from_codes("RC, M, RV, C"),
        "spec_lookup": tools.kinds_from_codes("M, RV, C"),
    }
    assert set(services.TOOL_NAMES) == set(expected)
    for name, kinds in expected.items():
        spec = tools.REGISTRY[name]
        assert spec.kinds == kinds, name
        assert spec.mode == "read" and spec.cost_class == "free" and not spec.internal_pricing
        assert spec.args_schema["additionalProperties"] is False
        assert set(spec.args_schema["required"]) == set(spec.args_schema["properties"])
    recount = {s.name for s in tools.offered_tools("recount", mode="read")}
    assert {"census", "legend_lookup", "sheet_index", "text_search", "keynote_lookup", "get_schedule"} <= recount
    assert "spec_lookup" not in recount
    assert tools.offered_tools("sub") == []
    bad = tools.validate_args(tools.REGISTRY["census"], {"document_id": ELEC, "page": 1, "pattern": {"kind": "regex", "value": "x"}, "region": None})
    assert bad["code"] == "invalid_args"
    assert tools.validate_args(tools.REGISTRY["text_search"], {"query": "x"})["code"] == "invalid_args"


def _ctx(env, **kw):
    return tools.ToolContext(run_id=RUN, kind="recount", **kw)


def test_tools_run_through_a_context_bound_to_the_run(env):
    services.register_tools()
    tools.reset_services()
    bound = services.bind_services(None, env.db, RUN)
    assert all(isinstance(getattr(bound, n), services._Bound) and getattr(bound, n).bound for n in services.WS1_SLOTS)
    ctx = _ctx(env, services=bound)
    sheets = tools.dispatch(tools.REGISTRY["sheet_index"], ctx, {})
    assert not sheets.refused and sheets.data["sheets"][0]["sheet_number"] == "E0.00"
    legend = tools.dispatch(tools.REGISTRY["legend_lookup"], ctx, {"query": "ECONN", "effect": None})
    assert legend.data["entries"] and all(c.startswith("l:") for c in legend.citations)
    assert set(legend.citations) <= ctx.ledger_refs
    census = tools.dispatch(tools.REGISTRY["census"], ctx, {"document_id": ELEC, "page": 7, "pattern": {"kind": "tag", "value": "(X)"}, "region": None})
    assert census.data["count"] == 34 and census.citations[0] == census.data["ref"] and len(census.citations) == 35
    sched = tools.dispatch(tools.REGISTRY["get_schedule"], ctx, {"document_id": None, "kind": "panel", "name": "1EELC", "row_key": "7"})
    assert sched.citations and sched.citations[0].startswith("s:")
    found = tools.dispatch(tools.REGISTRY["text_search"], ctx, {"query": "sheet", "document_id": None, "pages": None, "sheets": ["E0.00"], "mode": "token", "limit": 5, "offset": None})
    assert found.data["total"] >= 1 and all(h["page"] == 1 and h["document_id"] == ELEC for h in found.data["hits"])
    keynotes = tools.dispatch(tools.REGISTRY["keynote_lookup"], ctx, {"sheet": "E0.03", "number": None})
    assert not keynotes.refused and keynotes.data["sheet_number"] == "E0.03"
    spec = tools.dispatch(tools.REGISTRY["spec_lookup"], ctx, {"section": None, "keyword": "temporary", "limit": 3})
    assert spec.data["error"] == "facts_unavailable"  # this run has no specification document
    # A document of another run is refused, never read.
    foreign = tools.dispatch(tools.REGISTRY["census"], ctx, {"document_id": "ffffffff-0000-4000-8000-000000000009", "page": 1, "pattern": {"kind": "tag", "value": "(X)"}, "region": None})
    assert foreign.refused and foreign.code == "unknown_document"


def test_handlers_bind_the_run_from_the_context_when_only_defaults_are_registered(env):
    services.register_tools()
    tools.reset_services()
    ctx = _ctx(env, sb=env.db)
    out = tools.dispatch(tools.REGISTRY["legend_lookup"], ctx, {"query": "JB", "effect": None})
    assert out.data["entries"], out.data
    assert isinstance(ctx.services.legend, services._Bound) and ctx.services.legend.bound
    # The unbound process adapters (register_services) are replaced the same way.
    services.register_services(lambda: env.db)
    ctx2 = _ctx(env, sb=env.db)
    assert tools.dispatch(tools.REGISTRY["get_schedule"], ctx2, {"document_id": None, "kind": "luminaire", "name": None, "row_key": None}).data["schedules"]
    # Without a client and without bound adapters the tools say so instead of guessing.
    tools.reset_services()
    bare = tools.dispatch(tools.REGISTRY["sheet_index"], _ctx(env), {})
    assert bare.data["error"] == "facts_unavailable"


def test_bind_services_keeps_other_providers_and_needs_finished_facts(env):
    fake_text = tools.FakeTextIndex({})
    base = tools.ToolServices(text=fake_text)
    bound = services.bind_services(base, env.db, RUN)
    assert bound.text is fake_text and isinstance(bound.legend, services._Bound)
    pending = [dict(d, facts_status="pending") for d in env.docs]
    untouched = services.bind_services(base, env.db, RUN, documents=pending)
    assert isinstance(untouched.legend, tools.Unavailable)

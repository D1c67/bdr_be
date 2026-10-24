"""Tool registry, compaction, service seams and citation rules
(services/plans_reader_tools.py, spec sections 5.2 and 5.3).

No network, no database: every tool here is a sample registered by the test
and served from a fake. The registry and the service registry are restored
after each test so nothing leaks between them.
"""

from __future__ import annotations

import pytest

from app.services import plans_reader_tools as tools
from app.services.plans_reader_tools import (
    REGISTRY,
    FakeCensusService,
    FakeTextIndex,
    RowProblem,
    ToolContext,
    ToolResult,
    ToolSpec,
    compact_round,
    compact_text,
    dispatch,
    fake_services,
    kinds_from_codes,
    offered_tools,
    parse_ref,
    register,
    register_services,
    reset_services,
    services,
    to_anthropic_tools,
    to_openai_tools,
    validate_args,
    validate_rows,
)

DOC = "1a2b3c4d-0000-4000-8000-000000000001"
DOC8 = "1a2b3c4d"


@pytest.fixture(autouse=True)
def _isolate_registry():
    saved = dict(REGISTRY)
    REGISTRY.clear()
    yield
    REGISTRY.clear()
    REGISTRY.update(saved)
    reset_services()


def _schema(props: dict | None = None) -> dict:
    props = props or {}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(props.keys()),
        "properties": props,
    }


SEARCH_SCHEMA = _schema(
    {
        "query": {"type": "string", "minLength": 1, "maxLength": 120},
        "pages": {"type": ["array", "null"], "items": {"type": "integer"}},
        "limit": {"type": ["integer", "null"]},
    }
)


def _search_handler(ctx: ToolContext, args: dict) -> ToolResult:
    page = ctx.svc().text.search(
        args["query"],
        document_ids=None,
        pages=args.get("pages"),
        mode="token",
        limit=args.get("limit") or 40,
        offset=0,
    )
    return ToolResult.ok(page, citations=[h["ref"] for h in page["hits"]])


def _spec(name: str = "text_search", **over) -> ToolSpec:
    base = dict(
        name=name,
        description="Search the text layer. Returns hits with refs.",
        args_schema=SEARCH_SCHEMA,
        result_doc={"type": "object"},
        mode="read",
        kinds=kinds_from_codes("all"),
        handler=_search_handler,
    )
    base.update(over)
    return ToolSpec(**base)


def _ctx(**over) -> ToolContext:
    text = FakeTextIndex(
        {
            (DOC, 5): [
                {"text": "L4 TASK LED LOCAL", "bbox": [0.1, 0.1, 0.2, 0.12], "sheet": "E2.01"},
                {"text": "PANEL LP-1", "bbox": [0.5, 0.5, 0.6, 0.52], "sheet": "E2.01"},
            ]
        }
    )
    base = dict(run_id="run-1", kind="recount", group_id="g-1", services=fake_services(text=text))
    base.update(over)
    return ToolContext(**base)


# -- registration ----------------------------------------------------------------


def test_register_rejects_non_strict_schema():
    loose = {"type": "object", "properties": {"q": {"type": "string"}}}
    with pytest.raises(ValueError, match="not strict"):
        register(_spec(args_schema=loose))
    missing_required = {
        "type": "object",
        "additionalProperties": False,
        "required": [],
        "properties": {"q": {"type": "string"}},
    }
    with pytest.raises(ValueError, match="required must list every property"):
        register(_spec(args_schema=missing_required))
    assert REGISTRY == {}


def test_register_rejects_model_cost_class_and_bad_fields():
    with pytest.raises(ValueError, match="cost_class"):
        register(_spec(cost_class="model"))
    with pytest.raises(ValueError, match="mode"):
        register(_spec(mode="execute"))
    with pytest.raises(ValueError, match="kinds"):
        register(_spec(kinds=frozenset({"nope"})))
    with pytest.raises(ValueError, match="tool name"):
        register(_spec(name="Text-Search"))
    register(_spec())
    with pytest.raises(ValueError, match="already registered"):
        register(_spec())
    register(_spec(), replace=True)
    assert list(REGISTRY) == ["text_search"]


def test_register_refuses_kinds_without_tools():
    for kinds in (frozenset({"sub"}), frozenset({"escalation", "main"})):
        with pytest.raises(ValueError, match="sub and escalation get no tools"):
            register(_spec(kinds=kinds))
    assert REGISTRY == {}


def test_offered_tools_empty_for_kinds_without_tools():
    register(_spec())
    assert offered_tools("sub") == []
    assert offered_tools("escalation") == []
    assert [s.name for s in offered_tools("main")] == ["text_search"]


def test_kinds_from_codes_maps_the_catalog_table():
    assert kinds_from_codes("RC, M, RV") == frozenset({"recount", "main", "review"})
    assert kinds_from_codes("all") == tools.ALL_TOOL_KINDS
    assert "sub" not in tools.ALL_TOOL_KINDS
    assert kinds_from_codes(["D", "C"]) == frozenset({"derive", "chat"})
    with pytest.raises(ValueError):
        kinds_from_codes("XX")


# -- offer -----------------------------------------------------------------------


def test_offered_tools_sorted_by_name_and_filtered_by_kind():
    register(_spec("zoom_region", kinds=kinds_from_codes("RC, RV, C"), needs_pro_max=True))
    register(_spec("census", kinds=kinds_from_codes("RC, M, RV, C")))
    register(_spec("text_search"))
    register(_spec("get_rows", kinds=kinds_from_codes("M, RV, RS, C")))
    names = [s.name for s in offered_tools("recount")]
    assert names == ["census", "text_search", "zoom_region"]
    assert [s.name for s in offered_tools("main")] == ["census", "get_rows", "text_search"]
    assert [s.name for s in offered_tools("derive")] == ["text_search"]
    with pytest.raises(ValueError):
        offered_tools("nope")


def test_pricing_tools_hidden_unless_pricing_allowed():
    register(_spec("price_lookup", kinds=kinds_from_codes("M, RV, RS, C"), internal_pricing=True))
    register(_spec("get_rows", kinds=kinds_from_codes("M, RV, RS, C")))
    assert [s.name for s in offered_tools("chat", role="accountant")] == ["get_rows"]
    assert [s.name for s in offered_tools("chat", role="executive", pricing_allowed=True)] == [
        "get_rows",
        "price_lookup",
    ]
    # The external estimator never sees pricing, whatever the caller passes.
    assert [s.name for s in offered_tools("chat", role="estimator", pricing_allowed=True)] == [
        "get_rows"
    ]
    # Batch groups get them only when the caller says pricing is allowed too.
    assert [s.name for s in offered_tools("main")] == ["get_rows"]
    assert [s.name for s in offered_tools("main", pricing_allowed=True)] == [
        "get_rows",
        "price_lookup",
    ]


def test_pro_max_gating_applies_to_chat_only():
    register(_spec("zoom_region", kinds=kinds_from_codes("RC, RV, C"), needs_pro_max=True))
    assert offered_tools("chat") == []
    assert [s.name for s in offered_tools("chat", pro_max=True)] == ["zoom_region"]
    assert [s.name for s in offered_tools("recount")] == ["zoom_region"]


def test_offered_tools_mode_policy():
    register(_spec("text_search"))
    register(_spec("record_finding", mode="review", kinds=kinds_from_codes("RV")))
    register(_spec("add_boq_line", mode="write", kinds=kinds_from_codes("M")))
    assert [s.name for s in offered_tools("review", mode="read")] == ["text_search"]
    assert [s.name for s in offered_tools("review", mode="review")] == [
        "record_finding",
        "text_search",
    ]
    assert [s.name for s in offered_tools("main", mode="write")] == ["add_boq_line", "text_search"]
    assert [s.name for s in offered_tools("main", mode="read")] == ["text_search"]


# -- validation and dispatch ------------------------------------------------------


def test_validate_args_returns_structured_refusal():
    spec = _spec()
    assert validate_args(spec, {"query": "L4", "pages": None, "limit": None}) is None
    bad = validate_args(spec, {"query": "L4", "pages": None})
    assert bad["refused"] is True
    assert bad["code"] == "invalid_args"
    assert "limit" in bad["message"]
    assert bad["detail"][0]["path"] == "$"
    extra = validate_args(spec, {"query": "L4", "pages": None, "limit": 5, "regex": ".*"})
    assert extra["code"] == "invalid_args"
    assert "regex" in extra["message"]
    typed = validate_args(spec, {"query": "L4", "pages": ["5"], "limit": 5})
    assert typed["detail"][0]["path"] == "$.pages[0]"
    assert validate_args(spec, ["not", "an", "object"])["code"] == "invalid_args"


def test_dispatch_sample_tool_through_a_fake():
    spec = register(_spec())
    ctx = _ctx()
    beats = []
    ctx.heartbeat = lambda: beats.append(1)
    result = dispatch(spec, ctx, {"query": "L4", "pages": [5], "limit": None})
    assert not result.refused
    assert result.data["total"] == 1
    assert result.citations == [f"t:{DOC8}:5:0"]
    assert f"t:{DOC8}:5:0" in ctx.ledger_refs
    assert "L4 TASK LED LOCAL" in result.text
    assert beats == [1]
    assert ctx.calls_made("text_search") == 1
    record = result.to_record()
    assert record["result_data"]["hits"][0]["sheet"] == "E2.01"
    assert record["refused"] is False


def test_dispatch_invalid_args_refusal_does_not_count_or_call():
    calls = []

    def handler(ctx, args):
        calls.append(args)
        return ToolResult.ok({"ok": True})

    spec = register(_spec(handler=handler))
    ctx = _ctx()
    result = dispatch(spec, ctx, {"query": "L4"})
    assert result.refused and result.code == "invalid_args"
    assert result.data["refused"] is True
    assert calls == []
    assert ctx.calls_made("text_search") == 0


def test_dispatch_enforces_max_calls_per_group():
    spec = register(_spec(max_calls_per_group=2))
    ctx = _ctx()
    args = {"query": "panel", "pages": None, "limit": None}
    assert not dispatch(spec, ctx, args).refused
    assert not dispatch(spec, ctx, args).refused
    third = dispatch(spec, ctx, args)
    assert third.refused and third.code == "max_calls"
    assert third.data["detail"] == {"calls_made": 2, "max_calls_per_group": 2}
    # Another context (another group) starts fresh.
    assert not dispatch(spec, _ctx(group_id="g-2"), args).refused


def test_dispatch_turns_handler_exception_into_tool_error():
    def boom(ctx, args):
        raise KeyError("sheet")

    spec = register(_spec(handler=boom))
    result = dispatch(spec, _ctx(), {"query": "x", "pages": None, "limit": None})
    assert result.refused and result.code == "tool_error"
    assert "KeyError" in result.message
    assert result.data["detail"] == {"exception": "KeyError"}


def test_dispatch_lets_a_lost_lease_propagate():
    class LeaseLost(RuntimeError):
        pass

    def heartbeat():
        raise LeaseLost("gone")

    spec = register(_spec())
    ctx = _ctx(heartbeat=heartbeat)
    with pytest.raises(LeaseLost):
        dispatch(spec, ctx, {"query": "x", "pages": None, "limit": None})


def test_dispatch_against_unavailable_service_returns_typed_error():
    spec = register(_spec())
    ctx = _ctx(services=None)  # falls back to the process registry: all Unavailable
    result = dispatch(spec, ctx, {"query": "L4", "pages": None, "limit": None})
    # The fake handler forwards the service's error result as data; the
    # typed shape is what every sibling's handler relies on.
    assert result.refused or tools.is_unavailable(result.data) or result.code == "tool_error"
    assert tools.is_unavailable(services().text.search("x"))
    assert services().text.search("x")["service"] == "text"
    assert services().measure.measure_polyline(DOC, 1, [[0, 0], [1, 1]], None)["error"] == (
        "unavailable"
    )


# -- provider definitions ---------------------------------------------------------


def test_tool_definition_builders():
    spec = _spec()
    openai = to_openai_tools([spec])
    assert openai == [
        {
            "type": "function",
            "name": "text_search",
            "description": spec.description,
            "parameters": SEARCH_SCHEMA,
            "strict": True,
        }
    ]
    anthropic = to_anthropic_tools([spec])
    assert anthropic == [
        {
            "name": "text_search",
            "description": spec.description,
            "input_schema": SEARCH_SCHEMA,
            "strict": True,
        }
    ]
    assert "strict" not in to_anthropic_tools([spec], strict=False)[0]


# -- compaction ------------------------------------------------------------------


def test_compaction_per_result_and_per_round():
    text, cut = compact_text("x" * 100, 200)
    assert text == "x" * 100 and cut is False
    text, cut = compact_text("y" * 20_000, tools.RESULT_CHAR_LIMIT)
    assert cut is True
    assert len(text) <= tools.RESULT_CHAR_LIMIT
    assert "[truncated:" in text and "offset" in text
    assert tools.result_limit("get_output") == 40_000
    assert tools.result_limit("get_deliverable") == 40_000
    assert tools.result_limit("text_search") == 12_000

    big = ToolResult(text="a" * 15_000)
    tools.compact_result(big, tool_name="text_search")
    assert big.truncated and len(big.text) <= 12_000
    large = ToolResult(text="b" * 15_000)
    tools.compact_result(large, tool_name="get_output")
    assert not large.truncated

    results = [ToolResult(text="s" * 1_000)] + [ToolResult(text="L" * 40_000) for _ in range(3)]
    compact_round(results)
    assert sum(len(r.text) for r in results) <= tools.ROUND_CHAR_LIMIT
    assert results[0].truncated is False and len(results[0].text) == 1_000
    assert all(r.truncated for r in results[1:])
    lengths = {len(r.text) for r in results[1:]}
    assert len(lengths) == 1  # the long ones share the rest equally


def test_json_lines_pages_and_render_drops_nulls():
    items = [{"id": f"C{i}", "qty": i, "note": None, "bbox": [0.123456, 0.2]} for i in range(5)]
    text, omitted, next_offset = tools.json_lines(items, offset=0, limit=2)
    assert text.splitlines()[0] == '{"id":"C0","qty":0,"bbox":[0.1235,0.2]}'
    assert omitted == 3 and next_offset == 2
    assert "next_offset=2" in text
    text, omitted, next_offset = tools.json_lines(items, offset=4, limit=2)
    assert omitted == 0 and next_offset is None
    long_quote = tools.render_json({"text": "q" * 400})
    assert long_quote.endswith(' [cut]"}')


def test_refusal_result_shape():
    r = ToolResult.refusal("unknown_ref", "citation x is unknown", {"ref": "x"})
    assert r.refused and r.code == "unknown_ref"
    assert r.data == {
        "refused": True,
        "code": "unknown_ref",
        "message": "citation x is unknown",
        "detail": {"ref": "x"},
    }
    assert '"refused":true' in r.text


# -- services --------------------------------------------------------------------


def test_register_services_replaces_named_slots_only():
    assert services().available() == []
    text = FakeTextIndex()
    register_services(text=text)
    assert services().text is text
    assert services().available() == ["text"]
    assert isinstance(services().census, tools.Unavailable)
    with pytest.raises(ValueError, match="unknown service"):
        register_services(pricing=object())
    reset_services()
    assert services().available() == []


def test_fake_census_never_reports_zero_for_unknown():
    census = FakeCensusService({(DOC, 5, "L4"): {"count": 17, "family_specific": True}})
    hit = census.census(DOC, 5, {"kind": "tag", "value": "L4"}, None)
    assert hit["count"] == 17 and hit["method"] == "text_layer"
    assert hit["ref"].startswith(f"c:{DOC8}p5")
    miss = census.census(DOC, 5, {"kind": "tag", "value": "ZZ"}, None)
    assert miss["count"] is None and miss["method"] == "unavailable"
    symbol = census.census(DOC, 5, {"kind": "symbol", "value": "l:leg1:3"}, None)
    assert symbol["method"] == "unavailable"


def test_fake_measure_uses_sheet_size_and_scale():
    measure = tools.FakeMeasureService(w_in=36.0, h_in=24.0, ft_per_in=8.0)
    out = measure.measure_polyline(DOC, 3, [[0.0, 0.5], [0.5, 0.5]], 10.0)
    assert out["length_ft"] == pytest.approx(0.5 * 36.0 * 8.0 + 10.0)
    assert out["ref"] == f"m:{out['measure_id']}"


# -- citation refs -----------------------------------------------------------------


def test_parse_ref_grammar():
    assert parse_ref(f"t:{DOC8}:9:12") == ("t", (DOC8, "9", "12"))
    assert parse_ref("c:abc12") == ("c", ("abc12",))
    assert parse_ref("c:abc12:3") == ("c", ("abc12", "3"))
    assert parse_ref(f"s:{DOC8}:s2:7") == ("s", (f"{DOC8}:s2", "7"))
    assert parse_ref("s:sch1:7") == ("s", ("sch1", "7"))
    assert parse_ref("l:leg1:4") == ("l", ("leg1", "4"))
    assert parse_ref(f"sp:{DOC8}:14:260500") == ("sp", (DOC8, "14", "260500"))
    assert parse_ref("v:view7") == ("v", ("view7",))
    assert parse_ref("m:meas3") == ("m", ("meas3",))
    assert parse_ref("r:route2") == ("r", ("route2",))
    assert parse_ref("p:excl_temp_power") == ("p", ("excl_temp_power",))
    assert parse_ref("$:conduit_emt_34@lib7") == ("$", ("conduit_emt_34", "lib7"))
    assert parse_ref("f:fnd1") == ("f", ("fnd1",))
    for letter in "CRSPODQMFBK":
        assert parse_ref(f"{letter}12") == ("row", (letter, "12"))
    for bad in ("", "x:1", "T12", "C", "c:", "c:a:b", "s:7", f"t:{DOC8}:9", "$:conduit", 12):
        with pytest.raises(ValueError):
            parse_ref(bad)
    # The contract row id grammar: positive int, no leading zeros.
    for bad in ("B012", "B0", "C00", "K01"):
        assert not tools.is_ref(bad)
    assert tools.is_ref("B10")
    assert tools.census_id_of("c:abc:4") == "abc"
    assert tools.census_id_of("t:a:1:2") is None


def _row(**over) -> dict:
    base = dict(
        line_id="B1",
        description="2x4 LED troffer L4",
        quantity=17,
        unit="EA",
        basis="counted",
        source_refs=[{"ref": f"t:{DOC8}:5:0", "multiplier": 1}],
        coverage="sheet_total",
        sheet_key="D1P5",
    )
    base.update(over)
    return base


def _codes(problems: list[RowProblem]) -> list[str]:
    return [p.code for p in problems]


def test_validate_rows_counted_with_text_ref_passes():
    ledger = {f"t:{DOC8}:5:0"}
    assert validate_rows([_row()], ledger) == []
    # Bare string refs and a count row id resolved by the store work too.
    rows = [_row(source_refs=["C3"], basis="counted")]
    assert validate_rows(rows, set(), lambda ref: {"id": "C3"} if ref == "C3" else None) == []


def test_validate_rows_unknown_and_malformed_refs():
    problems = validate_rows([_row(source_refs=["t:zzzz:1:1", "bogus"])], set())
    assert _codes(problems) == ["unknown_ref", "malformed_ref"]
    assert "was not returned by any tool" in [p for p in problems if p.code == "unknown_ref"][
        0
    ].message


def test_validate_rows_measured_with_text_ref_refused():
    ledger = {f"t:{DOC8}:5:0", "m:meas1", "r:route1", "r:route2"}
    problems = validate_rows([_row(basis="measured", unit="FT")], ledger)
    assert _codes(problems) == ["basis_ref_mismatch"]
    assert "measured" in problems[0].message
    assert validate_rows([_row(basis="measured", unit="FT", source_refs=["m:meas1"])], ledger) == []

    def resolver(ref):
        return {"r:route1": {"basis": "measured"}, "r:route2": {"basis": "assumed_route"}}.get(ref)

    ok = _row(basis="measured", unit="FT", source_refs=["r:route1"])
    assert validate_rows([ok], ledger, resolver) == []
    assumed = _row(basis="measured", unit="FT", source_refs=["r:route2"])
    assert _codes(validate_rows([assumed], ledger, resolver)) == ["basis_ref_mismatch"]
    assumed_ok = _row(basis="assumed_route", unit="FT", source_refs=["r:route2"])
    assert validate_rows([assumed_ok], ledger, resolver) == []
    # An r: with no known basis is never a measurement.
    unknown = _row(basis="measured", unit="FT", source_refs=["r:route1"])
    assert _codes(validate_rows([unknown], ledger)) == ["basis_ref_mismatch"]


def test_validate_rows_basis_matrix():
    ledger = {"s:sch1:4", "p:rule1", "sp:" + DOC8 + ":14:260500", "c:cen1"}
    assert validate_rows([_row(basis="schedule", source_refs=["s:sch1:4"])], ledger) == []
    assert _codes(validate_rows([_row(basis="schedule")], ledger | {f"t:{DOC8}:5:0"})) == [
        "basis_ref_mismatch"
    ]
    assert validate_rows([_row(basis="policy", source_refs=["p:rule1"])], ledger) == []
    assert _codes(validate_rows([_row(basis="policy", source_refs=["c:cen1"])], ledger)) == [
        "basis_ref_mismatch"
    ]
    assert validate_rows([_row(basis="stated", source_refs=[f"sp:{DOC8}:14:260500"])], ledger) == []
    assert _codes(validate_rows([_row(basis="counted", source_refs=[])], ledger)) == [
        "unsourced_line"
    ]
    assert _codes(validate_rows([_row(basis="manual", source_refs=[])], ledger)) == [
        "manual_needs_user"
    ]
    assert validate_rows([_row(basis="manual", source_refs=[], user_id="u1")], ledger) == []
    assert _codes(validate_rows([_row(basis="teleported")], ledger | {f"t:{DOC8}:5:0"})) == [
        "unknown_basis"
    ]


def test_validate_rows_partial_coverage_cannot_be_sheet_total():
    ledger = {"v:view1", "v:view2", "c:cen9", f"t:{DOC8}:5:0"}

    def resolver(ref):
        return {
            "v:view1": {"coverage": {"targets": 3, "inside": 2, "complete": False}},
            "v:view2": {"coverage": {"targets": 3, "inside": 3, "complete": True}},
            "c:cen9": {"count": 17, "family_specific": True, "coverage": "partial"},
        }.get(ref)

    partial = _row(source_refs=["v:view1"], coverage="sheet_total")
    assert _codes(validate_rows([partial], ledger, resolver)) == ["partial_as_total"]
    # The same evidence declared as partial is fine.
    declared = _row(source_refs=["v:view1"], coverage="partial")
    assert validate_rows([declared], ledger, resolver) == []
    # One complete view among the citations makes a sheet total legitimate.
    both = _row(source_refs=["v:view1", "v:view2"], coverage="sheet_total")
    assert validate_rows([both], ledger, resolver) == []
    # A text-layer run is whole-sheet evidence.
    with_text = _row(source_refs=["v:view1", f"t:{DOC8}:5:0"], coverage="sheet_total")
    assert validate_rows([with_text], ledger, resolver) == []
    # A census whose result said partial coverage is partial evidence too
    # (and 17 == count, so no floor problem).
    census_partial = _row(source_refs=["c:cen9"], coverage="sheet_total")
    assert _codes(validate_rows([census_partial], ledger, resolver)) == ["partial_as_total"]


def test_validate_rows_census_floor_applies_to_the_sheet_sum():
    ledger = {"c:cen1", "c:cen2"}

    def resolver(ref):
        return {
            "c:cen1": {"count": 20, "family_specific": True},
            "c:cen2": {"count": 20, "family_specific": False},
        }.get(ref)

    # Two rows on the same sheet citing the same census: the sum (12 + 7 = 19)
    # is within tolerance of 20, so per-row shortfall is not a problem.
    rows = [
        _row(line_id="B1", quantity=12, source_refs=["c:cen1"], coverage="region_subtotal"),
        _row(line_id="B2", quantity=7, source_refs=["c:cen1"], coverage="region_subtotal"),
    ]
    assert validate_rows(rows, ledger, resolver) == []
    # Sum well below the floor is refused once, naming both rows.
    rows = [
        _row(line_id="B1", quantity=6, source_refs=["c:cen1"], coverage="region_subtotal"),
        _row(line_id="B2", quantity=7, source_refs=["c:cen1"], coverage="region_subtotal"),
    ]
    problems = validate_rows(rows, ledger, resolver)
    assert _codes(problems) == ["census_floor"]
    assert "B1, B2" in problems[0].message and "floor of 20" in problems[0].message
    # An override reason lifts it.
    rows[0]["override_reason"] = "7 of the tags are on the demolition layer"
    assert validate_rows(rows, ledger, resolver) == []
    # Rows on different sheets never sum together.
    rows = [
        _row(line_id="B1", quantity=6, source_refs=["c:cen1"], sheet_key="D1P5"),
        _row(line_id="B2", quantity=7, source_refs=["c:cen1"], sheet_key="D1P6"),
    ]
    assert _codes(validate_rows(rows, ledger, resolver)) == ["census_floor", "census_floor"]
    # A census that is not family specific is never a floor.
    rows = [_row(line_id="B1", quantity=3, source_refs=["c:cen2"])]
    assert validate_rows(rows, ledger, resolver) == []
    # Above the census by more than the tolerance is a warning without a caveat.
    rows = [_row(line_id="B1", quantity=30, source_refs=["c:cen1"])]
    problems = validate_rows(rows, ledger, resolver)
    assert [(p.code, p.severity) for p in problems] == [("census_over", "warn")]
    assert tools.blocking(problems) == []
    rows[0]["caveat"] = "includes the 10 on the enlarged plan"
    assert validate_rows(rows, ledger, resolver) == []


def test_validate_rows_linear_class_in_ea_refused():
    ledger = {"m:meas1", f"t:{DOC8}:5:0"}
    conduit = _row(
        description='3/4" EMT conduit',
        unit="EA",
        basis="measured",
        source_refs=["m:meas1"],
    )
    problems = validate_rows([conduit], ledger)
    assert _codes(problems) == ["linear_in_ea"]
    assert "conduit" in problems[0].message and "FT" in problems[0].message
    conduit["unit"] = "FT"
    assert validate_rows([conduit], ledger) == []
    # An explicit item_class wins over the description.
    wire = _row(description="misc", item_class="conductor", unit="EA")
    assert _codes(validate_rows([wire], ledger)) == ["linear_in_ea"]
    # A track head is a device, not a linear class.
    head = _row(description="track head, LED, 12W", unit="EA")
    assert validate_rows([head], ledger) == []
    assert set(tools.LINEAR_CLASSES) == {
        "conductor",
        "conduit",
        "cable",
        "raceway",
        "trench",
        "sawcut",
        "lighting_track",
    }


def test_validate_rows_demolition_of_a_linear_item_in_ea_refused():
    """is_linear(parse_item(...)): removing lighting track is still linear."""
    ledger = {f"t:{DOC8}:5:0"}
    removal = _row(description="REMOVAL OF LIGHTING TRACK", quantity=3, unit="EA")
    problems = validate_rows([removal], ledger)
    assert _codes(problems) == ["linear_in_ea"]
    assert "lighting_track" in problems[0].message
    removal["unit"] = "FT"
    assert validate_rows([removal], ledger) == []
    troffer = _row(description="2x4 LED troffer L4", unit="EA")
    assert validate_rows([troffer], ledger) == []


def test_validate_rows_census_tol_kwarg():
    ledger = {"c:cen1"}
    resolver = {"c:cen1": {"count": 40, "family_specific": True}}.get
    rows = [_row(basis="census_floor", quantity=37, unit="EA", source_refs=["c:cen1"],
                 sheet_key="D1P8", description="DUPLEX RECEPTACLE")]
    # Default (settings strong tol 0.05): tolerance max(1, ceil(2)) = 2, 37 < 38.
    assert _codes(validate_rows(rows, ledger, resolver)) == ["census_floor"]
    # A wider tolerance (0.10 -> 4) lets 37 pass.
    assert validate_rows(rows, ledger, resolver, census_tol=0.10) == []


def test_problems_refusal_lists_only_blocking():
    problems = [
        RowProblem("B1", "census_over", "over", severity="warn"),
        RowProblem("B2", "linear_in_ea", "conduit must be FT"),
    ]
    refusal = tools.problems_refusal(problems)
    assert refusal["refused"] is True and refusal["code"] == "citation"
    assert refusal["message"] == "conduit must be FT"
    assert [d["row_id"] for d in refusal["detail"]] == ["B2"]

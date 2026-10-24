"""plans_reader_recount: targets, tile ownership, outcome rules and a scripted
recount loop on the 7154 fixture (build spec WS7 wave 2). Every model
interaction is a scripted fake; synthetic prices only."""

from __future__ import annotations

import importlib.util
import math
import re

import pytest

from app.services import plans_reader_recount as rc
from app.services import plans_reader_rows as rows
from app.services.estimating_contracts import GateFinding
from app.services.plans_reader_review_schemas import RECOUNT_SCHEMA
from app.services.plans_reader_schemas import validate_strict
from app.services.plans_reader_tools import FakeCensusService, FakeLegendIndex, FakePriceService, fake_services

from tests.plans_reader_fixtures import ELEC_DOC_7154, load_7154, rows_meta
from tests.ws7_fake_sb import FakeSB

ROUTE = {"step": "recount", "provider": "openai", "model": "gpt-6-astra", "effort": "medium",
         "budget_usd": 2.0, "tools": True, "explicit": False}
ADJUDICATED_ED301_TROFFERS = 17  # spec WS7 acceptance (a count, not a price)


@pytest.fixture(scope="module")
def run7154() -> dict:
    return load_7154()


@pytest.fixture(scope="module")
def counts7154(run7154) -> list[dict]:
    v2 = rows.upgrade_category_output(run7154["electrical"], rows_meta(run7154))
    return [dict(r, kind="C", category="electrical_drawings") for r in v2["counts"]]


def _rect(counts7154) -> dict:
    return next(r for r in counts7154 if r["sheet"]["sheet_number"] == "ED3.01" and "rectangular" in r["description"])


# PARTIAL_RE ----------------------------------------------------------------------------


def test_partial_re_matches_the_ed301_escalation_notes(run7154):
    esc = [g for g in run7154["groups"] if g["kind"] == "escalation"
           and any(p["sheet_number"] == "ED3.01" for p in g["output"]["pages"])]
    assert len(esc) == 1
    out = esc[0]["output"]
    assert rc.PARTIAL_RE.search(out["counts"][0]["notes"])  # "New tile subtotal only ..."
    page = out["pages"][0]
    assert rc.PARTIAL_RE.search(page["notes"][2])  # "... a tile subtotal, not a complete sheet total."
    assert sum(1 for n in page["notes"] if rc.PARTIAL_RE.search(n)) == 5
    assert rc.PARTIAL_RE.search("Five complete symbols in enlarged tile only")  # BOQ B6 material note
    assert rc.PARTIAL_RE.search("counted in the tile")
    assert not rc.PARTIAL_RE.search("Open symbols without G/O prefix; includes above-counter locations")


def test_partial_re_never_fires_on_the_54_first_pass_rows(counts7154):
    sub = [r for r in counts7154 if r["origin"]["group_kind"] == "sub"]
    assert len(sub) == 54
    assert [r["row_id"] for r in sub if rc.PARTIAL_RE.search(rc._row_text(r))] == []


# Targets -------------------------------------------------------------------------------


def test_7154_targets_are_the_two_ed301_tile_subtotals(counts7154):
    targets = rc.select_targets(counts7154)
    assert [(t.row_id, t.priority, t.sheet_number) for t in targets] == [
        ("C22", "P1", "ED3.01"), ("C23", "P1", "ED3.01")]
    rect = targets[0]
    assert rect.builder_qty == 5 and rect.reason == "coverage tile_subtotal"
    assert rect.symbol == "Remove existing rectangular lighting fixture"
    assert not re.search(r"\d", rect.symbol)
    assert rect.document_id == ELEC_DOC_7154 and rect.page == 8 and rect.sheet_key == "D1P8"


def _row(rid, qty, *, sheet="D1P2", page=2, basis="counted_symbols", conf=0.9, desc=None, **extra):
    return {"row_id": rid, "kind": "C", "status": "active", "quantity": qty, "unit": "EA", "basis": basis,
            "confidence": conf, "description": desc or f"Duplex receptacle {rid}", "symbol_or_tag": None,
            "sheet": {"document_id": "doc-1", "page": page, "sheet_number": f"E{page}", "sheet_key": sheet},
            "coverage": "sheet_total", **extra}


def test_p2_census_shortfall_p3_low_confidence_p4_top_dollar():
    rows_in = [
        _row("C1", 10, family_tags=["l1@D1P2"], symbol_or_tag="L1", desc="L1 troffer"),
        _row("C2", 4, conf=0.4, desc="Pendant fixture"),
        _row("C3", 40, desc="Wall dimmer"),
        _row("C4", 2, desc="Exit sign"),
        _row("C5", 30, basis="schedule_text", desc="Panel breaker"),
    ]
    price = FakePriceService({
        "pendant fixture": {"uom": "EA", "material": {"median": 400.0}, "labor_hours": {"median": 1.0}},
        "wall dimmer": {"uom": "EA", "material": {"median": 30.0}, "labor_hours": {"median": 0.5}},
        "exit sign": {"uom": "EA", "material": {"median": 60.0}, "labor_hours": {"median": 0.5}},
        "panel breaker": {"uom": "EA", "material": {"median": 90.0}, "labor_hours": {"median": 0.5}},
    })
    floors = [{"sheet_key": "D1P2", "family": "l1", "floor": 14, "accounted": 10, "census_id": "c9",
               "family_specific": True}]
    targets = rc.select_targets(rows_in, floors=floors, price=price, labor_rate_usd=100.0, total_usd=10_000.0)
    got = {t.row_id: t.priority for t in targets}
    assert got["C1"] == "P2" and targets[0].census["count"] == 14
    assert got["C2"] == "P3"  # 4 x (400 + 100) = 2,000 >= 1,000 at confidence 0.4
    assert got["C3"] == "P4"  # top-dollar counted row, quantity >= 3
    assert "C4" not in got  # quantity below 3
    assert "C5" not in got  # schedule basis is deterministic
    gate = GateFinding(run_id="r", output_version=1, stage="pre", gate_id="census_floor", severity="block",
                       route="recount", target={"row_ids": ["C4"], "sheet_key": "D1P2"},
                       evidence={"floor": 5, "census_id": "c2", "family_specific": True})
    assert any(t.row_id == "C4" and t.priority == "P2" for t in rc.select_targets(rows_in, gate_findings=[gate]))


def test_caps_on_targets_and_sheets():
    rows_in = [_row(f"C{i}", 5, sheet=f"D1P{i}", page=i, coverage="tile_subtotal") for i in range(1, 11)]
    assert len(rc.select_targets(rows_in, max_targets=12, max_sheets=6)) == 6
    assert len(rc.select_targets(rows_in, max_targets=3, max_sheets=6)) == 3


# Groups --------------------------------------------------------------------------------


def test_request_recounts_inserts_groups_and_target_rows(counts7154):
    sb = FakeSB()
    queued = []
    targets = rc.select_targets(counts7154)
    run = {"id": "run-1", "settings_snapshot": {}}
    groups = rc.request_recounts(sb, run, targets, route=ROUTE, enqueue=lambda _sb, g, _r: queued.extend(g))
    assert len(groups) == 1 and queued == groups
    g = groups[0]
    assert (g["kind"], g["mode"], g["pass"], g["status"]) == ("recount", "read", 3, "queued")
    assert g["route"]["model"] == "gpt-6-astra"
    assert [t["row_id"] for t in g["loop_state"]["recount"]["targets"]] == ["C22", "C23"]
    recs = sb.tables["plans_reader_recounts"]
    assert {r["target_id"] for r in recs} == {"C22@D1P8", "C23@D1P8"}
    assert all(r["group_id"] == g["id"] for r in recs)
    assert all("review_id" not in r and "census_floor" not in r for r in recs)  # nulls omitted
    upsert = next(c for c in sb.calls if c[0] == "plans_reader_recounts")
    assert upsert[3] == "run_id,target_id"


def test_group_targets_splits_at_four_per_sheet():
    targets = rc.select_targets([_row(f"C{i}", 5, coverage="partial") for i in range(1, 7)], max_targets=12)
    assert [len(g) for g in rc.group_targets(targets)] == [4, 2]


# Tiles and ownership ---------------------------------------------------------------------


def test_tiles_fit_the_provider_and_partition_the_region():
    from app.services.llm_parts import image_spec_for

    for provider in ("anthropic", "openai"):
        tiles = rc.plan_tiles((0, 0, 1, 1), w_in=42, h_in=30, provider=provider)
        spec = image_spec_for(provider)
        assert all(spec.fits(*t.px) for t in tiles)
        area = sum((t.core[2] - t.core[0]) * (t.core[3] - t.core[1]) for t in tiles)
        assert math.isclose(area, 1.0, rel_tol=1e-6)
    assert 6 <= len(rc.plan_tiles((0, 0, 1, 1), w_in=42, h_in=30, provider="anthropic")) <= 9
    shifted = rc.plan_tiles(rc.DEFAULT_PLAN_REGION, w_in=42, h_in=30, provider="openai", offset=(0.5, 0.5))
    base = rc.plan_tiles(rc.DEFAULT_PLAN_REGION, w_in=42, h_in=30, provider="openai")
    assert {t.core for t in shifted} != {t.core for t in base}
    area = sum((t.core[2] - t.core[0]) * (t.core[3] - t.core[1]) for t in shifted)
    assert math.isclose(area, 0.78, rel_tol=1e-6)


def _local(tile: rc.Tile, px: float, py: float) -> tuple[float, float]:
    bx0, by0, bx1, by1 = tile.box
    return ((px - bx0) / (bx1 - bx0), (py - by0) / (by1 - by0))


def _marks_for(tiles: list[rc.Tile], points: list[tuple[float, float]]) -> list[dict]:
    """Every tile whose rendered box shows the point marks it (overlap
    duplicates included), the way a model sees overlapping tiles."""
    out = []
    for px, py in points:
        for t in tiles:
            if t.box[0] <= px <= t.box[2] and t.box[1] <= py <= t.box[3]:
                x, y = _local(t, px, py)
                out.append({"tile_id": t.tile_id, "x": x, "y": y, "confidence": 0.9})
    return out


def test_tile_ownership_removes_overlap_duplicates():
    region = (0.0, 0.0, 0.78, 1.0)
    tiles = rc.plan_tiles(region, w_in=42, h_in=30, provider="anthropic")
    boundary_x = tiles[0].core[2]
    points = [(boundary_x - 0.001, 0.2), (boundary_x + 0.001, 0.2), (0.1, 0.1)]
    marks = _marks_for(tiles, points)
    assert len(marks) > len(points)  # the boundary points appear in two tiles
    kept, dropped = rc.owned_marks(tiles, marks, region)
    assert len(kept) == len(points)
    assert dropped["outside_core"] == len(marks) - len(points)
    bad, dropped2 = rc.owned_marks(tiles, [{"tile_id": "T99", "x": 0.5, "y": 0.5}], region)
    assert bad == [] and dropped2["unknown_tile"] == 1


# Outcome rules ---------------------------------------------------------------------------


def test_confirmed_within_tolerance():
    assert rc.decide(builder_qty=20, recount_qty=21, unit_cost_usd=10.0, total_usd=None)[0] == "confirmed"


def test_raise_rules():
    small = rc.decide(builder_qty=5, recount_qty=17, unit_cost_usd=50.0, total_usd=20_000.0)
    assert small[:2] == ("raise", 17.0)  # impact 600 < max(1,000, 200)
    big = rc.decide(builder_qty=5, recount_qty=17, unit_cost_usd=500.0, total_usd=20_000.0)
    assert big[0] == "unresolved"  # impact 6,000 needs a second signal
    unpriced = rc.decide(builder_qty=5, recount_qty=17, unit_cost_usd=None, total_usd=None)
    assert unpriced[0] == "unresolved"
    census = {"count": 17, "family_specific": True}
    assert rc.decide(builder_qty=5, recount_qty=17, unit_cost_usd=500.0, total_usd=0, census=census)[0] == "raise"
    assert rc.decide(builder_qty=5, recount_qty=17, unit_cost_usd=500.0, total_usd=0, census=census,
                     used_text_search=True)[0] == "unresolved"  # census not independent
    ceiling = {"count": 9, "family_specific": False}
    out = rc.decide(builder_qty=5, recount_qty=17, unit_cost_usd=10.0, total_usd=0, census=ceiling)
    assert out[0] == "unresolved" and "ceiling" in out[3]
    assert rc.decide(builder_qty=5, recount_qty=17, unit_cost_usd=500.0, total_usd=0, tiebreak_qty=16)[0] == "raise"
    assert rc.decide(builder_qty=5, recount_qty=17, unit_cost_usd=500.0, total_usd=0, second_qty=17)[0] == "raise"
    assert rc.decide(builder_qty=5, recount_qty=17, unit_cost_usd=10.0, total_usd=0, tiebreak_qty=6)[0] == "unresolved"


def test_lower_needs_two_signals():
    one = rc.decide(builder_qty=20, recount_qty=12, unit_cost_usd=1.0, total_usd=0)
    assert one[0] == "unresolved" and "two signals" in one[3]
    assert rc.decide(builder_qty=20, recount_qty=12, unit_cost_usd=1.0, total_usd=0, tiebreak_qty=12)[:2] == ("lower", 12.0)
    assert rc.decide(builder_qty=20, recount_qty=12, unit_cost_usd=1.0, total_usd=0, second_qty=13)[0] == "lower"
    agree = {"count": 12, "family_specific": True}
    assert rc.decide(builder_qty=20, recount_qty=12, unit_cost_usd=1.0, total_usd=0, census=agree)[0] == "lower"
    floor = {"count": 18, "family_specific": True}
    out = rc.decide(builder_qty=20, recount_qty=12, unit_cost_usd=1.0, total_usd=0, census=floor, tiebreak_qty=12)
    assert out[0] == "unresolved" and "contradicts" in out[3]


# Scripted loop on ED3.01 -------------------------------------------------------------------


def _troffer_points(region, n: int) -> list[tuple[float, float]]:
    """n synthetic symbol centers spread over the region, several on tile
    boundaries so the overlap duplicates are exercised."""
    x0, y0, x1, y1 = region
    pts = []
    for i in range(n):
        fx = ((i * 0.37) % 1.0) * 0.9 + 0.05
        fy = ((i * 0.61) % 1.0) * 0.9 + 0.05
        pts.append((x0 + fx * (x1 - x0), y0 + fy * (y1 - y0)))
    return pts


def test_scripted_loop_recounts_17_troffers_on_ed301(counts7154):
    rect = _rect(counts7154)
    price = FakePriceService({rect["description"].strip().lower(): {
        "uom": "EA", "material": {"median": 20.0}, "labor_hours": {"median": 0.5}}})
    targets = [t for t in rc.select_targets(counts7154, price=price, labor_rate_usd=100.0) if t.row_id == rect["row_id"]]
    assert targets and targets[0].unit_cost_usd == 70.0
    sb = FakeSB()
    group = rc.request_recounts(sb, {"id": "run-7154"}, targets, route=ROUTE)[0]
    services = fake_services(
        legend=FakeLegendIndex([{"ref": "l:leg1:3", "tag": "(X)", "description": "Remove existing rectangular lighting fixture"}]),
        census=FakeCensusService({}),
    )
    request = rc.build_recount_request(group, page_size=(42.0, 30.0), services=services)
    assert request.region == rc.DEFAULT_PLAN_REGION and request.region_complete
    assert set(request.local_ids) == {"t1"}
    # Blinded: no builder quantity, no census number, no counting word reaches the model.
    prompt = request.user_text.lower()
    assert "quantity" not in prompt and "count of" not in prompt and "builder" not in prompt
    assert "t1: remove existing rectangular lighting fixture" in prompt
    assert all("5" not in t.symbol for t in request.targets)
    validate_strict(RECOUNT_SCHEMA)
    points = _troffer_points(request.region, ADJUDICATED_ED301_TROFFERS)
    raw_marks = _marks_for(request.tiles, points)
    assert len(raw_marks) >= ADJUDICATED_ED301_TROFFERS

    seen = {}

    def scripted(req: rc.RecountRequest) -> rc.RecountReply:
        seen["request"] = req
        return rc.RecountReply(parsed={"targets": [{"target_id": "t1", "marks": raw_marks, "uncertain_regions": [],
                                                    "symbol_seen_in_legend": True, "notes": ""}]},
                               cost_usd=0.0, model="scripted", provider="openai", tools_used=["zoom_region"])

    reply = rc.run_recount(request, scripted)
    assert seen["request"] is request
    outcomes = rc.resolve(request, reply, total_usd=10_000.0)
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.recount_qty == ADJUDICATED_ED301_TROFFERS
    assert (o.outcome, o.final_qty) == ("raise", 17.0)
    assert o.impact_usd == pytest.approx(12 * 70.0)
    out = rc.group_output(request, outcomes)
    row = out["counts"][0]
    assert row["quantity"] == 17 and row["supersedes_row_id"] == rect["row_id"]
    assert row["coverage"] == "whole_sheet" and len(row["points"]) == 17
    page = out["pages"][0]
    assert rows.infer_coverage("counts", row, group_kind="recount", page_item=page) == "sheet_total"
    assert rc.record_outcomes(sb, "run-7154", outcomes, group_id=group["id"]) == 1
    stored = next(r for r in sb.tables["plans_reader_recounts"] if r["target_id"] == targets[0].target_id)
    assert stored["outcome"] == "raise" and stored["recount_qty"] == 17
    assert "census_floor" not in stored and "tiebreak_qty" not in stored


def test_unresolved_then_tiebreak_settles_a_large_raise(counts7154):
    rect = _rect(counts7154)
    price = FakePriceService({rect["description"].strip().lower(): {
        "uom": "EA", "material": {"median": 900.0}, "labor_hours": {"median": 1.0}}})
    targets = [t for t in rc.select_targets(counts7154, price=price, labor_rate_usd=100.0) if t.row_id == rect["row_id"]]
    group = rc.request_recounts(FakeSB(), {"id": "run-7154"}, targets, route=ROUTE)[0]
    request = rc.build_recount_request(group, page_size=(42.0, 30.0))
    marks = _marks_for(request.tiles, _troffer_points(request.region, 17))
    reply = rc.RecountReply(parsed={"targets": [{"target_id": "t1", "marks": marks, "uncertain_regions": [],
                                                 "symbol_seen_in_legend": True, "notes": ""}]})
    first = rc.resolve(request, reply, total_usd=10_000.0)[0]
    assert first.outcome == "unresolved" and first.severity == "critical"
    assert rc.needs_followup(first) == "tiebreak"
    assert rc.count_rows(request, [first]) == []
    tb_req = rc.build_recount_request(group, page_size=(42.0, 30.0), offset=(0.5, 0.5), attempt="second_sample")
    tb_marks = _marks_for(tb_req.tiles, _troffer_points(request.region, 17))
    tb_reply = rc.RecountReply(parsed={"targets": [{"target_id": "t1", "marks": tb_marks, "uncertain_regions": [],
                                                    "symbol_seen_in_legend": True, "notes": ""}]})
    settled = rc.resolve(request, reply, total_usd=10_000.0, second_sample=(tb_req, tb_reply))[0]
    assert (settled.outcome, settled.final_qty) == ("raise", 17.0)
    assert "second_sample" in settled.signals


def test_toolloop_runner_builds_a_read_only_recount_loop(counts7154, monkeypatch):
    if importlib.util.find_spec("app.services.plans_reader_toolloop") is None:
        with pytest.raises(rc.RecountRuntimeUnavailable):
            rc.toolloop_runner(None, {"id": "r"}, {"id": "g"}, heartbeat=lambda: None)
        return
    from decimal import Decimal

    from app.services import plans_reader_toolloop as toolloop

    targets = [t for t in rc.select_targets(counts7154) if t.row_id == "C22"]
    group = rc.request_recounts(FakeSB(), {"id": "run-7154"}, targets, route=ROUTE)[0]
    request = rc.build_recount_request(group, page_size=(42.0, 30.0),
                                       render_tile=lambda _d, _p, _b, _ppi: b"png-bytes")
    captured = {}

    def scripted_run_loop(ctx):
        captured["ctx"] = ctx
        ctx.tool_ctx.call_counts["zoom_region"] = 2
        return toolloop.LoopResult(status="done", parsed={"targets": []}, text="{}", rounds=2,
                                   spent_usd=Decimal("0"), tool_calls=2, images=len(request.tiles),
                                   stop_reason="answer", resumed=False, model="scripted", provider="openai")

    monkeypatch.setattr(toolloop, "run_loop", scripted_run_loop)
    with pytest.raises(TypeError):
        rc.toolloop_runner(object(), {"id": "run-7154"}, group, store=object())  # heartbeat is required
    with pytest.raises(TypeError):
        rc.toolloop_runner(object(), {"id": "run-7154"}, group, store=object(), heartbeat=None)
    beats: list[int] = []
    runner = rc.toolloop_runner(object(), {"id": "run-7154"}, group, store=object(),
                                heartbeat=lambda: beats.append(1))
    reply = rc.run_recount(request, runner)
    ctx = captured["ctx"]
    ctx.tool_ctx.heartbeat()
    assert beats == [1]  # the loop's heartbeat is the caller's lease renewal, not a no-op
    names = [p.name for p in ctx.precalls]
    assert names.count("zoom_region") == 1 and names[-1] == "zoom_region"
    assert "legend_lookup" in names and "census" in names
    zoom = ctx.precalls[-1].args
    assert zoom["bbox"] == [round(v, 4) for v in request.region] and zoom["purpose"] == "count"
    census = next(p for p in ctx.precalls if p.name == "census").args
    assert census["document_id"] == request.document_id and census["page"] == request.page
    assert census["pattern"]["kind"] == "tag" and census["region"] == zoom["bbox"]
    import jsonschema

    from app.services.pagefacts import services as pf_services
    from app.services.plans_reader_tool_handlers import ZOOM_ARGS

    schemas = {"census": pf_services.CENSUS_ARGS, "legend_lookup": pf_services.LEGEND_LOOKUP_ARGS,
               "zoom_region": ZOOM_ARGS}
    kinds = {spec.name: spec.kinds for spec in pf_services.TOOL_SPECS}
    for pre in ctx.precalls:
        jsonschema.Draft202012Validator(schemas[pre.name]).validate(pre.args)  # strict args the loop accepts
        if pre.name in kinds:
            assert "recount" in kinds[pre.name], pre.name
    assert (ctx.kind, ctx.feature, ctx.step, ctx.schema_name) == ("recount", "plans_recount", "recount",
                                                                  rc.RECOUNT_SCHEMA_NAME)
    assert ctx.response_schema == RECOUNT_SCHEMA and ctx.group_id == group["id"]
    assert all(t.mode == "read" for t in ctx.tools)
    assert len(ctx.user_parts) == 1 + len(request.tiles)
    assert reply.tools_used == ["zoom_region"] and reply.model == "scripted"


def test_replay_stage_recount_is_registered_and_runs_at_zero(run7154):
    from types import SimpleNamespace

    from app.services import plans_reader_replay as replay

    rc.register_replay_stage()
    spec = replay.stage("recount")
    assert spec.owner == "WS7" and not spec.legacy
    notes: list[str] = []
    ctx = SimpleNamespace(note=notes.append)
    art = {"category:electrical_drawings": run7154["electrical"], "groups": []}
    with replay.no_provider_calls():
        out = spec.fn(ctx, art)
    assert out["recount"]["stored_groups"] == 0
    assert [t["row_id"] for t in out["recount"]["targets"]] == ["C22", "C23"]
    assert notes and "no stored recount groups" in notes[0]


def test_ws4_floors_and_regions_on_the_7154_page_facts(counts7154):
    pytest.importorskip("app.services.plans_reader_floors")
    pytest.importorskip("app.services.plans_reader_regions")
    import json

    from app.services import plans_reader_floors as pf

    from tests.plans_reader_fixtures import FIXTURE_7154

    facts = json.loads((FIXTURE_7154 / "page_facts_elec.json").read_text())["documents"][0]["facts"]
    floors = pf.compute_floors([(ELEC_DOC_7154, p["page"], p) for p in facts["pages"]],
                               doc_numbers={ELEC_DOC_7154: 1})
    targets = rc.select_targets(counts7154, floors=floors)
    by_row = {t.row_id: t for t in targets}
    assert by_row["C22"].priority == "P1" and by_row["C22"].family_key == "demo_x"
    for t in targets:
        if t.priority == "P2":
            assert t.family_key and t.census is not None
            if t.family_key.startswith("fixture_mark"):
                assert t.census["family_specific"] is False  # weak family: a ceiling, never a raise signal
    box, complete, source = rc.code_region([by_row["C22"]], floors=floors)
    assert source == "for_recount" and complete
    assert 0.0 <= box[0] < box[2] <= 1.0 and 0.0 <= box[1] < box[3] <= 1.0


# Second sample at a different tile offset (fix group Y4, run 51ec302b) -----------------------


def _two_target_group(counts7154, *, unit_material: float):
    """C22 (rectangular, builder 5, unresolved when priced high) and C23
    (builder count confirmed) on ED3.01 in one recount group."""
    price = FakePriceService({r["description"].strip().lower(): {
        "uom": "EA", "material": {"median": unit_material}, "labor_hours": {"median": 1.0}}
        for r in counts7154 if r["row_id"] in ("C22", "C23")})
    targets = [t for t in rc.select_targets(counts7154, price=price, labor_rate_usd=100.0)
               if t.row_id in ("C22", "C23")]
    assert [t.row_id for t in targets] == ["C22", "C23"]
    group = rc.request_recounts(FakeSB(), {"id": "run-7154"}, targets, route=ROUTE)[0]
    return rc.build_recount_request(group, page_size=(42.0, 30.0)), group


def _scripted_samples(points_by_attempt: dict[str, dict[str, int]], *, cost: float = 0.0):
    """A runner that marks n synthetic symbols per target over whatever
    tiles the request carries (overlap duplicates included), so tile
    ownership has to dedupe them on both grids. Records every request."""
    seen: list[rc.RecountRequest] = []

    def runner(req: rc.RecountRequest) -> rc.RecountReply:
        seen.append(req)
        counts = points_by_attempt[req.attempt]
        items = []
        for local, tid in req.local_ids.items():
            row = tid.split("@", 1)[0]
            pts = _troffer_points(req.region, counts[row])
            items.append({"target_id": local, "marks": _marks_for(req.tiles, pts), "uncertain_regions": [],
                          "symbol_seen_in_legend": True, "notes": ""})
        return rc.RecountReply(parsed={"targets": items}, cost_usd=cost, model="scripted", provider="openai")

    return runner, seen


def test_unresolved_then_second_sample_agrees_gives_raise(counts7154):
    request, group = _two_target_group(counts7154, unit_material=900.0)
    c23 = next(t for t in request.targets if t.row_id == "C23")
    runner, seen = _scripted_samples({
        "primary": {"C22": 17, "C23": int(c23.builder_qty)},
        rc.SECOND_SAMPLE_ATTEMPT: {"C22": 17, "C23": int(c23.builder_qty)},
    }, cost=0.25)
    done = rc.run_group(request, runner, total_usd=10_000.0, followups_used=0)
    assert [r.attempt for r in seen] == ["primary", rc.SECOND_SAMPLE_ATTEMPT]
    second = seen[1]
    # Only the unresolved target is sampled again, blinded, on a shifted grid.
    assert [t.row_id for t in second.targets] == ["C22"] and set(second.local_ids) == {"t1"}
    assert second.offset == rc.SECOND_SAMPLE_OFFSET and second.region == request.region
    assert {t.core for t in second.tiles} != {t.core for t in request.tiles}
    assert not re.search(r"\b5\b|17", second.user_text)
    assert rc.LOOP_ATTEMPTS[second.attempt] == 2 and rc.LOOP_ATTEMPTS[request.attempt] is None
    # The second loop gets what the group budget has left after the primary.
    budget = rc.group_budget_usd(request)
    assert second.route["budget_usd"] == pytest.approx(budget - 0.25, abs=1e-4)
    by_row = {o.row_id: o for o in done.outcomes}
    assert (by_row["C22"].outcome, by_row["C22"].final_qty) == ("raise", 17.0)
    assert "second_sample" in by_row["C22"].signals and by_row["C22"].tiebreak_qty == 17
    assert by_row["C23"].outcome == "confirmed"
    assert done.second_sample is not None and done.cost_usd == pytest.approx(0.5)
    out = rc.group_output(request, done.outcomes, second_sample=done.second_sample, followup=done.followup)
    assert [r["supersedes_row_id"] for r in out["counts"]] == ["C22", "C23"]
    assert out["counts"][0]["quantity"] == 17.0
    info = out["recount"]["second_sample"]
    assert info["targets"] == [by_row["C22"].target_id] and info["counts"] == {by_row["C22"].target_id: 17}
    sb = FakeSB()
    rc.record_outcomes(sb, "run-7154", done.outcomes, group_id=group["id"])
    stored = next(r for r in sb.tables["plans_reader_recounts"] if r["row_id"] == "C22")
    assert stored["outcome"] == "raise" and stored["tiebreak_qty"] == 17


def test_second_sample_that_disagrees_stays_unresolved(counts7154):
    request, _group = _two_target_group(counts7154, unit_material=900.0)
    c23 = next(t for t in request.targets if t.row_id == "C23")
    runner, seen = _scripted_samples({
        "primary": {"C22": 17, "C23": int(c23.builder_qty)},
        rc.SECOND_SAMPLE_ATTEMPT: {"C22": 9},
    })
    done = rc.run_group(request, runner, total_usd=10_000.0)
    assert len(seen) == 2
    o = next(x for x in done.outcomes if x.row_id == "C22")
    assert o.outcome == "unresolved" and "second_sample disagrees" in o.reason and o.final_qty is None
    assert o.tiebreak_qty == 9 and o.severity == "critical"
    assert [r["supersedes_row_id"] for r in rc.count_rows(request, done.outcomes)] == ["C23"]
    # A target the second answer leaves out is no sample (not a count of 0).
    empty, _ = _scripted_samples({"primary": {"C22": 17, "C23": int(c23.builder_qty)}})
    blank = rc.second_sample_request(request, [o.target_id])
    res = rc.resolve(request, empty(request), total_usd=10_000.0,
                     second_sample=(blank, rc.RecountReply(parsed={"targets": []})))
    c22 = next(x for x in res if x.row_id == "C22")
    assert c22.outcome == "unresolved" and "raise needs a second signal" in c22.reason and c22.tiebreak_qty is None


def test_second_samples_are_capped_at_two_per_run_and_by_the_group_budget(counts7154):
    request, _group = _two_target_group(counts7154, unit_material=900.0)
    c23 = next(t for t in request.targets if t.row_id == "C23")
    plan = {"primary": {"C22": 17, "C23": int(c23.builder_qty)}, rc.SECOND_SAMPLE_ATTEMPT: {"C22": 17}}
    assert rc.MAX_FOLLOWUPS_PER_RUN == 2
    used = 0
    results = []
    for _group_no in range(3):  # three unresolved groups in one run
        runner, seen = _scripted_samples(plan)
        done = rc.run_group(request, runner, total_usd=10_000.0, followups_used=used)
        used += 1 if done.second_sample is not None else 0
        results.append((len(seen), next(o.outcome for o in done.outcomes if o.row_id == "C22"), done.followup))
    assert [r[0] for r in results] == [2, 2, 1]
    assert [r[1] for r in results] == ["raise", "raise", "unresolved"]
    assert results[2][2].startswith("skipped: the run already used 2 of 2")
    # The group budget must cover another loop of the primary's cost.
    budget = rc.group_budget_usd(request)
    runner, seen = _scripted_samples(plan, cost=budget * 0.6)
    poor = rc.run_group(request, runner, total_usd=10_000.0)
    assert len(seen) == 1 and poor.second_sample is None and poor.followup.startswith("skipped: budget")
    # Nothing unresolved: no second loop at all.
    runner, seen = _scripted_samples({"primary": {"C22": 5, "C23": int(c23.builder_qty)}})
    calm = rc.run_group(request, runner, total_usd=10_000.0)
    assert len(seen) == 1 and calm.followup == "not needed"


def test_followups_used_in_run_counts_groups_with_attempt_rounds():
    from types import SimpleNamespace

    from app.services.plans_reader_loop_store import ATTEMPT_ROUND_BASE

    rows = [{"run_id": "r", "group_id": "g1", "round_no": 1}, {"run_id": "r", "group_id": "g1", "round_no": ATTEMPT_ROUND_BASE + 1},
            {"run_id": "r", "group_id": "g2", "round_no": ATTEMPT_ROUND_BASE}, {"run_id": "r", "group_id": "g3", "round_no": 2},
            {"run_id": "other", "group_id": "g9", "round_no": ATTEMPT_ROUND_BASE + 2}]

    class Q:
        def __init__(self):
            self.f = []

        def select(self, *_a):
            return self

        def eq(self, c, v):
            self.f.append(lambda r, c=c, v=v: r.get(c) == v)
            return self

        def gte(self, c, v):
            self.f.append(lambda r, c=c, v=v: r.get(c) >= v)
            return self

        def execute(self):
            return SimpleNamespace(data=[r for r in rows if all(f(r) for f in self.f)])

    sb = SimpleNamespace(table=lambda _name: Q())
    assert rc.followups_used_in_run(sb, "r") == 2
    assert rc.followups_used_in_run(sb, "r", exclude_group_id="g1") == 1
    broken = SimpleNamespace(table=lambda _name: (_ for _ in ()).throw(RuntimeError("down")))
    assert rc.followups_used_in_run(broken, "r") == rc.MAX_FOLLOWUPS_PER_RUN


def test_census_that_found_nothing_is_unknown_not_a_ceiling():
    # Run 51ec302b: ED3.01 track heads, builder 8, blind recount 12, census of
    # "(X), keynote 1" found no text occurrence (count 0, "unknown, not 0").
    nothing = {"count": 0, "family_specific": False, "note": "the count is unknown, not 0"}
    assert rc.census_known_count(nothing) is None and rc.census_known_count({"count": 0, "error": "x"}) is None
    out = rc.decide(builder_qty=8, recount_qty=12, unit_cost_usd=None, total_usd=None, census=nothing)
    assert out[0] == "unresolved" and "ceiling" not in out[3] and "second signal" in out[3]
    assert rc.needs_followup(rc.RecountOutcome(
        target_id="C22@D2P8", row_id="C22", document_id="d", page=8, priority="P1", builder_qty=8, recount_qty=12,
        census_floor=None, tiebreak_qty=None, mean_conf=0.98, outcome=out[0], reason=out[3], final_qty=None,
        signals=out[2], marks=[], impact_usd=None)) == "tiebreak"
    assert rc.decide(builder_qty=8, recount_qty=12, unit_cost_usd=None, total_usd=None, census=nothing,
                     second_qty=12)[:2] == ("raise", 12.0)
    assert rc.census_known_count({"count": 9, "family_specific": False}) == 9  # a real reading stays a ceiling


def test_linear_rows_are_never_recount_targets():
    # Run 51ec302b: the L5 undercabinet segments (6.7 FT, 5 FT, needs_recount)
    # were recounted as one mark each and ended "a decrease needs two signals".
    rows_in = [_row("C52", 6.7, unit="FT", flags=["needs_recount"], basis="inferred", desc="L5 undercabinet LED"),
               _row("C22", 8, coverage="tile_subtotal", desc="Remove existing track-light heads")]
    assert [t.row_id for t in rc.select_targets(rows_in)] == ["C22"]


def test_toolloop_runner_runs_the_second_sample_under_its_own_loop_key(counts7154, monkeypatch):
    from decimal import Decimal

    from app.services import plans_reader_toolloop as toolloop

    request, group = _two_target_group(counts7154, unit_material=900.0)
    captured: list = []

    def scripted_run_loop(ctx):
        captured.append(ctx)
        return toolloop.LoopResult(status="done", parsed={"targets": []}, text="{}", rounds=1,
                                   spent_usd=Decimal("0"), tool_calls=0, images=0, stop_reason="answer",
                                   resumed=False, model="scripted", provider="openai")

    monkeypatch.setattr(toolloop, "run_loop", scripted_run_loop)
    runner = rc.toolloop_runner(object(), {"id": "run-7154"}, group, store=object(), heartbeat=lambda: None)
    rc.run_recount(request, runner)
    rc.run_recount(rc.second_sample_request(request, [request.targets[0].target_id], budget_usd=1.0), runner)
    primary, second = captured
    assert primary.loop_attempt is None and primary.key == f"group:{group['id']}"
    assert second.loop_attempt == 2 and second.key == f"group:{group['id']}#a2"
    assert second.group_id == primary.group_id == group["id"]
    assert second.route["budget_usd"] == 1.0

"""plans_reader_lines: code computes BOQ line v2 quantities from cited rows
(build spec 5.4, 5.8 add_boq_line, WS4). Synthetic rows only (no real
pricing, no real quantities beyond the committed 7154 fixture rows)."""

from __future__ import annotations

import random

import pytest

from app.services import plans_reader_lines as lines
from app.services import plans_reader_rows as rows_mod
from app.services.estimating_contracts import Fact, FactCite
from app.services.plans_reader_review_schemas import REFUSAL_CODES
from tests.plans_reader_fixtures import load_7154, rows_meta


def _sheet(n: str, key: str = "D1P1") -> dict:
    return {"document_id": "d1", "page": 1, "sheet_number": n, "sheet_key": key}


def _row(**kw) -> dict:
    row = {"status": "active", "coverage": "sheet_total", "flags": [], "superseded": [], "alternates": []}
    row.update(kw)
    return row


ROWS: dict[str, dict] = {
    "C1": _row(row_id="C1", description="Duplex receptacle", quantity=12, unit="EA", basis="counted_symbols", sheet=_sheet("E2.01")),
    "C2": _row(row_id="C2", description="Duplex receptacle", quantity=8, unit="EA", basis="counted_symbols", sheet=_sheet("E10.01")),
    "C3": _row(row_id="C3", description="Remove rectangular fixture", quantity=5, unit="EA", basis="counted_symbols",
               coverage="tile_subtotal", flags=["needs_recount"], sheet=_sheet("ED3.01")),
    "C4": _row(row_id="C4", description="Old row", quantity=3, unit="EA", status="superseded", sheet=_sheet("E2.01")),
    "C5": _row(row_id="C5", description="Bad row", quantity=3, unit="EA", status="unverifiable", sheet=_sheet("E2.01")),
    "C6": _row(row_id="C6", description="Lighting track", quantity=40, unit="FT", basis="stated_quantity", sheet=_sheet("E3.01")),
    "C7": _row(row_id="C7", description="L1 troffer", quantity=21, unit="EA", basis="schedule_text", sheet=_sheet("E0.04")),
    "C8": _row(row_id="C8", description="Remove lighting track row", quantity=3, unit="EA", basis="counted_symbols", sheet=_sheet("ED3.01")),
    "C9": _row(row_id="C9", description="Duplex receptacle", quantity=2, unit="EA", basis="inferred", sheet=_sheet("E2.02")),
    "R1": _row(row_id="R1", description="Branch run", length_ft=100, conductors="2#12, 1#12G", basis="scaled_from_drawing", sheet=_sheet("E2.01")),
    "R2": _row(row_id="R2", description="Feeder", length_ft=50, conductors="4#500 KCMIL CU + 1#2/0 CU G", basis="stated", sheet=_sheet("E0.02")),
    "R3": _row(row_id="R3", description="Homerun", length_ft=30, conductors="2#10, 1#10G", basis="assumed", sheet=_sheet("E2.01")),
    "M1": _row(row_id="M1", description="#12 THHN/THWN Cu", quantity=120, unit="FT", basis="measured_route", sheet=_sheet("E2.01")),
    "M2": _row(row_id="M2", description="3/4\"C EMT", quantity=80, unit="FT", basis="assumed_route_untagged", sheet=_sheet("E2.02")),
    "S1": _row(row_id="S1", name="LUMINAIRE SCHEDULE", columns=["TYPE", "QTY", "WATTS"],
               rows=[["L1", "21", "40"], ["L2", "1,200", "20"], ["L3", "--", "10"]], coverage="schedule", sheet=_sheet("E0.04")),
    "P1": _row(row_id="P1", item="Receptacles", section="26 27 26", coverage="stated", sheet=_sheet("26 27 26")),
}


def _line(**kw) -> dict:
    line = {
        "area": "MAIN",
        "section": "NEW WORK",
        "group": "OTHER ITEMS",
        "subgroup": "RECEPTACLES (RECEPTACLES INCLUDES 4\"S DEEP COMB BOX AND COVER PLATE)",
        "description": "DUPLEX RECEPTACLE",
        "unit": "EA",
        "source_refs": [{"ref": "C1", "multiplier": 1}],
        "combine": "sum",
        "caveat": None,
        "material_note": None,
        "material_category": "General Material",
    }
    line.update(kw)
    return line


def _wire(**kw) -> dict:
    base = dict(section="NEW WORK", group="WIRING", subgroup="CONDUCTORS (AWG)",
                description="#12 THHN/THWN Cu", unit="FT", combine="conductor_footage",
                source_refs=[{"ref": "R1", "multiplier": 1}])
    base.update(kw)
    return _line(**base)


def _ok(result) -> lines.ComputedLine:
    assert isinstance(result, lines.ComputedLine), result
    return result


def _refused(result, code: str) -> lines.LineError:
    assert isinstance(result, lines.LineError), result
    assert result.code == code, (result.code, result.message)
    return result


# Combine modes -------------------------------------------------------------------------------


def test_sum_and_sheet_refs_and_basis():
    got = _ok(lines.compute(_line(source_refs=[{"ref": "C1", "multiplier": 1}, {"ref": "C2", "multiplier": 2}]), ROWS))
    assert got.quantity == 28
    assert got.sheet_refs == ["E2.01", "E10.01"]  # natural order, distinct
    assert got.basis == "counted" and got.wastage_applies is False and got.needs_recount is False
    assert got.consumes == [("C1", "count"), ("C2", "count")]
    res = got.as_result("B4")
    assert {"line_id", "quantity", "wastage_applies", "sheet_refs", "basis", "warnings"} <= set(res)


def test_max_single_floor():
    refs = [{"ref": "C1", "multiplier": 1}, {"ref": "C2", "multiplier": 1}]
    assert _ok(lines.compute(_line(source_refs=refs, combine="max"), ROWS)).quantity == 12
    assert _ok(lines.compute(_line(combine="single"), ROWS)).quantity == 12
    _refused(lines.compute(_line(source_refs=refs, combine="single"), ROWS), lines.INVALID_ARGS)
    demo = _line(section="DEMOLITION", group="DEMOLITION", subgroup="", description="REMOVE RECTANGULAR LIGHT FIXTURE",
                 source_refs=[{"ref": "C3", "multiplier": 1}], combine="floor", material_category="Demolition")
    got = _ok(lines.compute(demo, ROWS))
    assert got.quantity == 5 and got.basis == "census_floor" and got.needs_recount is True


def test_schedule_value():
    line = _line(group="LIGHTING FIXTURES", subgroup="FIXTURES", description="L1, 2X4 RECESSED LED TROFFER",
                 combine="schedule_value", source_refs=[{"ref": "S1#r1:QTY", "multiplier": 1}], material_category="Lighting")
    got = _ok(lines.compute(line, ROWS))
    assert got.quantity == 21 and got.basis == "schedule" and got.sheet_refs == ["E0.04"]
    assert got.consumes == [("S1#r1:QTY", "cell")]
    line["source_refs"] = [{"ref": "S1#r2:c2", "multiplier": 1}]
    assert _ok(lines.compute(line, ROWS)).quantity == 1200
    line["source_refs"] = [{"ref": "S1#r3:QTY", "multiplier": 1}]
    assert _refused(lines.compute(line, ROWS), lines.UNKNOWN_REF).detail["reason"] == "no_number"
    line["source_refs"] = [{"ref": "S1#r9:QTY", "multiplier": 1}]
    _refused(lines.compute(line, ROWS), lines.UNKNOWN_REF)
    line["source_refs"] = [{"ref": "C7", "multiplier": 1}]
    _refused(lines.compute(line, ROWS), lines.INVALID_ARGS)


def test_conductor_footage_counts_sizes_and_grounds():
    got = _ok(lines.compute(_wire(), ROWS))
    assert got.quantity == 200 and got.wastage_applies is True and got.basis == "measured"
    assert got.consumes == [("R1", "conductor:#12")]
    ground = _ok(lines.compute(_wire(description="#12 THHN/THWN Cu GROUND"), ROWS))
    assert ground.quantity == 100 and ground.consumes == [("R1", "conductor:#12 G")]
    feeder = _ok(lines.compute(_wire(description="#500 KCMIL THHN/THWN Cu", source_refs=[{"ref": "R2", "multiplier": 1}]), ROWS))
    assert feeder.quantity == 200 and feeder.basis == "stated"
    _refused(lines.compute(_wire(description="#10 THHN/THWN Cu"), ROWS), lines.UNIT_MISMATCH)
    _refused(lines.compute(_wire(description="THHN wire"), ROWS), lines.INVALID_ARGS)
    _refused(lines.compute(_wire(source_refs=[{"ref": "C1", "multiplier": 1}]), ROWS), lines.UNIT_MISMATCH)


def test_assumed_route_forces_the_assumed_caveat():
    got = _ok(lines.compute(_wire(description="#10 THHN/THWN Cu", source_refs=[{"ref": "R3", "multiplier": 1}],
                                  caveat="(NEED TO CONFIRM DETAILS)"), ROWS))
    assert got.basis == "assumed_route" and got.caveat == "(ASSUMED)" and got.quantity == 60
    assert any("replaced" in w for w in got.warnings)
    conduit = _line(group="WIRING", subgroup="CONDUITS", description="3/4\"C EMT", unit="FT",
                    source_refs=[{"ref": "M2", "multiplier": 1}, {"ref": "M1", "multiplier": 1}])
    # M1 is a measured #12 row: unit FT, so sum of M rows is allowed; bases mix
    got = _ok(lines.compute(conduit, ROWS))
    assert got.basis == "mixed" and got.caveat == "(ASSUMED)" and got.quantity == 200
    assert got.consumes == [("M2", "conduit"), ("M1", "conduit")]


def test_manual_needs_a_user():
    line = _line(combine="manual", source_refs=[], quantity=7)
    _refused(lines.compute(line, ROWS), lines.INVALID_ARGS)
    got = _ok(lines.compute(line, ROWS, user_id="u1"))
    assert got.quantity == 7 and got.basis == "manual"


# Refusals (5.8 add_boq_line) --------------------------------------------------------------------


def test_every_add_boq_line_refusal_code_is_reachable():
    reached = set()
    reached.add(_refused(lines.compute(_line(source_refs=[{"ref": "C99", "multiplier": 1}]), ROWS), lines.UNKNOWN_REF).code)
    reached.add(_refused(lines.compute(_line(source_refs=[{"ref": "t:abcd1234:1:2", "multiplier": 1}]), ROWS), lines.UNKNOWN_REF).code)
    reached.add(_refused(lines.compute(_line(source_refs=[{"ref": "C5", "multiplier": 1}]), ROWS), lines.UNVERIFIABLE_REF).code)
    reached.add(_refused(lines.compute(_line(source_refs=[{"ref": "C4", "multiplier": 1}]), ROWS), lines.SUPERSEDED_REF).code)
    reached.add(_refused(lines.compute(_line(unit="FT"), ROWS), lines.UNIT_MISMATCH).code)
    consumed = {("C1", "count"): "B3"}
    err = _refused(lines.compute(_line(), ROWS, consumed=consumed), lines.REF_CONSUMED)
    assert err.detail["line_id"] == "B3" and "B3" in err.message
    reached.add(err.code)
    err = _refused(lines.compute(_line(source_refs=[{"ref": "C3", "multiplier": 1}]), ROWS), lines.PARTIAL_REQUIRES_FLOOR)
    reached.add(err.code)
    fact = Fact(id="F1", system="fire_alarm", statement="Fire detection by Honeywell", party="Honeywell", verified=True,
                cite=FactCite(document_id="d9", page=2, quote="Fire detection systems are provided by Honeywell."),
                kind="provided_by_others")
    fa = _line(subgroup="FIRE ALARM ROUGHINS", description="SMOKE DETECTOR")
    err = _refused(lines.compute(fa, ROWS, excluded_by_fact=lambda line: fact), lines.EXCLUDED_BY_FACT)
    assert err.detail["fact_id"] == "F1" and "Honeywell" in err.message
    reached.add(err.code)
    reached.add(_refused(lines.compute(_line(group="NOT A GROUP"), ROWS), lines.VOCAB).code)
    conduit = _line(group="WIRING", subgroup="CONDUITS", description="3/4\" EMT", unit="FT",
                    source_refs=[{"ref": "M2", "multiplier": 1}])
    err = _refused(lines.compute(conduit, ROWS), lines.CONDUIT_FORM)
    assert err.detail["suggestion"] == '3/4"C EMT'
    reached.add(err.code)
    # luminaire_tag is raised by the consolidator's write-time conform_line
    # (plans_reader_consolidator), not by lines.compute.
    assert reached == set(REFUSAL_CODES["add_boq_line"]) - {"luminaire_tag"}
    assert err.as_refusal()["refused"] is True and err.as_refusal()["code"] == "conduit_form"


def test_excluded_by_fact_only_for_verified_facts_and_never_raceway():
    unverified = Fact(id="F2", system="fire_alarm", statement="x", party="others", verified=False,
                      cite=FactCite(document_id="d9", page=1, quote="q"), kind="provided_by_others")
    fa = _line(subgroup="FIRE ALARM ROUGHINS", description="SMOKE DETECTOR")
    _ok(lines.compute(fa, ROWS, excluded_by_fact=lambda line: unverified))
    _ok(lines.compute(fa, ROWS, excluded_by_fact=lambda line: {"verified": False, "system": "fire_alarm"}))
    verified = {"verified": True, "id": "F3", "system": "fire_alarm", "party": "others", "quote": "FA by others"}
    _refused(lines.compute(fa, ROWS, excluded_by_fact=lambda line: verified), lines.EXCLUDED_BY_FACT)
    rough = _line(subgroup="FIRE ALARM ROUGHINS", description="FIRE ALARM ROUGH-IN WITH BACKBOX")
    _ok(lines.compute(rough, ROWS, excluded_by_fact=lambda line: verified))
    conduit = _line(group="WIRING", subgroup="CONDUITS", description="3/4\"C EMT", unit="FT",
                    source_refs=[{"ref": "M2", "multiplier": 1}])
    _ok(lines.compute(conduit, ROWS, excluded_by_fact=lambda line: verified))


def test_unit_and_shape_refusals():
    _refused(lines.compute(_line(unit="LS"), ROWS), lines.UNIT_MISMATCH)
    _refused(lines.compute(_line(unit="KG"), ROWS), lines.UNIT_MISMATCH)
    _refused(lines.compute(_line(source_refs=[{"ref": "R1", "multiplier": 1}]), ROWS), lines.UNIT_MISMATCH)
    _refused(lines.compute(_line(source_refs=[{"ref": "P1", "multiplier": 1}]), ROWS), lines.UNIT_MISMATCH)
    _refused(lines.compute(_line(source_refs=[{"ref": "C1", "multiplier": 1.5}]), ROWS), lines.INVALID_ARGS)
    _refused(lines.compute(_line(source_refs=[{"ref": "C1", "multiplier": 0}]), ROWS), lines.INVALID_ARGS)
    _refused(lines.compute(_line(source_refs=[{"ref": "C1", "multiplier": 101}]), ROWS), lines.INVALID_ARGS)
    _refused(lines.compute(_line(source_refs=[{"ref": "C1", "multiplier": 1}, {"ref": "C1", "multiplier": 1}]), ROWS), lines.INVALID_ARGS)
    _refused(lines.compute(_line(source_refs=[]), ROWS), lines.INVALID_ARGS)
    _refused(lines.compute(_line(combine="average"), ROWS), lines.INVALID_ARGS)
    _refused(lines.compute(_line(description=""), ROWS), lines.INVALID_ARGS)
    ft = _ok(lines.compute(_line(group="LIGHTING FIXTURES", subgroup="FIXTURES", description="LIGHTING TRACK", unit="FT",
                                 source_refs=[{"ref": "C6", "multiplier": 1.5}]), ROWS))
    assert ft.quantity == 60 and ft.basis == "stated"


def test_vocabulary():
    assert lines.vocab_error(_line()) is None
    assert lines.vocab_error(_line(subgroup="RECEPTACLES")) is None
    assert lines.vocab_error(_line(subgroup="")) is None
    assert lines.vocab_error(_line(subgroup="CONDUITS")).code == "vocab"
    assert lines.vocab_error(_line(group="WIRING", subgroup="OTHER CONDUIT ACCESSORIES")).code == "vocab"
    assert lines.vocab_error(_line(group="WIRING", subgroup="TRENCH AND UNDERGROUND CONDUITS")) is None
    assert lines.vocab_error(_line(group="SOLAR WORK", subgroup="ANY NAME")) is None
    assert lines.vocab_error(_line(section="DEMOLITION", group="DEMOLITION", subgroup="")) is None
    assert lines.vocab_error(_line(section="DEMOLITION", group="WIRING")).code == "vocab"
    assert lines.vocab_error(_line(section="ALTERNATE")).code == "vocab"
    assert lines.vocab_error(_line(material_category="Groceries")).code == "vocab"


def test_conduit_form():
    ok = _line(group="WIRING", subgroup="CONDUITS", description="1-1/4\"C EMT", unit="FT", source_refs=[{"ref": "M2", "multiplier": 1}])
    assert _ok(lines.compute(ok, ROWS)).wastage_applies is True
    flex = dict(ok, description="1/2\" FLEX")
    _ok(lines.compute(flex, ROWS))  # not a type the renderer fits, so no form rule
    pvc = dict(ok, description="2\" PVC SCH 40")
    assert _refused(lines.compute(pvc, ROWS), lines.CONDUIT_FORM).detail["suggestion"] == '2"C PVC SCH 40'


def test_wastage_rules():
    def w(desc, unit="FT"):
        return lines._wastage(unit, lines.parse_item(desc, unit))

    assert w("#12 THHN/THWN Cu") is True
    assert w("3/4\"C EMT") is True
    assert w("MC CABLE 12/2") is True
    assert w("#6 BARE Cu GROUND") is False
    assert w("#12 THHN/THWN Cu", "EA") is False
    assert w("DUPLEX RECEPTACLE", "EA") is False
    assert w("SAW CUT") is False


def test_linear_class_in_ea_refused_and_linear_luminaire_warned():
    # spec 5.1: a linear class is always FT; an EA line of one is refused
    demo = _line(section="DEMOLITION", group="DEMOLITION", subgroup="", description="REMOVE LIGHTING TRACK ROW",
                 source_refs=[{"ref": "C8", "multiplier": 1}], material_category="Demolition")
    err = _refused(lines.compute(demo, ROWS), lines.UNIT_MISMATCH)
    assert err.detail == {"unit": "EA", "item_class": "demolition"}
    track = _line(group="LIGHTING FIXTURES", subgroup="FIXTURES", description="LIGHTING TRACK",
                  source_refs=[{"ref": "C8", "multiplier": 1}])
    _refused(lines.compute(track, ROWS), lines.UNIT_MISMATCH)
    _refused(lines.compute(track, ROWS, write_checks=False), lines.UNIT_MISMATCH)
    # linear luminaires are FT only on evidence, so EA is allowed with a warning
    strip_rows = dict(ROWS, C10=_row(row_id="C10", description="LED strip light", quantity=6, unit="EA",
                                     basis="counted_symbols", sheet=_sheet("E3.01")))
    strip = _ok(lines.compute(_line(group="LIGHTING FIXTURES", subgroup="FIXTURES", description="LED STRIP LIGHT",
                                    source_refs=[{"ref": "C10", "multiplier": 1}]), strip_rows))
    assert strip.quantity == 6 and any(w.startswith("linear_in_ea") for w in strip.warnings)
    duplex = _ok(lines.compute(_line(), ROWS))
    assert not any(w.startswith("linear_in_ea") for w in duplex.warnings)


def test_warnings_inferred():
    inferred = _ok(lines.compute(_line(source_refs=[{"ref": "C9", "multiplier": 1}]), ROWS))
    assert any("inferred" in w for w in inferred.warnings)


# parse_conductors --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2#12, 1#12G", {"#12": 2, "#12 G": 1}),
        ("4#500 KCMIL CU + 1#2/0 CU G", {"#500 KCMIL": 4, "#2/0 G": 1}),
        ("3#10 & 1#10 GND", {"#10": 3, "#10 G": 1}),
        ("(2) #12, (1) #12 GROUND", {"#12": 2, "#12 G": 1}),
        ("3/4\"C EMT W/ 2#12, 1#12G", {"#12": 2, "#12 G": 1}),
        ("4-500KCMIL, 1#3 G", {"#500 KCMIL": 4, "#3 G": 1}),
        ("3 #10 THHN, #10 GRN GND", {"#10": 3, "#10 G": 1}),
        ("#12", {"#12": 1}),
        ("2#12 THHN AND 2#10 THHN", {"#12": 2, "#10": 2}),
        ("", {}),
        (None, {}),
        ("1/2\" conduit only", {}),
    ],
)
def test_parse_conductors(text, expected):
    assert lines.parse_conductors(text) == expected


def test_line_conductor():
    assert lines.line_conductor("#12 THHN/THWN Cu") == "#12"
    assert lines.line_conductor("#4/0 XHHW-2 Al") == "#4/0"
    assert lines.line_conductor("500 KCMIL CU") == "#500 KCMIL"
    assert lines.line_conductor("#6 BARE Cu GROUND") == "#6 G"
    assert lines.line_conductor("3/4\"C EMT") is None


def test_run_conductors_prefers_ws3_parsed():
    assert lines.run_conductors({"conductors_parsed": {"#10": 3}, "conductors": "2#12"}) == {"#10": 3}
    assert lines.run_conductors({"conductors": "2#12"}) == {"#12": 2}


# LineBook: consumption --------------------------------------------------------------------------


def test_linebook_consumption_add_update_remove():
    book = lines.LineBook(ROWS)
    first = book.add(_line())
    assert first["line_id"] == "B1" and first["quantity"] == 12
    second = book.add(_line(description="DUPLEX RECEPTACLE, GFCI"))
    assert second["refused"] is True and second["code"] == "ref_consumed"
    assert second["detail"]["line_id"] == "B1"
    assert book.consumed_by("C1") == "B1"
    # one run row feeds one conduit line and one line per conductor size
    assert book.add(_wire())["line_id"] == "B2"
    assert book.add(_wire(description="#12 THHN/THWN Cu GROUND"))["line_id"] == "B3"
    conduit = _line(group="WIRING", subgroup="CONDUITS", description="3/4\"C EMT", unit="FT",
                    source_refs=[{"ref": "R1", "multiplier": 1}])
    assert book.add(conduit)["line_id"] == "B4"
    assert book.add(dict(conduit, description="1\"C EMT"))["code"] == "ref_consumed"
    assert book.add(_wire())["code"] == "ref_consumed"
    # update keeps its own refs and recomputes
    upd = book.update("B1", {"source_refs": [{"ref": "C1", "multiplier": 1}, {"ref": "C2", "multiplier": 1}], "description": None})
    assert upd["quantity"] == 20 and book.lines["B1"]["description"] == "DUPLEX RECEPTACLE"
    assert book.update("B9", {})["code"] == "unknown_line"
    # a refused update changes nothing
    bad = book.update("B1", {"source_refs": [{"ref": "C3", "multiplier": 1}]})
    assert bad["code"] == "partial_requires_floor" and book.lines["B1"]["quantity"] == 20
    freed = book.remove("B1", "duplicate")
    assert freed == {"line_id": "B1", "freed_refs": ["C1", "C2"]}
    again = book.add(_line(description="DUPLEX RECEPTACLE, GFCI"))
    assert again["line_id"] == "B5"  # ids are never reused
    assert book.lines["B5"]["source_refs"] == [{"ref": "C1", "multiplier": 1.0}]
    assert book.lines["B5"]["combine"] == "sum" and book.lines["B5"]["is_accessory"] is False


def test_recheck_arithmetic():
    book = lines.LineBook(ROWS)
    book.add(_line())
    book.add(_wire())
    stored = [dict(v) for v in book.lines.values()]
    assert lines.recheck(stored, ROWS) == []
    stored[0]["quantity"] = 13
    stored[1]["quantity"] = 200.5  # FT within max(1 FT, 0.5%)
    problems = lines.recheck(stored, ROWS)
    assert [p["line_id"] for p in problems] == ["B1"] and problems[0]["code"] == "arithmetic"


# 7154 fixture rows --------------------------------------------------------------------------------


def test_7154_tile_subtotal_row_only_as_floor():
    d = load_7154()
    v2 = rows_mod.upgrade_category_output(d["electrical"], rows_meta(d))
    index = rows_mod.index_rows(v2)
    rect = [r for r in index.values() if "rectangular" in r.get("description", "")][0]["row_id"]
    demo = _line(section="DEMOLITION", group="DEMOLITION", subgroup="", description="REMOVE RECTANGULAR LIGHT FIXTURE",
                 source_refs=[{"ref": rect, "multiplier": 1}], material_category="Demolition")
    _refused(lines.compute(demo, index), lines.PARTIAL_REQUIRES_FLOOR)
    got = _ok(lines.compute(dict(demo, combine="floor"), index))
    assert got.quantity == 5 and got.needs_recount and got.sheet_refs == ["ED3.01"]
    track = [r for r in index.values() if r.get("description") == "Remove existing lighting track row"][0]["row_id"]
    _refused(lines.compute(dict(demo, description="REMOVE LIGHTING TRACK", combine="sum",
                                source_refs=[{"ref": track, "multiplier": 1}]), index), lines.UNIT_MISMATCH)


# Never lower (property) -------------------------------------------------------------------------


def _random_rows(rng: random.Random, n: int) -> dict[str, dict]:
    out = {}
    for i in range(1, n + 1):
        cov = rng.choice(["sheet_total", "sheet_total", "schedule", "tile_subtotal", "partial"])
        out[f"C{i}"] = _row(row_id=f"C{i}", description="Duplex receptacle", quantity=rng.randint(0, 60), unit="EA",
                            basis="counted_symbols", coverage=cov, sheet=_sheet(f"E{rng.randint(1, 9)}.01"))
    return out


def _check_never_lower(rng: random.Random) -> None:
    rows = _random_rows(rng, rng.randint(1, 8))
    ids = list(rows)
    picked = rng.sample(ids, rng.randint(1, len(ids)))
    refs = [{"ref": r, "multiplier": rng.randint(1, 4)} for r in picked]
    partial = any(rows[r]["coverage"] in ("tile_subtotal", "partial") for r in picked)
    modes = ["floor"] if partial else ["sum", "max", "floor"]
    for mode in modes:
        line = _line(source_refs=refs, combine=mode)
        got = _ok(lines.compute(line, rows))
        terms = [rows[r["ref"]]["quantity"] * r["multiplier"] for r in refs]
        assert got.quantity >= max(terms) - 1e-9
        if mode in ("sum", "floor"):
            assert got.quantity == pytest.approx(sum(terms))
        # raising any cited row never lowers the line
        bumped = {k: dict(v) for k, v in rows.items()}
        victim = rng.choice(picked)
        bumped[victim]["quantity"] += rng.randint(1, 10)
        assert _ok(lines.compute(line, bumped)).quantity >= got.quantity
        # citing one more row never lowers the line
        spare = [r for r in ids if r not in picked and (mode == "floor" or rows[r]["coverage"] not in ("tile_subtotal", "partial"))]
        if spare:
            more = line | {"source_refs": refs + [{"ref": spare[0], "multiplier": 1}]}
            assert _ok(lines.compute(more, rows)).quantity >= got.quantity
        if mode == "floor":
            assert got.needs_recount is True and got.basis == "census_floor"


def test_never_lower_seeded_random():
    rng = random.Random(7154)
    for _ in range(400):
        _check_never_lower(rng)


def test_never_lower_hypothesis():
    hypothesis = pytest.importorskip("hypothesis")
    st = pytest.importorskip("hypothesis.strategies")

    @hypothesis.settings(max_examples=150, deadline=None)
    @hypothesis.given(st.integers(min_value=0, max_value=2**31 - 1))
    def run(seed: int) -> None:
        _check_never_lower(random.Random(seed))

    run()


# Split pairs (DEV-TR on 7154 run 2; Z2) ----------------------------------------------

SPLIT_ROWS: dict[str, dict] = {
    "C34": _row(row_id="C34", description="Duplex receptacle, 125V, emergency circuit", quantity=31, unit="EA",
                basis="counted_symbols", sheet=_sheet("E2.01")),
    "C32": _row(row_id="C32", description="Duplex receptacle, 125V, normal circuit", quantity=26, unit="EA",
                basis="counted_symbols", sheet=_sheet("E2.01")),
}


def _split_pair(parent_qty=28, child_qty=3, listed=None, **child_over) -> list[dict]:
    """Run 2 of 7154: B8 cites C34 (31 EA); enforce moved 3 to the TR line
    B41, which cites the same row with combine split and split_of B8."""
    parent = {"line_id": "B8", "description": "DUPLEX RECEPTACLE", "quantity": parent_qty, "unit": "EA",
              "source_refs": [{"ref": "C34", "multiplier": 1}], "combine": "single",
              "split_to": [{"line_id": "B41", "quantity": child_qty if listed is None else listed}]}
    child = {"line_id": "B41", "description": "DUPLEX RECEPTACLE, TR", "quantity": child_qty, "unit": "EA",
             "source_refs": [{"ref": "C34", "multiplier": 1}], "combine": "split", "split_of": "B8", "scope_rule": "DEV-TR"}
    child.update(child_over)
    return [parent, child]


def test_split_pair_markers_and_pairing():
    parent, child = _split_pair()
    assert lines.split_parent(child) == "B8" and lines.split_parent(parent) is None
    assert lines.split_children(parent) == [("B41", 3.0)] and lines.split_children(child) == []
    assert lines.is_split_line(child) and not lines.is_split_line(parent)
    assert lines.split_pairs([parent, child]) == {"B41": "B8"}
    # the list form of split_of is accepted too
    listed = {**child, "split_of": [{"line_id": "B8", "quantity": 3}]}
    assert lines.split_parent(listed) == "B8" and lines.split_pairs([parent, listed]) == {"B41": "B8"}
    # a one-sided marker is not a pair
    assert lines.split_pairs([{**parent, "split_to": []}, child]) == {}
    assert lines.split_pairs([parent, {**child, "split_of": None}]) == {}
    assert lines.split_pairs([parent, {**child, "split_of": "B99"}]) == {}
    assert (lines.SPLIT_COMBINE, lines.SPLIT_OF, lines.SPLIT_TO) == ("split", "split_of", "split_to")


def test_recheck_split_pair_adds_up_to_the_sources():
    """28 + 3 = 31: the parent is expected at its sources minus split_to,
    the split line at its own stored quantity."""
    assert lines.recheck(_split_pair(), SPLIT_ROWS) == []
    assert lines.recheck(_split_pair(split_of=[{"line_id": "B8", "quantity": 3}]), SPLIT_ROWS) == []
    # the parent not reduced (run 2 before the fix: stored 31 against 28 + 3)
    problems = lines.recheck(_split_pair(parent_qty=31), SPLIT_ROWS)
    assert [p["line_id"] for p in problems] == ["B8"]
    assert (problems[0]["code"], problems[0]["stored"], problems[0]["computed"], problems[0]["sources"], problems[0]["split_off"]) == ("arithmetic", 31, 28, 31, 3)
    assert "minus 3 split off" in problems[0]["message"]
    # the split line stores more than the parent lists for it
    problems = lines.recheck(_split_pair(child_qty=4, listed=3), SPLIT_ROWS)
    assert [(p["line_id"], p["stored"], p["computed"]) for p in problems] == [("B41", 4, 3)]
    # the parent lists 4, keeps 28: 28 + 4 != 31
    problems = lines.recheck(_split_pair(child_qty=4, listed=4), SPLIT_ROWS)
    assert [(p["line_id"], p["computed"]) for p in problems] == [("B8", 27)]
    # the split line must cite its parent's rows
    problems = lines.recheck(_split_pair(source_refs=[{"ref": "C32", "multiplier": 1}]), SPLIT_ROWS)
    assert len(problems) == 1 and problems[0]["line_id"] == "B41" and problems[0]["refs"] == ["C32"] and problems[0]["parent_refs"] == ["C34"]
    # bounded by the parent's sources
    problems = lines.recheck(_split_pair(parent_qty=28, child_qty=40, listed=40), SPLIT_ROWS)
    assert {p["line_id"] for p in problems} == {"B8", "B41"} and any("exceeds the parent" in p["message"] for p in problems)
    # a split line without a parent, or not listed by it, or of another unit
    problems = lines.recheck(_split_pair(split_of="B99"), SPLIT_ROWS)
    assert [p["line_id"] for p in problems] == ["B41"] and "names no parent" in problems[0]["message"]
    parent, child = _split_pair()
    problems = lines.recheck([{**parent, "split_to": []}, child], SPLIT_ROWS)
    assert [p["line_id"] for p in problems] == ["B8", "B41"]  # parent now expected at 31; child not listed
    assert "not listed" in problems[1]["message"]
    problems = lines.recheck(_split_pair(unit="FT"), SPLIT_ROWS)
    assert any("is FT; its parent B8 is EA" in p["message"] for p in problems)
    # a zero split line is a mistake; a manual parent is skipped as before
    assert any("quantity > 0" in p["message"] for p in lines.recheck(_split_pair(child_qty=0, listed=0), SPLIT_ROWS))
    assert lines.recheck([{**parent, "combine": "manual"}], SPLIT_ROWS) == []
    # the plain recheck is unchanged for lines without markers
    plain = {k: v for k, v in parent.items() if k != "split_to"}
    assert lines.recheck([{**plain, "quantity": 31}], SPLIT_ROWS) == []
    assert lines.recheck([plain], SPLIT_ROWS)[0]["computed"] == 31

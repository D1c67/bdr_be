"""WS5 device rules (app/services/scope_devices.py): unpriced modifiers merge
in the receptacle family conserving totals, TR from the legend symbol count,
hospital and spec grade to material notes and SpecFacts (E21, E13)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.services import scope_devices as sd
from app.services.eval_bundle import DEFAULT_BACKUP_DIR
from app.services.scope_attribution import Statement

FIXTURE = Path(__file__).parent / "fixtures" / "plans_reader" / "run_7154"
ELEC = (
    DEFAULT_BACKUP_DIR / "storage" / "project-files" / "7dfc64e6-36ef-4c67-9d02-0d7942015011" / "drawing"
    / "c85a4720840d49a288d9d0c7d3c87fed-Reference Material - Architectural - ELEC_RESPONSE_TO_CLV_COMMENTS D1.pdf"
)


def _boq() -> dict:
    return copy.deepcopy(json.loads((FIXTURE / "outputs" / "main_none_v1.json").read_text())["boq"])


def _receptacles(spec: dict) -> list[tuple[str, float]]:
    out = []
    for heading, _g, _s, items in sd._subgroups(spec):
        if heading.upper().startswith("DEMO"):
            continue
        out += [(it["description"], it["quantity"]) for it in items if sd.is_receptacle_row(it["description"])]
    return out


def test_7154_receptacles_total_74_with_no_style_splits_and_tr_3():
    spec = _boq()
    assert sd.receptacle_total(spec) == 74
    log = sd.merge_receptacles(spec)
    assert log
    rows = _receptacles(spec)
    assert sd.receptacle_total(spec) == 74
    assert not [d for d, _q in rows if sd.has_unpriced_modifier(d)]
    res = sd.apply_tr(spec, 3, cite="REC-TAMP legend symbol on E0.00; counted E2.01 x3")
    assert res and res["applied"] and res["tr_quantity"] == 3
    rows = dict(_receptacles(spec))
    assert rows[sd.TR_DESCRIPTION] == 3
    assert rows["DUPLEX RECEPTACLE"] == 54
    assert rows["DUPLEX RECEPTACLE, GFCI"] == 10
    assert sd.receptacle_total(spec) == 74


def test_merge_and_tr_are_idempotent():
    spec = _boq()
    sd.merge_receptacles(spec)
    sd.apply_tr(spec, 3)
    snapshot = json.dumps(spec, sort_keys=True)
    assert sd.merge_receptacles(spec) == []
    assert sd.apply_tr(spec, 3) is None
    assert json.dumps(spec, sort_keys=True) == snapshot


def test_canonical_receptacle_drops_only_unpriced_modifiers():
    assert sd.canonical_receptacle("DUPLEX RECEPTACLE, EMERGENCY, DEDICATED") == "DUPLEX RECEPTACLE"
    assert sd.canonical_receptacle("DUPLEX RECEPTACLE, GFCI, EMERGENCY, DEDICATED") == "DUPLEX RECEPTACLE, GFCI"
    assert sd.canonical_receptacle("DUPLEX RECEPTACLE, RED") == "DUPLEX RECEPTACLE"
    assert sd.canonical_receptacle("DUPLEX RECEPTACLE, OCCUPANCY CONTROLLED") == "DUPLEX RECEPTACLE, OCCUPANCY CONTROLLED"
    assert sd.canonical_receptacle("DUPLEX RECEPTACLE, HOSPITAL GRADE, GREEN DOT") == "DUPLEX RECEPTACLE, HOSPITAL GRADE, GREEN DOT"
    assert not sd.is_receptacle_row("REMOVE DUPLEX RECEPTACLE")
    assert not sd.is_receptacle_row("SUPPLY AND INSTALL AV POWER SYSTEM, INCLUDING NEMA RECEPTACLES")


def test_exempt_rows_are_never_merged():
    spec = {"areas": [{"area": "A", "sections": [{"heading": "NEW WORK", "groups": [{"name": "OTHER ITEMS", "subgroups": [
        {"name": "RECEPTACLES", "items": [
            {"description": "DUPLEX RECEPTACLE", "quantity": 4, "unit": "EA"},
            {"description": "DUPLEX RECEPTACLE, EMERGENCY", "quantity": 2, "unit": "EA", "origin": "chat"},
        ]}]}]}]}]}
    assert sd.merge_receptacles(spec) == []
    assert len(spec["areas"][0]["sections"][0]["groups"][0]["subgroups"][0]["items"]) == 2


def test_tr_without_plain_duplex_line_reports_and_changes_nothing():
    spec = {"areas": [{"area": "A", "sections": [{"heading": "NEW WORK", "groups": [{"name": "OTHER ITEMS", "subgroups": [
        {"name": "RECEPTACLES", "items": [{"description": "DUPLEX RECEPTACLE, GFCI", "quantity": 4, "unit": "EA"}]}]}]}]}]}
    res = sd.apply_tr(spec, 2)
    assert res == {"applied": False, "reason": "no_plain_duplex_line", "target": 2, "current": 0.0}


def test_grade_facts_go_to_material_notes_and_spec_facts():
    note = Statement(
        text="ALL RECEPTACLES INSTALLED IN PATIENT CARE AREAS SHALL BE HOSPITAL GRADE DEVICES WITH GREEN DOT. "
             "ALL RECEPTACLES IN OFFICES, CORRIDORS SHALL BE TAMPER RESISTANT PER NEC ARTICLES 517 AND 406.12.",
        document_id="d", page=9, role="sheet_note", level="sheet_note", sheet="E2.01", ref="SN10")
    notes, facts, room = sd.grade_facts([note])
    assert facts == [{"key": "device_grade", "value": "hospital", "source_ref": "E2.01 p.9 SN10",
                      "quote": note.text[:240]}]
    assert room and "Tamper resistant" in room
    spec = _boq()
    sd.merge_receptacles(spec)
    assert sd.apply_grade_notes(spec, notes) > 0
    assert sd.apply_grade_notes(spec, notes) == 0  # once only
    rows = [it for h, _g, _s, items in sd._subgroups(spec) if not h.startswith("DEMO") for it in items
            if sd.is_receptacle_row(it["description"])]
    assert all("Hospital grade" in (it.get("material_note") or "") for it in rows)
    assert not [it for it in rows if "GREEN" in it["description"]]


def test_tr_legend_entry_from_the_ws1_legend():
    data = json.loads((FIXTURE / "page_facts_elec.json").read_text())
    facts = next(d for d in data["documents"] if d["key"] == "elec")["facts"]
    entry = sd.tr_legend_entry(facts["legend"])
    assert entry and entry["tag"] == "REC-TAMP" and entry["page"] == 1 and entry["symbol_bbox"]


@pytest.mark.golden
@pytest.mark.skipif(not ELEC.exists(), reason="7154 electrical set is not in the backup on disk")
def test_7154_rec_tamp_symbol_count_is_3_on_e201():
    data = json.loads((FIXTURE / "page_facts_elec.json").read_text())
    facts = next(d for d in data["documents"] if d["key"] == "elec")["facts"]
    entry = sd.tr_legend_entry(facts["legend"])
    res = sd.count_legend_symbol(ELEC.read_bytes(), legend_page=entry["page"], symbol_bbox=entry["symbol_bbox"],
                                 plan_pages=[7, 8, 9, 10])
    assert res is not None
    assert res["pages"] == {"7": 0, "8": 0, "9": 3, "10": 0}
    assert res["total"] == 3
    assert 1.6 < res["template"]["arm_ratio"] < 1.9 and 110 < res["template"]["angle_deg"] < 130


# Wave 3 fix Y3 (run 51ec302b): the TR split on the reader's duplex rows ----------------------------


def _spec_with(items: list[dict]) -> dict:
    return {"areas": [{"area": "A", "sections": [{"heading": "NEW WORK", "groups": [{"name": "OTHER ITEMS", "subgroups": [
        {"name": "RECEPTACLES", "items": items}]}]}]}]}


def test_canonical_drops_modifier_residue_location_tails_default_voltage_and_plural():
    # the two duplex rows of run 51ec302b, as the reader wrote them
    assert sd.canonical_receptacle("DUPLEX RECEPTACLES, 125V, EMERGENCY BRANCH") == "DUPLEX RECEPTACLE"
    assert sd.canonical_receptacle("DUPLEX RECEPTACLES, NORMAL BRANCH, COUNTER AND GENERAL LOCATIONS") == "DUPLEX RECEPTACLE"
    # and as the stored (already enforced) main output carries them
    assert sd.canonical_receptacle("DUPLEX RECEPTACLES, 125V, BRANCH") == "DUPLEX RECEPTACLE"
    assert sd.is_plain_duplex("DUPLEX RECEPTACLES, 125V, BRANCH")
    assert sd.is_plain_duplex("DUPLEX RECEPTACLE, 20A")
    assert sd.canonical_receptacle("QUAD RECEPTACLE, 125V, TV LOCATION") == "QUAD RECEPTACLE"
    assert sd.canonical_receptacle("GFCI DUPLEX RECEPTACLE, EMERGENCY BRANCH, LOUNGE REFRIGERATOR") == \
        "GFCI DUPLEX RECEPTACLE, LOUNGE REFRIGERATOR"
    # priced splits and non-default voltages stay
    assert sd.canonical_receptacle("DUPLEX RECEPTACLE, 250V") == "DUPLEX RECEPTACLE, 250V"
    assert not sd.is_plain_duplex("DUPLEX RECEPTACLE, GFCI")
    assert not sd.is_plain_duplex("DUPLEX RECEPTACLE, TR")
    assert not sd.is_plain_duplex("DUPLEX RECEPTACLE, OCCUPANCY CONTROLLED")
    assert not sd.is_plain_duplex("DUPLEX RECEPTACLE, 250V")


def test_tr_split_on_the_run_51ec302b_duplex_rows_gives_54_and_3():
    spec = _spec_with([
        {"line_id": "B5", "description": "DUPLEX RECEPTACLES, BRANCH, COUNTER AND GENERAL LOCATIONS", "quantity": 26,
         "unit": "EA", "sheet_refs": ["E2.01"]},
        {"line_id": "B6", "description": "GFCI DUPLEX RECEPTACLES, 125V, BRANCH", "quantity": 9, "unit": "EA",
         "sheet_refs": ["E2.01"]},
        {"line_id": "B7", "description": "DUPLEX RECEPTACLES, 125V, BRANCH", "quantity": 31, "unit": "EA",
         "sheet_refs": ["E2.01"]},
        {"line_id": "B8", "description": "GFCI DUPLEX RECEPTACLE, BRANCH, LOUNGE REFRIGERATOR", "quantity": 1,
         "unit": "EA", "sheet_refs": ["E2.01"]},
        {"line_id": "B12", "description": "QUAD RECEPTACLE, 125V, TV LOCATION", "quantity": 1, "unit": "EA",
         "sheet_refs": ["E2.01"]},
    ])
    assert sd.receptacle_total(spec) == 68
    log = sd.merge_receptacles(spec)
    assert [(m["description"], m["quantity"]) for m in log if len(m["merged"]) > 1] == [("DUPLEX RECEPTACLE", 57)]
    res = sd.apply_tr(spec, 3, cite="REC-TAMP legend symbol on E0.00; counted E2.01 x3", tr_pages={"E2.01": 3})
    assert res and res["applied"] and res["reduced"] and res["tr_quantity"] == 3 and res["from_line"] == "B5"
    rows = dict(_receptacles(spec))
    assert rows["DUPLEX RECEPTACLE"] == 54 and rows[sd.TR_DESCRIPTION] == 3
    assert rows["GFCI DUPLEX RECEPTACLE"] == 9 and rows["QUAD RECEPTACLE"] == 1
    assert sd.receptacle_total(spec) == 68
    assert sd.apply_tr(spec, 3, tr_pages={"E2.01": 3}) is None  # idempotent


def test_tr_line_is_not_reduced_from_a_duplex_line_that_cites_other_sheets():
    spec = _spec_with([{"line_id": "B1", "description": "DUPLEX RECEPTACLE", "quantity": 20, "unit": "EA",
                        "sheet_refs": ["E2.02"]}])
    res = sd.apply_tr(spec, 3, tr_pages={"E2.01": 3})
    assert res and res["applied"] and res["reduced"] is False
    assert res["reason"] == "tr_symbols_counted_on_other_sheets" and res["plain_sheets"] == ["E2.02"]
    rows = dict(_receptacles(spec))
    assert rows["DUPLEX RECEPTACLE"] == 20 and rows[sd.TR_DESCRIPTION] == 3
    assert sd.apply_tr(spec, 3, tr_pages={"E2.01": 3}) is None
    # without sheet knowledge on either side the quantity moves (totals conserved), as before
    spec2 = _spec_with([{"line_id": "B1", "description": "DUPLEX RECEPTACLE", "quantity": 20, "unit": "EA"}])
    res2 = sd.apply_tr(spec2, 3, tr_pages={"E2.01": 3})
    assert res2 and res2["reduced"] and dict(_receptacles(spec2))["DUPLEX RECEPTACLE"] == 17


def test_tr_prefers_the_plain_duplex_line_on_the_counted_sheet():
    spec = _spec_with([
        {"line_id": "B1", "description": "DUPLEX RECEPTACLE", "quantity": 40, "unit": "EA", "sheet_refs": ["E2.02"]},
        {"line_id": "B2", "description": "DUPLEX RECEPTACLE, 20A", "quantity": 10, "unit": "EA", "sheet_refs": ["E2.01"],
         "origin": "chat"},
        {"line_id": "B3", "description": "DUPLEX RECEPTACLE, 125V", "quantity": 12, "unit": "EA", "sheet_refs": ["E2.01"]},
    ])
    res = sd.apply_tr(spec, 3, tr_pages={"E2.01": 3})
    assert res and res["from_line"] == "B3" and res["reduced"]
    rows = {it.get("line_id"): it["quantity"] for _h, _g, _s, items in sd._subgroups(spec) for it in items}
    assert rows["B1"] == 40 and rows["B2"] == 10 and rows["B3"] == 9 and rows[None] == 3


def _rows_by_id(spec: dict) -> dict:
    return {it.get("line_id"): it for _h, _g, _s, items in sd._subgroups(spec) for it in items}


def test_tr_split_carries_the_provenance_markers_of_its_parent():
    """WS4 gates on run 2492ba7a (B41 unsourced, B8 arithmetic): the TR line
    the rule creates cites the rows of the plain line it split from with
    combine split and split_of; the parent lists it in split_to with its
    quantity, the shape plans_reader_lines.recheck and split_pairs read."""
    from app.services import plans_reader_lines as lines_mod

    ids = iter(["B41"])
    spec = _spec_with([
        {"line_id": "B8", "description": "DUPLEX RECEPTACLE", "quantity": 31, "unit": "EA", "sheet_refs": ["E2.01"],
         "source_refs": [{"ref": "C34", "multiplier": 1.0}], "combine": "single", "basis": "counted"},
    ])
    res = sd.apply_tr(spec, 3, tr_pages={"E2.01": 3}, new_line_id=lambda: next(ids))
    assert res and res["applied"] and res["reduced"] and res["created"] and res["moved"] == 3
    assert res["split"] == {"parent": "B8", "child": "B41", "quantity": 3} and res["line_id"] == "B41"
    rows = _rows_by_id(spec)
    tr, plain = rows["B41"], rows["B8"]
    assert tr["quantity"] == 3 and tr["source_refs"] == [{"ref": "C34", "multiplier": 1.0}]
    assert tr["combine"] == "split" and tr["split_of"] == "B8" and tr["split_quantity"] == 3 and tr["basis"] == "counted"
    assert plain["quantity"] == 28 and plain["split_to"] == [{"line_id": "B41", "quantity": 3}]
    assert plain["source_refs"] == [{"ref": "C34", "multiplier": 1.0}] and plain["combine"] == "single"
    assert lines_mod.split_pairs([plain, tr]) == {"B41": "B8"}
    c34 = {"row_id": "C34", "kind": "C", "description": "DUPLEX RECEPTACLE", "quantity": 31, "unit": "EA",
           "status": "active", "coverage": "sheet_total", "sheet": {"sheet_number": "E2.01", "sheet_key": "E2.01"}}
    assert lines_mod.recheck([plain, tr], {"C34": c34}) == []
    snapshot = json.dumps(spec, sort_keys=True)
    assert sd.apply_tr(spec, 3, tr_pages={"E2.01": 3}, new_line_id=lambda: "B99") is None  # idempotent
    assert json.dumps(spec, sort_keys=True) == snapshot
    # a legacy plain line (no refs) carries nothing to split: no markers, as before
    legacy = _spec_with([{"line_id": "B1", "description": "DUPLEX RECEPTACLE", "quantity": 20, "unit": "EA"}])
    res2 = sd.apply_tr(legacy, 3, new_line_id=lambda: "B2")
    assert res2 and res2["applied"] and res2["split"] is None
    rows2 = _rows_by_id(legacy)
    assert rows2["B1"]["quantity"] == 17 and "split_to" not in rows2["B1"]
    assert rows2[None]["quantity"] == 3 and "split_of" not in rows2[None] and "source_refs" not in rows2[None]
    # a TR line the model wrote on other rows is not a split of the plain line: quantity moves, no markers
    other = _spec_with([
        {"line_id": "B1", "description": "DUPLEX RECEPTACLE", "quantity": 31, "unit": "EA",
         "source_refs": [{"ref": "C34", "multiplier": 1.0}], "combine": "single"},
        {"line_id": "B2", "description": "DUPLEX RECEPTACLE, TR", "quantity": 1, "unit": "EA",
         "source_refs": [{"ref": "C35", "multiplier": 1.0}], "combine": "single"},
    ])
    res3 = sd.apply_tr(other, 3)
    rows3 = _rows_by_id(other)
    assert res3 and res3["split"] is None and not res3["created"]
    assert rows3["B2"]["quantity"] == 3 and "split_of" not in rows3["B2"] and "split_to" not in rows3["B1"]
    assert rows3["B2"]["combine"] == "single" and rows3["B1"]["quantity"] == 29


def test_mark_split_rebuilds_the_pair_from_a_recorded_decision():
    """A main output enforced before the markers existed: the enforcer calls
    mark_split with the quantity the recorded decision moved."""
    parent = {"line_id": "B8", "description": "DUPLEX RECEPTACLE", "quantity": 28, "unit": "EA",
              "source_refs": [{"ref": "C34", "multiplier": 1.0}], "combine": "single", "basis": "counted"}
    child = {"line_id": "B41", "description": sd.TR_DESCRIPTION, "quantity": 3, "unit": "EA", "scope_rule": "DEV-TR"}
    assert sd.mark_split(parent, child, 3) == {"parent": "B8", "child": "B41", "quantity": 3}
    assert child["source_refs"] == parent["source_refs"] and child["combine"] == "split" and child["split_of"] == "B8"
    assert parent["split_to"] == [{"line_id": "B41", "quantity": 3}]
    assert sd.mark_split(parent, {"line_id": "B42", "quantity": 2, "source_refs": [{"ref": "C9", "multiplier": 1.0}]}, 2) is None
    assert parent["split_to"] == [{"line_id": "B41", "quantity": 3}]
    assert sd.mark_split({"line_id": "B1", "quantity": 5}, {"quantity": 1}, 1) is None  # legacy parent


def test_merge_carries_every_member_through_the_merge_sources_hook():
    """Run ad69e5a4: B36 (9 EA, C33) folded into B35 (26 EA, C32) summed the
    quantities but kept only B35's refs (gate arithmetic "stored 35 EA,
    computed 26 EA", orphan C33). `merge_sources` receives the surviving
    line and every merged member, and the log records each member's refs."""
    calls: list[tuple[str, list[str]]] = []

    def hook(head: dict, members) -> None:
        calls.append((head["line_id"], [m["line_id"] for m in members]))
        head["source_refs"] = [r for m in members for r in m.get("source_refs") or []]
        head["combine"] = "sum"

    spec = _spec_with([
        {"line_id": "B35", "description": "DUPLEX RECEPTACLE, GFCI", "quantity": 26, "unit": "EA",
         "source_refs": [{"ref": "C32", "multiplier": 1.0}], "combine": "single", "sheet_refs": ["E2.01"]},
        {"line_id": "B36", "description": "DUPLEX RECEPTACLE, GFCI, EMERGENCY", "quantity": 9, "unit": "EA",
         "source_refs": [{"ref": "C33", "multiplier": 1.0}], "combine": "single", "sheet_refs": ["E2.01"]},
        {"line_id": "B37", "description": "QUAD RECEPTACLE", "quantity": 2, "unit": "EA",
         "source_refs": [{"ref": "C40", "multiplier": 1.0}], "combine": "single"},
    ])
    log = sd.merge_receptacles(spec, merge_sources=hook)
    assert calls == [("B35", ["B35", "B36"])]  # a lone canonical line never reaches the hook
    rows = _rows_by_id(spec)
    assert "B36" not in rows and rows["B35"]["quantity"] == 35
    assert rows["B35"]["source_refs"] == [{"ref": "C32", "multiplier": 1.0}, {"ref": "C33", "multiplier": 1.0}]
    assert rows["B35"]["combine"] == "sum" and rows["B37"]["source_refs"] == [{"ref": "C40", "multiplier": 1.0}]
    (entry,) = log
    assert [(m["line_id"], m["quantity"], m["source_refs"]) for m in entry["merged"]] == [
        ("B35", 26, [{"ref": "C32", "multiplier": 1.0}]), ("B36", 9, [{"ref": "C33", "multiplier": 1.0}])]
    assert sd.merge_receptacles(spec, merge_sources=hook) == [] and calls == [("B35", ["B35", "B36"])]


def test_merge_without_the_hook_keeps_the_legacy_shape():
    spec = _spec_with([
        {"line_id": "B1", "description": "DUPLEX RECEPTACLE", "quantity": 4, "unit": "EA"},
        {"line_id": "B2", "description": "DUPLEX RECEPTACLE, EMERGENCY", "quantity": 2, "unit": "EA"},
    ])
    (entry,) = sd.merge_receptacles(spec)
    assert [set(m) for m in entry["merged"]] == [{"line_id", "description", "quantity"}] * 2
    rows = _rows_by_id(spec)
    assert rows["B1"]["quantity"] == 6 and "source_refs" not in rows["B1"] and "combine" not in rows["B1"]

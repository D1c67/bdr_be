"""plans_reader_merge: the code merge of group outputs into category output v2
(build spec 4.1, 5.4, WS4). Conservation on the committed 7154 fixture;
synthetic groups for the duplicate rules."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.services import plans_reader_merge as MG
from app.services import plans_reader_rows as R
from app.services.plans_reader_replay import registered_stages
from app.services.plans_reader_runs import _category_output
from app.services.plans_reader_schemas import CATEGORIES
from tests.plans_reader_fixtures import ELEC_DOC_7154, FIXTURE_7154, load_7154


@pytest.fixture(scope="module")
def run7154() -> dict:
    return load_7154()


@pytest.fixture(scope="module")
def elec_pages() -> dict[tuple[str, int], dict]:
    facts = json.loads((FIXTURE_7154 / "page_facts_elec.json").read_text())
    return {(ELEC_DOC_7154, p["page"]): p for p in facts["documents"][0]["facts"]["pages"]}


@pytest.fixture(scope="module")
def merged(run7154, elec_pages) -> dict:
    return MG.consolidate_category(
        "electrical_drawings", run7154["groups"], words_by_page=elec_pages, ledger=run7154["run"]["ledger"],
        documents=run7154["documents"], llm_rollup=False,
    )


# 7154 ---------------------------------------------------------------------------------------


def test_conservation_all_54_sub_rows_active(run7154, merged):
    assert merged["rows_version"] == 2
    first_pass = [r for r in merged["counts"] if r["origin"]["group_kind"] == "sub"]
    assert len(first_pass) == 54 and all(r["status"] == "active" for r in first_pass)
    sub_ids = {g["id"] for g in run7154["groups"] if g["kind"] == "sub"}
    assert {r["origin"]["group_id"] for r in first_pass} <= sub_ids
    active = {(r["document_id"], r["page"], r["description"], r["quantity"]) for r in first_pass}
    for g in run7154["groups"]:
        if g["kind"] == "sub" and g["category"] == "electrical_drawings":
            for c in g["output"]["counts"]:
                assert (c["document_id"], c["page"], c["description"], c["quantity"]) in active
    log = merged["merge_log"]
    assert log["conservation"]["ok"] and log["merge_version"] == MG.MERGE_VERSION
    assert log["inputs"]["counts"] == 58 and log["outputs"]["counts"] == 56
    assert log["duplicates"] == []
    assert MG.conservation(merged)["ok"]
    assert "escalation_patches" not in merged and merged["source"]["rolled_up"] is False
    assert len(merged["source"]["group_ids"]) == 6  # 3 subs + 3 escalations, no roll-up


def test_patch_tile_subtotal_never_total_and_containment_recorded(merged):
    rect = [r for r in merged["counts"] if "rectangular" in r["description"]][0]
    assert rect["quantity"] == 5 and rect["coverage"] == "tile_subtotal" and "needs_recount" in rect["flags"]
    assert rect["origin"]["group_kind"] == "escalation" and rect["status"] == "active"
    cont = {c["page"]: c for c in merged["merge_log"]["containment"]}
    assert cont[8] == {"document_id": ELEC_DOC_7154, "page": 8, "family": "demo_x", "targets": 39, "inside": 22, "complete": False}
    assert cont[7]["inside"] == 16 and cont[7]["targets"] == 34
    # the patches landed as the rows module applies them (alternates, never lower)
    filled = [r for r in merged["counts"] if r["description"] == "Remove existing duplex receptacle, filled symbol"][0]
    assert filled["quantity"] == 9 and filled["coverage"] == "sheet_total" and len(filled["alternates"]) == 1
    track = [r for r in merged["counts"] if "paired track-light head" in r["description"]][0]
    assert track["quantity"] == 12 and len(track["alternates"]) == 1


def test_floors_stored_on_the_output(merged):
    by = {(f["sheet_key"], f["key"]): f for f in merged["floors"]}
    assert by[("D1P8", "demo_x")]["floor"] == 39 and by[("D1P7", "demo_x")]["floor"] == 34
    assert by[("D1P8", "demo_x")]["strength"] == "strong"


def test_v1_assembly_matches_todays_code_with_the_rollup_flag(run7154):
    for cat in ("electrical_drawings", "rfp", "specifications"):
        theirs, their_sources = _category_output(cat, run7154["groups"])
        ours, our_sources = MG.category_v1_output(cat, run7154["groups"], llm_rollup=True)
        assert json.dumps(theirs, sort_keys=True) == json.dumps(ours, sort_keys=True), cat
        assert their_sources == our_sources
    # the roll-up stays behind plans_reader_llm_rollup (code default off)
    ours, sources = MG.category_v1_output("electrical_drawings", run7154["groups"], llm_rollup=False)
    assert ours["source"]["rolled_up"] is False and len(sources) == 6
    assert len(ours["counts"]) == 54 and len(ours["escalation_patches"]) == 3
    assert MG.category_v1_output("mechanical_drawings", run7154["groups"]) == (None, [])
    assert MG.consolidate_category("mechanical_drawings", run7154["groups"]) is None


def test_remerge_with_prior_keeps_ids(run7154, elec_pages, merged):
    again = MG.consolidate_category(
        "electrical_drawings", run7154["groups"], words_by_page=elec_pages, ledger=run7154["run"]["ledger"],
        documents=run7154["documents"], llm_rollup=False, prior=merged,
    )
    assert [(r["row_id"], r["fingerprint"]) for r in again["counts"]] == [(r["row_id"], r["fingerprint"]) for r in merged["counts"]]
    passthrough = MG.consolidate_output("electrical_drawings", merged, run7154["groups"])
    assert [r["row_id"] for r in passthrough["counts"]] == [r["row_id"] for r in merged["counts"]]


def test_replay_merge_stage_registered_and_runs(run7154, elec_pages):
    MG.register_replay_stage()  # another test may have reset the registry to the legacy entries
    spec = registered_stages()["merge"]
    assert spec.owner == "WS4" and not spec.legacy and spec.version == MG.MERGE_VERSION

    class _Ctx:
        def __init__(self):
            self.notes = []

        def note(self, text):
            self.notes.append(text)

    art = {
        "groups": run7154["groups"], "documents": run7154["documents"], "ledger": run7154["run"]["ledger"],
        "category:electrical_drawings": run7154["electrical"], "category:rfp": run7154["rfp"],
        "page_facts": elec_pages,
    }
    ctx = _Ctx()
    out = MG.replay_merge(ctx, art)
    assert set(out) == {"category:electrical_drawings", "category:rfp"}
    assert R.is_v2(out["category:electrical_drawings"]) and R.is_v2(out["category:rfp"])
    assert sum(1 for r in out["category:electrical_drawings"]["counts"] if r["status"] == "active") == 56


# Duplicate rules (synthetic) ------------------------------------------------------------------


def _group(gid: str, kind: str, index: int, counts: list[dict], *, created: str, tiles=None) -> dict:
    page = {"document_id": "d1", "page": 1, "kind": "image", "tiles": tiles}
    return {
        "id": gid, "kind": kind, "category": "electrical_drawings", "group_index": index, "pass": 1 if kind == "sub" else 2,
        "status": "done", "created_at": created, "pages": [page],
        "output": {"category": "electrical_drawings", "summary": gid, "pages": [{"document_id": "d1", "page": 1, "doc_index": 1, "notes": [], "tags": [], "resolution_flags": []}],
                   "counts": counts, "runs": [], "scope": [], "specs": [], "schedules": [], "details": [], "questions": []},
    }


def _count(desc: str, qty: float, tag: str | None = "(X)", **kw) -> dict:
    row = {"description": desc, "symbol_or_tag": tag, "quantity": qty, "unit": "EA", "document_id": "d1", "page": 1,
           "doc_index": 1, "sheet_number": "ED1.01", "location": None, "basis": "counted_symbols", "confidence": 0.8,
           "material_category_hint": None, "notes": None}
    row.update(kw)
    return row


DOCS = [{"id": "d1", "category": "electrical_drawings", "created_at": "2026-01-01"}]


def test_same_group_rows_never_deduped_and_never_summed_across_pages():
    g = _group("g1", "sub", 0, [_count("Remove existing duplex receptacle", 4), _count("Remove existing duplex receptacle", 4),
                                _count("Remove existing duplex receptacle", 6, page=2, sheet_number="ED1.02")], created="2026-01-01T00:00:00")
    g["pages"].append({"document_id": "d1", "page": 2, "kind": "image", "tiles": None})
    v2 = MG.consolidate_category("electrical_drawings", [g], documents=DOCS)
    assert sum(1 for r in v2["counts"] if r["status"] == "active") == 3
    assert v2["merge_log"]["duplicates"] == []
    assert [r["quantity"] for r in v2["counts"]] == [4, 4, 6]  # no sum, no merge


def test_true_duplicates_across_groups_merge_and_never_lower():
    g1 = _group("g1", "sub", 0, [_count("Remove existing duplex receptacle", 4), _count("Remove existing quad receptacle", 2)], created="2026-01-01T00:00:00")
    g2 = _group("g2", "sub", 1, [_count("Remove existing duplex receptacle", 6), _count("Remove existing quad receptacle", 2, tag="(R)"),
                                 _count("Remove  existing duplex  receptacles", 3)], created="2026-01-01T00:00:01")
    v2 = MG.consolidate_category("electrical_drawings", [g1, g2], documents=DOCS)
    active = [r for r in v2["counts"] if r["status"] == "active"]
    descs = [r["description"] for r in active]
    assert descs.count("Remove existing duplex receptacle") == 1
    dup = [r for r in active if r["description"] == "Remove existing duplex receptacle"][0]
    assert dup["quantity"] == 6 and "needs_recount" in dup["flags"]  # raised to the larger read, never lowered
    assert dup["superseded"][-1]["quantity"] == 4 and dup["superseded"][-1]["reason"] == R.RAISED_REASON
    assert {a["quantity"] for a in dup["alternates"]} == {6, 3}  # the similar wording merged too (similarity >= 0.9)
    assert len(v2["merge_log"]["duplicates"]) == 2
    # a different printed tag is a different item
    assert sum(1 for r in active if "quad" in r["description"]) == 2
    assert v2["merge_log"]["conservation"]["ok"] and MG.conservation(v2)["ok"]
    assert v2["merge_log"]["inputs"]["counts"] == 5


def test_escalation_patch_rules_through_the_merge():
    sub = _group("g1", "sub", 0, [_count("Remove existing duplex receptacle", 10)], created="2026-01-01T00:00:00")
    facts = {("d1", 1): {"page": 1, "w_in": 42, "h_in": 30, "text_layer_quality": "full", "roles": ["plan"], "views": [], "regions": [],
                         "tags": {"status": {"X": [[0.2, 0.2], [0.3, 0.3], [0.8, 0.8]]}, "circuit": [], "type_marks": {}, "panel_labels": []}}}
    # a whole_sheet claim with the tiles missing a printed tag is a tile subtotal; it cannot lower
    esc = _group("g2", "escalation", 0, [_count("Remove existing duplex receptacle", 6, coverage="whole_sheet", supersedes_row_id="c1")],
                 created="2026-01-01T00:10:00", tiles=[[0.1, 0.1, 0.5, 0.5]])
    v2 = MG.consolidate_category("electrical_drawings", [sub, esc], words_by_page=facts, documents=DOCS)
    row = [r for r in v2["counts"] if r["status"] == "active"][0]
    assert row["quantity"] == 10 and row["alternates"][0]["quantity"] == 6 and row["alternates"][0]["coverage"] == "tile_subtotal"
    assert v2["merge_log"]["containment"][0]["inside"] == 2 and v2["merge_log"]["containment"][0]["targets"] == 3
    assert v2["merge_log"]["patches"][0]["matched_by"] == "supersedes_row_id"
    # a raise with partial coverage raises, sets partial and needs_recount
    esc_raise = _group("g2", "escalation", 0, [_count("Remove existing duplex receptacle", 14, coverage="inside_tiles_only", supersedes_row_id=None)],
                       created="2026-01-01T00:10:00", tiles=[[0.1, 0.1, 0.5, 0.5]])
    v2 = MG.consolidate_category("electrical_drawings", [sub, esc_raise], words_by_page=facts, documents=DOCS)
    row = [r for r in v2["counts"] if r["status"] == "active"][0]
    assert row["quantity"] == 14 and row["coverage"] == "partial" and "needs_recount" in row["flags"]
    assert v2["merge_log"]["patches"][0]["matched_by"] == "description" and v2["merge_log"]["patches"][0]["action"] == "raised"
    # complete containment makes an equal whole_sheet claim a sheet total that replaces its target
    esc_full = _group("g2", "escalation", 0, [_count("Remove existing duplex receptacle", 10, coverage="whole_sheet", supersedes_row_id="c1")],
                      created="2026-01-01T00:10:00", tiles=[[0.0, 0.0, 1.0, 1.0]])
    v2 = MG.consolidate_category("electrical_drawings", [sub, esc_full], words_by_page=facts, documents=DOCS)
    row = [r for r in v2["counts"] if r["status"] == "active"][0]
    assert row["coverage"] == "sheet_total" and row["origin"]["group_kind"] == "escalation" and v2["merge_log"]["patches"][0]["action"] == "replaced"
    assert v2["floors"][0]["floor"] == 3


def test_merge_error_on_bad_input_and_rollup_flag():
    with pytest.raises(MG.MergeError):
        MG.consolidate_output("electrical_drawings", "nope", [])
    assert MG.llm_rollup_enabled({"plans_reader_llm_rollup": True}) is True
    assert MG.llm_rollup_enabled({}) is False
    sub = _group("g1", "sub", 0, [_count("Remove existing duplex receptacle", 10)], created="2026-01-01T00:00:00")
    roll = _group("g9", "rollup", 0, [_count("Remove existing duplex receptacle", 12)], created="2026-01-01T00:20:00")
    off, _ = MG.category_v1_output("electrical_drawings", [sub, roll], llm_rollup=False)
    on, _ = MG.category_v1_output("electrical_drawings", [sub, roll], llm_rollup=True)
    assert off["counts"][0]["quantity"] == 10 and on["counts"][0]["quantity"] == 12 and on["source"]["rolled_up"]


def test_fixture_outputs_untouched(run7154):
    before = copy.deepcopy(run7154["groups"])
    MG.consolidate_category("electrical_drawings", run7154["groups"], documents=run7154["documents"])
    assert before == run7154["groups"]


# Two groups reading the same page (patch targeting) -----------------------------------------


def test_two_groups_same_page_supersedes_row_id_names_the_source_groups_row():
    """g1 reads [duplex 4], g2 reads [quad 2, duplex 6] on the same page. The
    escalation re-reads g2's sheet and says supersedes_row_id c2, the c-id of
    g2's duplex in g2's own output: the patch lands on g2's duplex, never on
    the quad (the second row of the concatenated output)."""
    g1 = _group("g1", "sub", 0, [_count("Remove existing duplex receptacle", 4, tag="(X)")], created="2026-01-01T00:00:00")
    g2 = _group("g2", "sub", 1, [_count("Remove existing quad receptacle", 2, tag="(Q)"), _count("Remove existing duplex receptacle", 6, tag="(D)")],
                created="2026-01-01T00:00:01")
    esc = _group("g3", "escalation", 0, [_count("Remove existing duplex receptacle", 7, tag="(D)", coverage="inside_tiles_only", supersedes_row_id="c2")],
                 created="2026-01-01T00:10:00", tiles=[[0.1, 0.1, 0.5, 0.5]])
    esc["pages"][0]["source_group_id"] = "g2"
    v2 = MG.consolidate_category("electrical_drawings", [g1, g2, esc], documents=DOCS)
    by = {(r["origin"]["group_id"], r["description"]): r for r in v2["counts"] if r["status"] == "active"}
    assert by[("g2", "Remove existing quad receptacle")]["quantity"] == 2  # untouched
    assert by[("g2", "Remove existing quad receptacle")]["origin"]["local_id"] == "c1"
    target = by[("g2", "Remove existing duplex receptacle")]
    assert target["origin"]["local_id"] == "c2" and target["quantity"] == 7 and target["coverage"] == "partial"
    assert by[("g1", "Remove existing duplex receptacle")]["quantity"] == 4 and by[("g1", "Remove existing duplex receptacle")]["origin"]["local_id"] == "c1"
    (entry,) = v2["merge_log"]["patches"]
    assert entry["matched_by"] == "supersedes_row_id" and entry["action"] == "raised" and entry["target_row_id"] == target["row_id"]
    assert all(R.ORIGIN_KEY not in r for r in v2["counts"])
    assert v2["merge_log"]["conservation"]["ok"]
    # scoped to the source group: c1 names g2's quad, never g1's duplex, even with the same description elsewhere
    esc_c1 = _group("g3", "escalation", 0, [_count("Remove existing quad receptacle", 3, tag="(Q)", coverage="inside_tiles_only", supersedes_row_id="c1")],
                    created="2026-01-01T00:10:00", tiles=[[0.1, 0.1, 0.5, 0.5]])
    esc_c1["pages"][0]["source_group_id"] = "g2"
    v2 = MG.consolidate_category("electrical_drawings", [g1, g2, esc_c1], documents=DOCS)
    by = {(r["origin"]["group_id"], r["description"]): r for r in v2["counts"] if r["status"] == "active"}
    assert by[("g2", "Remove existing quad receptacle")]["quantity"] == 3
    assert by[("g1", "Remove existing duplex receptacle")]["quantity"] == 4


# Recount patches (WS7 outputs) land on their target, never a second active row -------------


_RC_FACTS = {("d1", 1): {"page": 1, "w_in": 42, "h_in": 30, "text_layer_quality": "full", "roles": ["plan"], "views": [], "regions": [],
                         "tags": {"status": {"X": [[0.2, 0.2], [0.3, 0.3]]}, "circuit": [], "type_marks": {}, "panel_labels": []}}}


def _recount_group(gid: str, counts: list[dict], *, complete: bool = True) -> dict:
    """A recount group as plans_reader_recount.group_output writes it: the
    page item has no tiles, the output page carries the code-computed region
    containment."""
    g = _group(gid, "recount", 0, counts, created="2026-01-01T00:20:00")
    g["pass"] = 3
    g["output"]["pages"] = [{"document_id": "d1", "page": 1, "sheet_number": "ED1.01", "tiles": [[0.0, 0.0, 1.0, 1.0]],
                             "containment": {"complete": complete}, "notes": [], "tags": []}]
    return g


def _rc_base() -> list[dict]:
    """7154 shape: the sub read switches and receptacles; an escalation
    re-read the switches whole-sheet and REPLACED the first-pass row, so the
    recount's target is an escalation row the rows module cannot see."""
    sub = _group("g1", "sub", 0, [_count("Remove existing light switches and control wiring", 2, tag="S (X)"),
                                  _count("Remove existing duplex receptacle", 4)], created="2026-01-01T00:00:00")
    esc = _group("g2", "escalation", 0, [_count("Remove existing light switches and control wiring", 2, tag="S (X), keynote 2",
                                                coverage="whole_sheet", supersedes_row_id="c1")],
                 created="2026-01-01T00:10:00", tiles=[[0.0, 0.0, 1.0, 1.0]])
    return [sub, esc]


def _active(v2: dict, desc: str) -> list[dict]:
    return [r for r in v2["counts"] if r["status"] == "active" and r["description"] == desc]


@pytest.mark.parametrize("with_prior", [True, False])
def test_confirmed_and_raised_recount_patches_supersede_their_targets(with_prior):
    switches, duplex = "Remove existing light switches and control wiring", "Remove existing duplex receptacle"
    v1 = MG.consolidate_category("electrical_drawings", _rc_base(), words_by_page=_RC_FACTS, documents=DOCS)
    (sw1,) = _active(v1, switches)
    (dx1,) = _active(v1, duplex)
    assert sw1["origin"]["group_kind"] == "escalation"  # the replacement is the target
    confirmed = _count(switches, 2.0, tag="S (X), keynote 2", coverage="whole_sheet", supersedes_row_id=sw1["row_id"],
                       notes="Blind recount confirmed: recount 2 within 1 of 2.")
    raised = _count(duplex, 6.0, coverage="whole_sheet", supersedes_row_id=dx1["row_id"], notes="Blind recount raise: 6 vs 4.")
    groups = _rc_base() + [_recount_group("g3", [confirmed, raised])]
    v2 = MG.consolidate_category("electrical_drawings", groups, words_by_page=_RC_FACTS, documents=DOCS,
                                 prior=v1 if with_prior else None)
    (sw2,) = _active(v2, switches)  # one active row, never a duplicate
    assert sw2["row_id"] == sw1["row_id"] and sw2["quantity"] == 2.0
    assert sw2["origin"] == {"group_id": "g3", "group_kind": "recount", "pass": 3, "local_id": "p2.c1"}
    assert [s["reason"] for s in sw2["superseded"]] == [MG.REPLACED_REASON, MG.REPLACED_REASON]
    (dx2,) = _active(v2, duplex)
    assert dx2["row_id"] == dx1["row_id"] and dx2["quantity"] == 6.0 and dx2["coverage"] == "sheet_total"
    entries = {e["target_row_id"]: e for e in v2["merge_log"]["patches"] if (e["patch_row"]["origin"] or {}).get("group_kind") == "recount"}
    assert entries[sw1["row_id"]]["action"] == "confirmed" and entries[sw1["row_id"]]["matched_by"] == "supersedes_row_id"
    assert entries[dx1["row_id"]]["action"] == "replaced"
    assert sum(1 for r in v2["counts"] if r["status"] == "active") == 2
    assert v2["merge_log"]["inputs"]["counts"] == 5 and v2["merge_log"]["outputs"]["counts"] == 2
    assert v2["merge_log"]["conservation"]["ok"] and MG.conservation(v2)["ok"]
    assert {r["row_id"] for r in v2["counts"]} == {r["row_id"] for r in v1["counts"]}


def test_recount_partial_raise_and_lower_stay_alternates_on_the_target():
    switches, duplex = "Remove existing light switches and control wiring", "Remove existing duplex receptacle"
    v1 = MG.consolidate_category("electrical_drawings", _rc_base(), words_by_page=_RC_FACTS, documents=DOCS)
    (sw1,) = _active(v1, switches)
    (dx1,) = _active(v1, duplex)
    # region not complete: the raise is a floor (partial, needs_recount); a whole-sheet lower never lowers
    raised = _count(duplex, 6.0, coverage="whole_sheet", supersedes_row_id=dx1["row_id"])
    lower = _count(switches, 1.0, tag="S (X), keynote 2", coverage="whole_sheet", supersedes_row_id=sw1["row_id"])
    groups = _rc_base() + [_recount_group("g3", [raised]), _recount_group("g4", [lower])]
    groups[-1]["output"]["pages"][0]["containment"] = {"complete": True}
    groups[-2]["output"]["pages"][0]["containment"] = {"complete": False}
    v2 = MG.consolidate_category("electrical_drawings", groups, words_by_page=_RC_FACTS, documents=DOCS, prior=v1)
    (dx2,) = _active(v2, duplex)
    assert dx2["row_id"] == dx1["row_id"] and dx2["quantity"] == 6.0 and dx2["coverage"] == "partial"
    assert "needs_recount" in dx2["flags"] and dx2["alternates"][-1]["origin"]["group_kind"] == "recount"
    (sw2,) = _active(v2, switches)
    assert sw2["row_id"] == sw1["row_id"] and sw2["quantity"] == 2 and "needs_recount" in sw2["flags"]
    assert sw2["alternates"][-1]["quantity"] == 1.0
    assert MG.conservation(v2)["ok"]


def test_recount_id_from_another_numbering_never_lands_on_another_item():
    """Without prior the run-level id of the recount may name a different
    row; the row id is trusted only when the item agrees, else the exact
    description finds the target."""
    switches = "Remove existing light switches and control wiring"
    v1 = MG.consolidate_category("electrical_drawings", _rc_base(), words_by_page=_RC_FACTS, documents=DOCS)
    (dx1,) = _active(v1, "Remove existing duplex receptacle")
    wrong = _count(switches, 2.0, tag="S (X), keynote 2", coverage="whole_sheet", supersedes_row_id=dx1["row_id"])
    v2 = MG.consolidate_category("electrical_drawings", _rc_base() + [_recount_group("g3", [wrong])], words_by_page=_RC_FACTS, documents=DOCS)
    assert _active(v2, "Remove existing duplex receptacle")[0]["quantity"] == 4
    (sw2,) = _active(v2, switches)
    assert sw2["origin"]["group_kind"] == "recount"
    assert [e for e in v2["merge_log"]["patches"] if e["patch_row"]["origin"]["group_kind"] == "recount"][0]["matched_by"] == "description"


def test_split_recount_patches_and_restore_prior_ids():
    groups = _rc_base() + [_recount_group("g3", [_count("Remove existing duplex receptacle", 4.0, coverage="whole_sheet")])]
    v1, _ = MG.category_v1_output("electrical_drawings", groups)
    base, recounts = MG.split_recount_patches(v1, groups, "electrical_drawings")
    assert len(v1["escalation_patches"]) == 2 and len(base["escalation_patches"]) == 1
    assert [(i, g["id"]) for i, g, _p in recounts] == [(1, "g3")]
    assert MG.split_recount_patches(v1, groups[:2], "electrical_drawings")[0] == v1
    out = {"counts": [{"row_id": "C9", "fingerprint": "fpA"}, {"row_id": "C3", "fingerprint": "fpB"}],
           "merge_log": {"patches": [{"target_row_id": "C9"}]}}
    assert MG.restore_prior_ids(out, {"fpA": ["C1"], "fpB": ["C3"]}) == {"C9": "C1"}
    assert out["counts"][0]["row_id"] == "C1" and out["merge_log"]["patches"][0]["target_row_id"] == "C1"
    assert MG.restore_prior_ids(out, None) == {}


# Text layer: vector_text is not usable ------------------------------------------------------


def test_vector_text_page_has_no_floor_and_uses_the_tile_rule():
    sub = _group("g1", "sub", 0, [_count("Remove existing duplex receptacle", 10)], created="2026-01-01T00:00:00")
    tags = {"status": {"X": [[0.2, 0.2], [0.3, 0.3], [0.8, 0.8]]}, "circuit": [], "type_marks": {}, "panel_labels": []}
    vec = {("d1", 1): {"page": 1, "w_in": 42, "h_in": 30, "text_class": "vector_text", "text_layer_quality": "vector_text",
                       "roles": ["plan"], "views": [], "regions": [], "tags": tags}}
    assert MG._page_text_usable(vec[("d1", 1)]) is False
    assert MG._page_text_usable({"text_class": "scanned_ocr"}) is True and MG._page_text_usable({"text_class": "sparse"}) is False
    # whole tiles on a page without a usable text layer: the 0.98 rule makes the claim a sheet total
    esc = _group("g2", "escalation", 0, [_count("Remove existing duplex receptacle", 12, coverage="whole_sheet", supersedes_row_id="c1")],
                 created="2026-01-01T00:10:00", tiles=[[0.0, 0.0, 1.0, 1.0]])
    v2 = MG.consolidate_category("electrical_drawings", [sub, esc], words_by_page=vec, documents=DOCS)
    row = [r for r in v2["counts"] if r["status"] == "active"][0]
    assert row["coverage"] == "sheet_total" and row["quantity"] == 12 and v2["merge_log"]["patches"][0]["action"] == "replaced"
    assert v2["floors"] and all(f["floor"] is None and f["method"] == "unavailable" for f in v2["floors"])
    # half tiles: a tile subtotal
    esc_half = _group("g2", "escalation", 0, [_count("Remove existing duplex receptacle", 12, coverage="whole_sheet", supersedes_row_id="c1")],
                      created="2026-01-01T00:10:00", tiles=[[0.0, 0.0, 0.5, 1.0]])
    v2 = MG.consolidate_category("electrical_drawings", [sub, esc_half], words_by_page=vec, documents=DOCS)
    row = [r for r in v2["counts"] if r["status"] == "active"][0]
    assert row["quantity"] == 12 and row["coverage"] == "partial"  # raised by a floor, never a total


# Legacy view (flags off: the main prompt reads today's v1) -----------------------------------


def test_legacy_view_rebuilds_todays_v1_from_the_groups(run7154, merged):
    from app.services.plans_reader_runs import _category_output

    theirs, _ = _category_output("electrical_drawings", run7154["groups"])
    ours = MG.legacy_view(merged, groups=run7154["groups"])
    assert json.dumps(ours, sort_keys=True) == json.dumps(theirs, sort_keys=True)
    # without groups: the v2-only keys stripped (a v1 shape, not today's bytes: patches applied by code)
    shape = MG.legacy_view(merged)
    assert not set(MG.V2_OUTPUT_KEYS) & set(shape) and "escalation_patches" not in shape
    for key, _p in R.ROW_KINDS:
        for row in shape.get(key) or []:
            assert not set(MG.V2_ROW_KEYS) & set(row)
            assert "model_coverage" not in row and "sheet_number_printed" not in row
    assert json.dumps(shape, sort_keys=True) != json.dumps(theirs, sort_keys=True)
    v1 = MG.category_v1_output("electrical_drawings", run7154["groups"], llm_rollup=True)[0]
    assert MG.legacy_view(v1) == v1 and MG.legacy_view(v1) is not v1
    with pytest.raises(MG.MergeError):
        MG.legacy_view("nope")


# Chained replay: plan -> merge -> gate at $0 ------------------------------------------------


@pytest.mark.golden
def test_replay_plan_merge_gate_evaluates_census_floor():
    """The WS1 plan stage writes `documents` (facts_status done, facts_doc)
    and no facts artifact; the merge stage reads the facts of those
    documents, so the gate stage evaluates census_floor on the 7154 bundle."""
    from tests.test_pagefacts_integration import BUNDLE, ELEC_PDF, FLAGS_ON, MECH_PDF

    if not (BUNDLE.is_dir() and ELEC_PDF.exists() and MECH_PDF.exists()):
        pytest.skip("7154 bundle or backup PDFs not on this machine")
    from app.services import eval_bundle
    from app.services import plans_reader_gates as G
    from app.services import plans_reader_replay as rp
    from app.services.pagefacts import replay as pf_replay

    bundle = eval_bundle.load_bundle(BUNDLE)
    saved = dict(rp._STAGES)
    try:
        pf_replay.register()
        MG.register_replay_stage()
        G.register_replay_stage()
        result = rp.replay(
            bundle, from_stage="plan", to_stage="gate", out_dir=None,
            settings_overrides=dict(FLAGS_ON, plans_reader_gates_blocking=True),
            stages={"recount": "skip", "derive": "skip", "consolidate": "skip", "enforce": "skip"},
        )
    finally:
        rp._STAGES.clear()
        rp._STAGES.update(saved)
    for name in ("plan", "merge", "gate"):
        assert result.stage(name).error is None, (name, result.stage(name).error)
    art = result.artifacts
    assert "page_facts" not in art and all(d.get("facts_status") == "done" for d in art["documents"] if d.get("status") != "failed")
    elec = art["category:electrical_drawings"]
    assert R.is_v2(elec) and elec["merge_log"]["conservation"]["ok"]
    floors = {(f["sheet_number"], f["key"]): f for f in elec["floors"] if f.get("floor") is not None}
    assert floors[("ED3.01", "demo_x")]["floor"] == 39 and floors[("ED2.01", "demo_x")]["floor"] == 34
    assert any(n.startswith("merge: ") and "facts pages from the plan stage documents" in n for n in result.notes)
    report = art["gate_report"]
    summary = art["gate_summary"]
    assert "census_floor" in summary["evaluated"] and "census_floor" not in summary["not_applicable"]
    census = [c for c in report["checks"] if c["code"] == "census_floor"]
    assert census, report["checks"]
    findings = [f for f in art["gate_findings"] if f["gate_id"] == "census_floor"]
    assert any(f["target"]["family"] == "demo_x" and f["target"]["sheet_key"] for f in findings)


# Run-unique ids across categories (V1 hook 1, spec 5.1) ---------------------------------------

BUNDLE_7172 = Path("/Users/thomasmoore/Desktop/BDR/backups/eval/bundles/7172/785ae29e")
DOCS_TWO = DOCS + [{"id": "d2", "category": "general_drawings", "created_at": "2026-01-02"}]


def _general_group(gid: str, counts: list[dict], *, created: str = "2026-01-02T00:00:00") -> dict:
    g = _group(gid, "sub", 0, counts, created=created)
    g["category"] = "general_drawings"
    g["pages"][0]["document_id"] = "d2"
    g["output"]["category"] = "general_drawings"
    g["output"]["pages"][0].update({"document_id": "d2", "doc_index": 2})
    for c in g["output"]["counts"]:
        c.update({"document_id": "d2", "doc_index": 2, "sheet_number": "G0.01"})
    return g


def _all_ids(*outputs: dict) -> list[str]:
    return [r["row_id"] for o in outputs for k, _p in R.ROW_KINDS for r in o.get(k) or []]


def test_consolidate_category_continues_the_run_sequence_from_the_stored_outputs(run7154, merged):
    """A later category merged with `run_outputs` (the outputs already
    stored for the run) continues their high-water mark instead of
    starting at C1 again; without them the first category still merges
    from 1 (the 7154 fixture ids stay C1..C56)."""
    gen_groups = [_general_group("g-gen", [_count("Fire extinguisher cabinet", 3, tag=None), _count("Knox box", 1, tag=None)])]
    alone = MG.consolidate_category("general_drawings", gen_groups, documents=DOCS_TWO)
    assert [r["row_id"] for r in alone["counts"]] == ["C1", "C2"]
    assert [r["row_id"] for r in merged["counts"]] == [f"C{i}" for i in range(1, 57)]

    gen = MG.consolidate_category("general_drawings", gen_groups, documents=DOCS_TWO, run_outputs=[merged])
    assert [r["row_id"] for r in gen["counts"]] == ["C57", "C58"] and gen["row_seq"]["C"] == 58
    ids = _all_ids(merged, gen)
    assert len(ids) == len(set(ids))
    assert MG.run_sequence([merged, gen]).as_dict()["C"] == 58
    # a v1 output (no ids yet) seeds nothing; an explicit row_seq wins over run_outputs
    assert MG.run_sequence([run7154["rfp"]]).as_dict() == {}
    forced = MG.consolidate_category("general_drawings", gen_groups, documents=DOCS_TWO, run_outputs=[merged], row_seq=R.RowSeq({"C": 100}))
    assert [r["row_id"] for r in forced["counts"]] == ["C101", "C102"]

    # a re-merge of the later category (prior) keeps its ids and a new row
    # continues after every stored output, this category's own included
    more = copy.deepcopy(gen_groups)
    more[0]["output"]["counts"].append(_count("Fire department connection sign", 1, tag=None, document_id="d2", doc_index=2, sheet_number="G0.01"))
    again = MG.consolidate_category("general_drawings", more, documents=DOCS_TWO, prior=gen, run_outputs=[merged, gen])
    assert [r["row_id"] for r in again["counts"]] == ["C57", "C58", "C59"]
    assert len(set(_all_ids(merged, again))) == len(_all_ids(merged, again))


def test_replay_merge_shares_one_sequence_across_categories(run7154, elec_pages):
    """The replay merge stage numbers every category of a bundle on one
    sequence in vocabulary order: the electrical rows keep C1..C56 (first
    in rank among the fixture's categories), specifications and rfp follow,
    and no id is issued twice."""
    MG.register_replay_stage()

    class _Ctx:
        def __init__(self):
            self.notes = []

        def note(self, text):
            self.notes.append(text)

    art = {
        "groups": run7154["groups"], "documents": run7154["documents"], "ledger": run7154["run"]["ledger"],
        "category:rfp": run7154["rfp"], "category:electrical_drawings": run7154["electrical"],
        "category:specifications": run7154["specifications"], "page_facts": elec_pages,
    }
    out = MG.replay_merge(_Ctx(), art)
    assert list(out) == ["category:electrical_drawings", "category:specifications", "category:rfp"]
    elec, specs, rfp = out["category:electrical_drawings"], out["category:specifications"], out["category:rfp"]
    assert [r["row_id"] for r in elec["counts"]] == [f"C{i}" for i in range(1, 57)]
    ids = _all_ids(elec, specs, rfp)
    assert len(ids) == len(set(ids)) and len(ids) == 56 + 4 + 111 + 73 + 61 + 19
    checked = 0
    for prefix in "OPQS":
        spec_ids = [int(r["row_id"][1:]) for k, p in R.ROW_KINDS if p == prefix for r in specs[k]]
        rfp_ids = [int(r["row_id"][1:]) for k, p in R.ROW_KINDS if p == prefix for r in rfp[k]]
        if spec_ids:
            assert min(spec_ids) > elec["row_seq"].get(prefix, 0)
        if spec_ids and rfp_ids:
            assert min(rfp_ids) > max(spec_ids)
            checked += 1
    assert checked >= 2
    assert R.index_rows([elec, specs, rfp]).keys() == set(ids)
    assert [CATEGORIES.index(k.split(":", 1)[1]) for k in out] == sorted(CATEGORIES.index(k.split(":", 1)[1]) for k in out)


@pytest.mark.golden
@pytest.mark.skipif(not BUNDLE_7172.is_dir(), reason="7172 bundle 785ae29e not on this machine")
def test_7172_bundle_remerged_on_one_sequence_has_no_id_collisions():
    """Measured on the stored 7172 run (bundle 785ae29e): every stored
    category output was merged from 1 (general C1-C5, mechanical C1-C20,
    electrical C1-C62: 25 ids held twice, the electrical schedule counts,
    panels, transformer and breaker rows dropped by index_rows). Re-merged
    from the bundle's groups with each category seeded from the outputs
    already produced, the 87 count rows hold 87 distinct ids and a main
    session built on them has no collision alias."""
    from app.services import plans_reader_consolidator as C
    from app.services.eval_bundle import load_bundle

    bundle = load_bundle(BUNDLE_7172)
    stored = {c: bundle.category_output(c) for c in bundle.categories}
    stored_ids = [r["row_id"] for o in stored.values() for r in o["counts"]]
    assert len(stored_ids) == 87 and len(set(stored_ids)) == 62  # the collision as stored
    rank = {c: i for i, c in enumerate(CATEGORIES)}
    rebuilt: dict[str, dict] = {}
    for cat in sorted(stored, key=lambda c: rank[c]):
        v2 = MG.consolidate_category(
            cat, bundle.groups, ledger=bundle.run.get("ledger"), documents=bundle.documents, llm_rollup=False,
            run_outputs=list(rebuilt.values()),
        )
        assert v2 is not None, cat
        rebuilt[cat] = v2
    ids = _all_ids(*rebuilt.values())
    assert len(ids) == len(set(ids))
    counts = {c: sum(1 for r in o["counts"] if r["status"] == "active") for c, o in rebuilt.items()}
    assert counts == {"general_drawings": 5, "mechanical_drawings": 20, "electrical_drawings": 62}
    assert len(R.index_rows(list(rebuilt.values()))) == len(ids)
    rows, aliases = C.index_sources(list(rebuilt.values()), None)
    assert aliases == {} and len([r for r in rows if r[0] == "C"]) == 87

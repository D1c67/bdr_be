"""plans_reader_rows: category output v2, row ids, fingerprints, coverage
(build spec 5.1, 5.4, WS4). Runs on the committed Q27 fixtures of run
ff8fad25 (7154) under tests/fixtures/plans_reader/run_7154; the one test that
compares the fixtures with their source bundle needs EVAL_ROOT and is golden.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re

import pytest

from app.services import plans_reader_rows as rows
from app.services.estimating_contracts import parse_row_id

from tests.plans_reader_fixtures import (
    ELEC_DOC_7154,
    EVAL_BUNDLE_7154,
    FIXTURE_7154,
    MECH_DOC_7154,
    load_7154,
    load_json,
    rows_meta,
)

FIXTURE = FIXTURE_7154
EVAL_BUNDLE = EVAL_BUNDLE_7154
ELEC_DOC = ELEC_DOC_7154
MECH_DOC = MECH_DOC_7154
_load = load_json
_meta = rows_meta


@pytest.fixture(scope="module")
def run7154() -> dict:
    return load_7154()


@pytest.fixture(scope="module")
def elec_v2(run7154) -> dict:
    return rows.upgrade_category_output(run7154["electrical"], _meta(run7154))


def _by_sheet(v2: dict, sheet_number: str) -> list[dict]:
    return [r for r in v2["counts"] if r["sheet"]["sheet_number"] == sheet_number]


# Fixtures (Q27) -------------------------------------------------------------------------


def test_fixture_row_counts(run7154):
    cat = run7154["electrical"]
    assert len(cat["counts"]) == 54
    assert len(cat["runs"]) == 0
    assert len(cat["schedules"]) == 14
    assert len(cat["questions"]) == 36
    assert len(cat["escalation_patches"]) == 3
    assert sum(len(p["counts"]) for p in cat["escalation_patches"]) == 4
    subs = [g for g in run7154["groups"] if g["kind"] == "sub" and g["category"] == "electrical_drawings"]
    assert sum(len(g["output"]["counts"]) for g in subs) == 54
    manifest = _load("MANIFEST.json")
    assert manifest["row_counts"]["sub_count_rows"] == 54
    assert manifest["row_counts"]["category_electrical_drawings"]["counts"] == 54


_PRICE_KEY = re.compile(r"(?i)(^|_)(price|prices|cost|costs|usd|amount|amounts|dollars?)(_|$)")


def _walk_keys(node, out: list[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            out.append(k)
            _walk_keys(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_keys(v, out)


def test_fixture_has_no_prices_drawings_or_raw_dashes():
    files = sorted(p for p in FIXTURE.rglob("*") if p.is_file())
    assert files, "fixtures missing"
    for path in files:
        assert path.suffix == ".json", f"only trimmed JSON is committed: {path.name}"
        text = path.read_text()
        assert chr(0x2014) not in text and chr(0x2013) not in text, path.name
        assert not re.search(r"\$\s?\d", text), f"dollar figure in {path.name}"
        keys: list[str] = []
        _walk_keys(json.loads(text), keys)
        assert not [k for k in keys if _PRICE_KEY.search(k)], path.name


@pytest.mark.golden
@pytest.mark.skipif(not EVAL_BUNDLE.is_dir(), reason="EVAL_ROOT bundle not on this machine")
def test_fixture_sources_match_the_eval_bundle():
    manifest = _load("MANIFEST.json")
    for rel, digest in manifest["sources_sha256"].items():
        assert hashlib.sha256((EVAL_BUNDLE / rel).read_bytes()).hexdigest() == digest, rel


# Conservation and patches on 7154 ----------------------------------------------------------


def test_conservation_all_54_sub_rows_stay_active(run7154, elec_v2):
    assert elec_v2["rows_version"] == 2
    first_pass = [r for r in elec_v2["counts"] if r["origin"]["group_kind"] == "sub"]
    assert len(first_pass) == 54
    assert all(r["status"] == "active" for r in first_pass)
    sub_ids = {g["id"] for g in run7154["groups"] if g["kind"] == "sub"}
    assert {r["origin"]["group_id"] for r in first_pass} <= sub_ids
    # every sub group row is an active row with its own description and quantity
    active = {(r["document_id"], r["page"], r["description"], r["quantity"]) for r in first_pass}
    for g in run7154["groups"]:
        if g["kind"] == "sub" and g["category"] == "electrical_drawings":
            for c in g["output"]["counts"]:
                assert (c["document_id"], c["page"], c["description"], c["quantity"]) in active
    log = elec_v2["merge_log"]
    for kind, n in log["inputs"].items():
        out_rows = elec_v2[kind]
        active_n = sum(1 for r in out_rows if r["status"] == "active")
        alts = sum(len(r["alternates"]) for r in out_rows)
        sup = sum(1 for r in out_rows for s in r["superseded"] if s["reason"] != rows.RAISED_REASON)
        assert n == active_n + alts + sup + log["dropped_exact_duplicates"], kind
    assert log["inputs"]["counts"] == 58
    assert log["outputs"]["counts"] == 56
    assert log["alternates"] == 2


def test_patch_tile_subtotal_never_a_total_7154(elec_v2):
    ed3 = _by_sheet(elec_v2, "ED3.01")
    rect = [r for r in ed3 if "rectangular" in r["description"]]
    assert len(rect) == 1
    rect = rect[0]
    assert rect["quantity"] == 5
    assert rect["coverage"] == "tile_subtotal"
    assert "needs_recount" in rect["flags"]
    assert rect["origin"]["group_kind"] == "escalation"
    assert rect["status"] == "active"


def test_legacy_patches_land_in_alternates_7154(elec_v2):
    ed2 = _by_sheet(elec_v2, "ED2.01")
    filled = [r for r in ed2 if r["description"] == "Remove existing duplex receptacle, filled symbol"]
    assert len(filled) == 1
    row = filled[0]
    assert row["quantity"] == 9 and row["coverage"] == "sheet_total"
    assert len(row["alternates"]) == 1
    assert row["alternates"][0]["coverage"] == "tile_subtotal"
    assert row["alternates"][0]["description"].startswith("Remove existing receptacles, hatched")
    track = [r for r in _by_sheet(elec_v2, "ED3.01") if "paired track-light head" in r["description"]][0]
    assert track["quantity"] == 12 and len(track["alternates"]) == 1
    actions = [(p["action"], p["matched_by"]) for p in elec_v2["merge_log"]["patches"]]
    assert actions.count(("added", "none")) == 2
    assert actions.count(("alternate", "corrects_text")) == 2
    assert all(p["target_row_id"] for p in elec_v2["merge_log"]["patches"])


def test_escalation_patches_are_not_stored_in_v2(elec_v2):
    assert "escalation_patches" not in elec_v2
    assert elec_v2["unresolved_flags"] == []
    assert elec_v2["floors"] == []
    ed3_page = [p for p in elec_v2["pages"] if p["document_id"] == ELEC_DOC and p["page"] == 8][0]
    assert len(ed3_page["notes"]) >= 1


def test_linear_and_family_flags_7154(elec_v2):
    track_row = [r for r in _by_sheet(elec_v2, "ED3.01") if r["description"] == "Remove existing lighting track row"][0]
    assert "linear_in_ea" in track_row["flags"]
    assert "demo_x@D1P8" in track_row["family_tags"]
    demo_ed2 = [r for r in _by_sheet(elec_v2, "ED2.01") if "demo_x@D1P7" in r["family_tags"]]
    assert sum(r["quantity"] for r in demo_ed2) == 34


# Ids --------------------------------------------------------------------------------------


def test_row_ids_unique_canonical_and_per_prefix(elec_v2):
    seen: set[str] = set()
    for key, prefix in rows.ROW_KINDS:
        for r in elec_v2[key]:
            p, n = parse_row_id(r["row_id"])
            assert p == prefix and n >= 1
            assert r["row_id"] not in seen
            seen.add(r["row_id"])
    assert elec_v2["row_seq"]["C"] == 56
    assert [r["row_id"] for r in elec_v2["counts"]] == [f"C{i}" for i in range(1, 57)]


def test_row_ids_deterministic(run7154):
    a = rows.upgrade_category_output(run7154["electrical"], _meta(run7154))
    b = rows.upgrade_category_output(copy.deepcopy(run7154["electrical"]), _meta(run7154))
    assert [(r["row_id"], r["fingerprint"]) for r in a["counts"]] == [(r["row_id"], r["fingerprint"]) for r in b["counts"]]


def test_row_ids_never_reused_across_categories_and_remerge(run7154):
    seq = rows.RowSeq()
    elec = rows.upgrade_category_output(run7154["electrical"], _meta(run7154), row_seq=seq)
    rfp = rows.upgrade_category_output(run7154["rfp"], _meta(run7154), row_seq=seq)
    specs = rows.upgrade_category_output(run7154["specifications"], _meta(run7154), row_seq=seq)
    ids = [r["row_id"] for out in (elec, rfp, specs) for k, _p in rows.ROW_KINDS for r in out[k]]
    assert len(ids) == len(set(ids))
    assert min(int(r["row_id"][1:]) for r in rfp["scope"]) > elec["row_seq"]["O"]

    # A re-merge with prior keeps every id; a new row takes the next id.
    v1 = copy.deepcopy(run7154["electrical"])
    extra = copy.deepcopy(v1["counts"][0])
    extra["description"] = "New recount row"
    v1["counts"].append(extra)
    seq2 = rows.RowSeq(elec["row_seq"])
    again = rows.upgrade_category_output(v1, _meta(run7154), row_seq=seq2, prior=elec)
    before = {r["fingerprint"]: r["row_id"] for r in elec["counts"]}
    for r in again["counts"]:
        if r["fingerprint"] in before:
            assert r["row_id"] == before[r["fingerprint"]]
    new = [r for r in again["counts"] if r["description"] == "New recount row"][0]
    assert new["row_id"] == "C57"

    # A row dropped on a later merge never gives its id to another row.
    v1b = copy.deepcopy(run7154["electrical"])
    v1b["counts"] = v1b["counts"][1:]
    seq3 = rows.RowSeq(again["row_seq"])
    third = rows.upgrade_category_output(v1b, _meta(run7154), row_seq=seq3, prior=again)
    assert "C1" not in {r["row_id"] for r in third["counts"]}
    assert seq3.next("C") == "C58"


def test_rowseq_observe_and_next():
    seq = rows.RowSeq({"C": 3})
    seq.observe("C9")
    seq.observe("not an id")
    assert seq.next("C") == "C10"
    assert seq.next("B") == "B1"
    assert seq.as_dict() == {"B": 1, "C": 10}


def test_v2_output_passes_through(elec_v2):
    seq = rows.RowSeq()
    again = rows.upgrade_category_output(elec_v2, None, row_seq=seq)
    assert [r["row_id"] for r in again["counts"]] == [r["row_id"] for r in elec_v2["counts"]]
    assert seq.next("C") == "C57"


# Sheets ---------------------------------------------------------------------------------------


def test_sheet_key_format_d1p8(run7154, elec_v2):
    for key, _p in rows.ROW_KINDS:
        for r in elec_v2[key]:
            assert rows.SHEET_KEY_RE.match(r["sheet"]["sheet_key"]), r["sheet"]
    ed3 = _by_sheet(elec_v2, "ED3.01")
    assert ed3 and {r["sheet"]["sheet_key"] for r in ed3} == {"D1P8"}
    mech = [r for r in elec_v2["counts"] if r["document_id"] == MECH_DOC]
    assert mech and all(r["sheet"]["sheet_key"].startswith("D2P") for r in mech)
    assert rows.parse_sheet_key("D1P8") == (1, 8)
    with pytest.raises(Exception):
        rows.parse_sheet_key("D0P8")


def test_doc_order_matches_agents_run_doc_order(run7154):
    from app.services.plans_reader_agents import run_doc_order

    expected = {d["id"]: n for n, d in enumerate(run_doc_order(run7154["documents"]), start=1)}
    assert rows.doc_order(run7154["documents"]) == expected
    assert expected[ELEC_DOC] == 1 and expected[MECH_DOC] == 2


def test_sheet_number_repaired_only_against_ledger():
    out = {
        "category": "electrical_drawings",
        "counts": [
            {"description": "Duplex receptacle", "quantity": 4, "unit": "EA", "document_id": "d1",
             "page": 1, "doc_index": 1, "sheet_number": "E101", "basis": "counted_symbols"},
            {"description": "Quad receptacle", "quantity": 2, "unit": "EA", "document_id": "d1",
             "page": 2, "doc_index": 1, "sheet_number": "E-201", "basis": "counted_symbols"},
        ],
    }
    v2 = rows.upgrade_category_output(out, {"sheet_numbers": {("d1", 1): "E-101", "d1:2": "E-202"}})
    a, b = v2["counts"]
    assert "sheet_number_repaired" not in a["flags"] and a["sheet"]["sheet_number"] == "E-101"
    assert "sheet_number_repaired" in b["flags"]
    assert b["sheet_number"] == "E-202" and b["sheet_number_printed"] == "E-201"
    assert a["sheet"]["sheet_key"] == "D1P1"


# Coverage rules ---------------------------------------------------------------------------------


def _count(**kw):
    row = {"description": "Duplex receptacle", "quantity": 3, "unit": "EA", "basis": "counted_symbols",
           "notes": None, "location": None, "document_id": "d1", "page": 1}
    row.update(kw)
    return row


def test_coverage_sub_rows():
    assert rows.infer_coverage("counts", _count()) == "sheet_total"
    assert rows.infer_coverage("counts", _count(notes="New tile subtotal only.")) == "partial"
    assert rows.infer_coverage("counts", _count(location="partial total inside the tile only")) == "partial"
    assert rows.infer_coverage("counts", _count(notes="This is not a complete sheet total")) == "partial"
    assert rows.infer_coverage("counts", _count(basis="schedule_text")) == "schedule"
    assert rows.infer_coverage("counts", _count(basis="stated_quantity")) == "stated"
    assert rows.infer_coverage("counts", _count(basis="inferred")) == "derived"
    tiled = {"tiles": [[0, 0, 0.5, 1]]}
    assert rows.infer_coverage("counts", _count(), page_item=tiled) == "tile_subtotal"
    full = {"tiles": [[0, 0, 0.6, 1], [0.5, 0, 1, 1]]}
    assert rows.infer_coverage("counts", _count(), page_item=full) == "sheet_total"
    assert rows.infer_coverage("counts", _count(), group_kind="rollup") == "sheet_total"


def test_coverage_escalation_and_recount_rows():
    complete = {"tiles": [[0.1, 0.1, 0.3, 0.3]], "containment": {"targets": 39, "inside": 39}}
    incomplete = {"tiles": [[0.1, 0.1, 0.3, 0.3]], "containment": {"targets": 39, "inside": 22}}
    for kind in ("escalation", "recount"):
        whole = _count(coverage="whole_sheet")
        assert rows.infer_coverage("counts", whole, group_kind=kind, page_item=complete) == "sheet_total"
        assert rows.infer_coverage("counts", whole, group_kind=kind, page_item=incomplete) == "tile_subtotal"
        assert rows.infer_coverage("counts", whole, group_kind=kind, page_item={"tiles": [[0, 0, 1, 1]]}) == "tile_subtotal"
        inside = _count(coverage="inside_tiles_only")
        assert rows.infer_coverage("counts", inside, group_kind=kind, page_item=complete) == "tile_subtotal"
        legacy = _count()  # no coverage field: inside_tiles_only
        assert rows.infer_coverage("counts", legacy, group_kind=kind, page_item=complete) == "tile_subtotal"
        says = _count(coverage="whole_sheet", notes="tile subtotal")
        assert rows.infer_coverage("counts", says, group_kind=kind, page_item=complete) == "tile_subtotal"


def test_coverage_scanned_pages_use_the_tile_rule():
    whole = _count(coverage="whole_sheet")
    near_full = {"tiles": [[0, 0, 0.995, 1]]}
    half = {"tiles": [[0, 0, 0.5, 1]], "containment": {"targets": 3, "inside": 3}}
    assert rows.infer_coverage("counts", whole, group_kind="escalation", page_item=near_full, text_layer=False) == "sheet_total"
    assert rows.infer_coverage("counts", whole, group_kind="escalation", page_item=half, text_layer=False) == "tile_subtotal"
    # with a text layer the containment decides, not the tile area
    assert rows.infer_coverage("counts", whole, group_kind="escalation", page_item=near_full, text_layer=True) == "tile_subtotal"


def test_coverage_other_kinds():
    assert rows.infer_coverage("schedules", {}) == "schedule"
    assert rows.infer_coverage("specs", {}) == "stated"
    assert rows.infer_coverage("scope", {}) == "stated"
    assert rows.infer_coverage("questions", {}) == "unknown"
    assert rows.infer_coverage("runs", {"basis": "stated"}) == "stated"
    assert rows.infer_coverage("runs", {"basis": "assumed"}) == "derived"
    assert rows.infer_coverage("runs", {"basis": "scaled_from_drawing"}) == "sheet_total"


def test_tiles_union_fraction():
    assert rows.tiles_union_fraction([]) == 0.0
    assert rows.tiles_union_fraction([[0, 0, 1, 1]]) == pytest.approx(1.0)
    assert rows.tiles_union_fraction([[0, 0, 0.5, 0.5], [0.25, 0.25, 0.75, 0.75]]) == pytest.approx(0.4375)
    assert rows.tiles_union_fraction([[0, 0, 0.5, 1], [0.5, 0, 1, 1]]) == pytest.approx(1.0)
    assert rows.tiles_union_fraction([[-1, -1, 2, 0.5]]) == pytest.approx(0.5)


# Synthetic patch rules ------------------------------------------------------------------------------


def _patched_output(patch_count: dict, *, containment: dict | None = None) -> tuple[dict, dict]:
    sub = {"id": "g-sub", "kind": "sub", "category": "electrical_drawings", "pass": 1, "group_index": 0,
           "created_at": "2026-01-01T00:00:00", "pages": [{"document_id": "d1", "page": 1, "kind": "image", "tiles": None}]}
    esc_page = {"document_id": "d1", "page": 1, "kind": "image", "tiles": [[0.1, 0.1, 0.4, 0.4]]}
    if containment is not None:
        esc_page["containment"] = containment
    esc = {"id": "g-esc", "kind": "escalation", "category": "electrical_drawings", "pass": 2, "group_index": 0,
           "created_at": "2026-01-01T00:10:00", "pages": [esc_page]}
    first = _count(description="Remove existing duplex receptacle", quantity=10, symbol_or_tag="(X)")
    other = _count(description="Remove existing quad receptacle", quantity=4, symbol_or_tag="(X)")
    output = {
        "category": "electrical_drawings",
        "counts": [first, other],
        "pages": [{"document_id": "d1", "page": 1, "notes": [], "tags": []}],
        "escalation_patches": [{"pages": [{"document_id": "d1", "page": 1, "notes": ["patched"]}], "counts": [patch_count]}],
    }
    meta = {"groups": [sub, esc], "documents": [{"id": "d1", "category": "electrical_drawings", "created_at": "x"}]}
    return output, meta


def test_whole_sheet_patch_with_full_containment_replaces_and_keeps_the_id():
    patch = _count(description="Remove existing duplex receptacle", quantity=12, coverage="whole_sheet", symbol_or_tag="(X)")
    output, meta = _patched_output(patch, containment={"targets": 12, "inside": 12})
    base = copy.deepcopy(output)
    base["escalation_patches"] = []
    before = rows.upgrade_category_output(base, meta)
    after = rows.upgrade_category_output(output, meta, prior=before)
    row = [r for r in after["counts"] if "duplex" in r["description"]][0]
    assert row["quantity"] == 12 and row["coverage"] == "sheet_total"
    assert row["origin"]["group_kind"] == "escalation"
    assert row["superseded"][0]["quantity"] == 10
    assert row["row_id"] == [r for r in before["counts"] if "duplex" in r["description"]][0]["row_id"]
    assert after["merge_log"]["patches"][0]["action"] == "replaced"
    assert sum(1 for r in after["counts"] if r["status"] == "active") == 2


def test_patch_floor_raises_never_lowers():
    raise_patch = _count(description="Remove existing duplex receptacle", quantity=14, symbol_or_tag="(X)")
    out, meta = _patched_output(raise_patch, containment={"targets": 20, "inside": 9})
    v2 = rows.upgrade_category_output(out, meta)
    row = [r for r in v2["counts"] if "duplex" in r["description"]][0]
    assert row["quantity"] == 14 and row["coverage"] == "partial" and "needs_recount" in row["flags"]
    assert row["superseded"][-1]["quantity"] == 10
    assert v2["merge_log"]["patches"][0]["action"] == "raised"

    lower_patch = _count(description="Remove existing duplex receptacle", quantity=6, coverage="whole_sheet", symbol_or_tag="(X)")
    out, meta = _patched_output(lower_patch, containment={"targets": 20, "inside": 9})
    v2 = rows.upgrade_category_output(out, meta)
    row = [r for r in v2["counts"] if "duplex" in r["description"]][0]
    assert row["quantity"] == 10 and row["coverage"] == "sheet_total"
    assert row["alternates"][0]["quantity"] == 6
    assert v2["merge_log"]["patches"][0]["action"] == "alternate"

    # complete containment makes the patch a whole-sheet count, yet it still
    # cannot lower: it goes to alternates and the target waits for a recount
    out, meta = _patched_output(lower_patch, containment={"targets": 20, "inside": 20})
    v2 = rows.upgrade_category_output(out, meta)
    row = [r for r in v2["counts"] if "duplex" in r["description"]][0]
    assert row["quantity"] == 10 and row["status"] == "active"
    assert row["origin"]["group_kind"] == "sub" and not row.get("superseded")
    assert row["alternates"][0]["quantity"] == 6 and row["alternates"][0]["coverage"] == "sheet_total"
    assert "needs_recount" in row["flags"]
    assert v2["merge_log"]["patches"][0]["action"] == "alternate"
    assert sum(1 for r in v2["counts"] if r["status"] == "active") == 2

    # an equal whole-sheet patch with complete containment still replaces
    equal = _count(description="Remove existing duplex receptacle", quantity=10, coverage="whole_sheet", symbol_or_tag="(X)")
    out, meta = _patched_output(equal, containment={"targets": 10, "inside": 10})
    v2 = rows.upgrade_category_output(out, meta)
    row = [r for r in v2["counts"] if "duplex" in r["description"]][0]
    assert row["quantity"] == 10 and row["origin"]["group_kind"] == "escalation"
    assert v2["merge_log"]["patches"][0]["action"] == "replaced"


def test_patch_by_supersedes_local_id_and_ambiguous():
    patch = _count(description="Remove existing receptacles of every kind", quantity=4, supersedes_row_id="c2", symbol_or_tag="(X)")
    out, meta = _patched_output(patch)
    v2 = rows.upgrade_category_output(out, meta)
    assert v2["merge_log"]["patches"][0]["matched_by"] == "supersedes_row_id"
    quad = [r for r in v2["counts"] if "quad" in r["description"]][0]
    assert quad["alternates"][0]["quantity"] == 4

    # corrects-first-pass text finds the closest row when it is clearly best
    near = _count(description="Remove receptacles", quantity=3,
                  notes="corrects first pass: Remove existing receptacle, quad. More text", symbol_or_tag="(X)")
    out, meta = _patched_output(near)
    v2 = rows.upgrade_category_output(out, meta)
    entry = v2["merge_log"]["patches"][0]
    assert entry["matched_by"] == "corrects_similarity" and entry["action"] == "alternate"
    assert [r for r in v2["counts"] if "quad" in r["description"]][0]["alternates"]

    # two first-pass rows with the patch's description: ambiguous, added as its own row
    amb = _count(description="Remove existing quad receptacle", quantity=3, symbol_or_tag="(X)")
    out, meta = _patched_output(amb)
    out["counts"].append(_count(description="Remove existing quad receptacle", quantity=1, symbol_or_tag="(X)2"))
    v2 = rows.upgrade_category_output(out, meta)
    entry = v2["merge_log"]["patches"][0]
    assert entry["action"] == "added_ambiguous"
    added = [r for r in v2["counts"] if r["origin"]["group_kind"] == "escalation"][0]
    assert {"ambiguous_patch", "needs_recount"} <= set(added["flags"])
    assert added["coverage"] == "tile_subtotal"
    assert all(r["status"] == "active" for r in v2["counts"])


def test_unverifiable_rows():
    out = {
        "category": "electrical_drawings",
        "counts": [
            _count(page=1),
            _count(page=5, description="Row on a page no group read"),
            _count(page=1, description="Negative", quantity=-2),
        ],
    }
    sub = {"id": "g1", "kind": "sub", "category": "electrical_drawings", "pass": 1,
           "pages": [{"document_id": "d1", "page": 1, "kind": "image"}]}
    v2 = rows.upgrade_category_output(out, {"groups": [sub]})
    status = {r["description"]: (r["status"], r["flags"]) for r in v2["counts"]}
    assert status["Duplex receptacle"][0] == "active"
    assert status["Row on a page no group read"][0] == "unverifiable"
    assert "page_not_read" in status["Row on a page no group read"][1]
    assert status["Negative"][0] == "unverifiable"


def test_points_win_over_location_positions():
    out = {"category": "electrical_drawings", "counts": [
        _count(location="at (0.1,0.2)", points=[[0.31234, 0.4], [2, 0.5], "bad"]),
        _count(description="Quad receptacle", location="at (0.1,0.2)", points=None),
    ]}
    v2 = rows.upgrade_category_output(out)
    assert v2["counts"][0]["positions"] == [[0.3123, 0.4]]
    assert v2["counts"][1]["positions"] == [[0.1, 0.2]]


def test_parse_positions():
    assert rows.parse_positions("One-line at (0.42,0.43) (0.42,0.56)") == [[0.42, 0.43], [0.42, 0.56]]
    assert rows.parse_positions("centers: (0.224, 0.511), (.367,.494)") == [[0.224, 0.511], [0.367, 0.494]]
    assert rows.parse_positions("(12, 3) and (0.5,1.5)") == []
    assert rows.parse_positions(None) == []


def test_fingerprint_ignores_quantity_and_spacing():
    a = _count(quantity=3)
    b = _count(quantity=7, description="Duplex   receptacle")
    assert rows.fingerprint("electrical_drawings", "counts", a) == rows.fingerprint("electrical_drawings", "counts", b)
    c = _count(page=2)
    assert rows.fingerprint("electrical_drawings", "counts", a) != rows.fingerprint("electrical_drawings", "counts", c)


# Readers ------------------------------------------------------------------------------------------------


def test_iter_rows_over_all_outputs_and_legacy_boq(run7154):
    outputs = [run7154["specifications"], run7154["electrical"], run7154["rfp"], run7154["main"]]
    got = list(rows.iter_rows(outputs))
    ids = [r["row_id"] for r in got]
    assert len(ids) == len(set(ids))
    counts = [r for r in got if r["kind"] == "C"]
    assert len(counts) == 56 and counts[0]["category"] == "electrical_drawings"
    lines = [r for r in got if r["kind"] == "B"]
    assert len(lines) == 41
    assert [r["line_id"] for r in lines] == [f"B{i}" for i in range(1, 42)]
    assert lines[0]["section"] == "DEMOLITION" and lines[0]["transient_id"] is True
    assert lines[0]["description"] == "REMOVE DUPLEX RECEPTACLE"
    # input order does not change transient ids
    again = list(rows.iter_rows(list(reversed(outputs))))
    assert {r["row_id"]: r["fingerprint"] for r in again if r["kind"] == "C"} == {r["row_id"]: r["fingerprint"] for r in counts}


def test_iter_rows_filters_and_output_rows(run7154):
    stored = {"scope": "category", "category": "electrical_drawings", "run_id": "r", "output": run7154["electrical"]}
    only_counts = list(rows.iter_rows(stored, kinds=["counts"]))
    assert len(only_counts) == 56 and {r["kind"] for r in only_counts} == {"C"}
    by_cat = list(rows.iter_rows({"electrical_drawings": run7154["electrical"]}, kinds="S"))
    assert len(by_cat) == 14


def test_iter_boq_lines_skip_existing_ids(run7154):
    boq = copy.deepcopy(run7154["main"]["boq"])
    boq["areas"][0]["sections"][0]["groups"][0]["subgroups"][0]["items"][0]["line_id"] = "B3"
    lines = list(rows.iter_boq_lines(boq))
    ids = [r["line_id"] for r in lines]
    assert ids[0] == "B3" and len(set(ids)) == len(ids) and "B3" not in ids[1:]


def test_iter_rows_continues_after_v2_sequences(run7154, elec_v2):
    got = list(rows.iter_rows([run7154["rfp"], elec_v2], kinds=["O"]))
    elec_scope = {r["row_id"] for r in elec_v2["scope"]}
    rfp_ids = [r["row_id"] for r in got if r["category"] == "rfp"]
    assert rfp_ids and not set(rfp_ids) & elec_scope
    assert min(int(i[1:]) for i in rfp_ids) == elec_v2["row_seq"]["O"] + 1


def test_get_row_and_index_rows(run7154, elec_v2):
    rid = [r for r in elec_v2["counts"] if "rectangular" in r["description"]][0]["row_id"]
    row = rows.get_row([run7154["electrical"]], rid, meta=_meta(run7154))
    assert row is not None and row["coverage"] == "tile_subtotal" and row["origin"]["group_kind"] == "escalation"
    assert rows.get_row([run7154["electrical"]], "C999") is None
    assert rows.get_row([run7154["electrical"]], "nonsense") is None
    index = rows.index_rows(elec_v2)
    assert index[rid]["quantity"] == 5
    assert rows.index_rows(index) == index
    assert set(rows.index_rows(list(index.values()))) == set(index)


def test_compact_row(elec_v2):
    row = dict(elec_v2["counts"][0], kind="C")
    c = rows.compact_row(row, consumed_by="B2")
    assert set(c) == {"id", "sheet_key", "description", "tag", "qty", "unit", "basis", "coverage", "conf", "status", "consumed_by"}
    assert c["id"] == "C1" and c["consumed_by"] == "B2" and c["unit"] == "EA"


# Measure rows (M<n>, MeasureOutput v1 quantities[]) ------------------------------------------------


def _measure_output() -> dict:
    return {
        "version": 1,
        "wiring_method": "emt",
        "routes": [],
        "quantities": [
            {"row_id": "M1", "group": "BRANCH", "subgroup": "WIRE", "description": "#12 THHN CU",
             "quantity": 1200, "unit": "FT", "basis": "measured_route"},
            {"row_id": "M3", "group": "BRANCH", "subgroup": "CONDUIT", "description": '3/4"C EMT',
             "quantity": 400, "unit": "FT", "basis": "measured_route"},
            {"group": "BRANCH", "subgroup": "WIRE", "description": "#10 THHN CU", "quantity": 80},
        ],
    }


def test_kind_filter_accepts_every_prefix_letter():
    from app.services.estimating_contracts import ROW_ID_PREFIXES

    for letter in ROW_ID_PREFIXES:
        assert rows._kind_filter(letter) == {letter}
    assert rows._kind_filter(["quantities", "counts"]) == {"M", "C"}


def test_iter_rows_yields_measure_quantities_as_m_rows(run7154):
    measure = _measure_output()
    got = list(rows.iter_rows(measure, kinds="M"))
    assert [r["row_id"] for r in got] == ["M1", "M3", "M4"]
    assert {r["kind"] for r in got} == {"M"}
    assert got[2]["transient_id"] is True and got[2]["unit"] == "FT"
    assert "transient_id" not in got[0]
    # mixed with category outputs; a kind filter of C leaves the M rows out
    mixed = list(rows.iter_rows([run7154["electrical"], {"scope": "measure", "category": None, "run_id": "r", "output": measure}]))
    assert sum(1 for r in mixed if r["kind"] == "M") == 3
    assert not [r for r in rows.iter_rows([run7154["electrical"], measure], kinds="C") if r["kind"] == "M"]
    row = rows.get_row([measure], "M3")
    assert row is not None and row["quantity"] == 400 and row["description"] == '3/4"C EMT'
    assert rows.get_row([measure], "M9") is None
    index = rows.index_rows([run7154["electrical"], measure])
    assert index["M1"]["quantity"] == 1200
    c = rows.compact_row(row)
    assert c["id"] == "M3" and c["unit"] == "FT" and c["qty"] == 400


# Collisions (two categories merged from 1) ---------------------------------------------------------


def _other_category_with_c1(elec_v2: dict) -> dict:
    other = copy.deepcopy(elec_v2)
    other["category"] = "general_drawings"
    other["counts"] = [dict(other["counts"][0], description="Fire extinguisher cabinet", quantity=3)]
    for key, _p in rows.ROW_KINDS:
        if key != "counts":
            other[key] = []
    other["row_seq"] = {"C": 1}
    return other


def test_index_rows_logs_a_collision_and_keeps_the_first_row(elec_v2, caplog):
    """A second, different row of an id (the 7172 general C1 beside the
    electrical C1) is still not in the index (the first wins, as every
    consumer relies on) but the collision is logged, never silent; the
    same output handed in twice is not a collision."""
    other = _other_category_with_c1(elec_v2)
    with caplog.at_level(logging.WARNING, logger="app.services.plans_reader_rows"):
        index = rows.index_rows([elec_v2, other])
    assert index["C1"]["description"] == elec_v2["counts"][0]["description"] and len(index) == len(rows.index_rows(elec_v2))
    warnings = [r.getMessage() for r in caplog.records if "row id collision" in r.getMessage()]
    assert len(warnings) == 1 and "1 row id collision(s)" in warnings[0]
    assert "C1 (electrical_drawings kept, general_drawings dropped)" in warnings[0]

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.services.plans_reader_rows"):
        rows.index_rows([elec_v2, copy.deepcopy(elec_v2)])
        rows.index_rows(elec_v2)
    assert not [r for r in caplog.records if "collision" in r.getMessage()]

    # the row-list path logs the same way
    kept = dict(elec_v2["counts"][0], kind="C", category="electrical_drawings")
    lost = dict(other["counts"][0], kind="C", category="general_drawings")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.services.plans_reader_rows"):
        by_list = rows.index_rows([kept, lost])
    assert by_list["C1"]["description"] == kept["description"]
    assert any("C1 (electrical_drawings kept, general_drawings dropped)" in r.getMessage() for r in caplog.records)
    assert rows.collision_identity(kept) != rows.collision_identity(lost) and rows.collision_identity(kept) == rows.collision_identity(dict(kept))


def test_a_later_category_merged_on_the_run_sequence_never_collides(run7154, elec_v2):
    """The id contract of spec 5.1 end to end in this module: a category
    upgraded on a sequence seeded from the stored outputs
    (RowSeq.from_outputs) takes ids after every stored one, and index_rows
    over both outputs holds every row with nothing to log."""
    seq = rows.RowSeq.from_outputs([elec_v2])
    rfp = rows.upgrade_category_output(run7154["rfp"], _meta(run7154), row_seq=seq)
    both = rows.index_rows([elec_v2, rfp])
    n_elec = sum(len(elec_v2[k]) for k, _p in rows.ROW_KINDS)
    n_rfp = sum(len(rfp[k]) for k, _p in rows.ROW_KINDS)
    assert len(both) == n_elec + n_rfp
    assert min(int(r["row_id"][1:]) for r in rfp["scope"]) == elec_v2["row_seq"]["O"] + 1

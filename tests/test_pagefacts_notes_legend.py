"""pagefacts.notes and pagefacts.legend on synthetic PDFs (exact strings,
column joins, key-value legends, modifier effects, ranking under a token
cap) plus the golden-local 7154 checks PF5 and the keynote exact string on
E0.03 (marked `golden`; skipped when the backup is absent).

No network, no database, no model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.pagefacts import block, extract, legend, notes
from app.services.pagefacts.contracts import DocFacts
from tests.pagefacts_fixtures import make_pdf, sheet, title_block_texts

BACKUP = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")
PROJECT_DIR = BACKUP / "storage/project-files/7dfc64e6-36ef-4c67-9d02-0d7942015011"
ELEC_PDF = (
    PROJECT_DIR
    / "drawing/c85a4720840d49a288d9d0c7d3c87fed-Reference Material - Architectural - ELEC_RESPONSE_TO_CLV_COMMENTS D1.pdf"
)
golden = pytest.mark.golden
needs_backup = pytest.mark.skipif(
    not ELEC_PDF.exists(), reason="7154 backup PDF not on this machine"
)
BUDGETS = {"page_budget_s": 4.0, "doc_budget_s": 240.0}

KEYNOTE_1 = (
    "FURNISH AND INSTALL NEW GFCI TYPE CIRCUIT BREAKER WITH ALL REQUIRED MOUNTING HARDWARE. "
    "BREAKER MANUFACTURER AND AIC RATING TO MATCH EXISTING."
)


def _extract(pdf: bytes, **kw) -> DocFacts:
    return extract.extract_document(pdf, label="electrical_drawings", **BUDGETS, **kw)


def _words(pdf: bytes, page: int = 1):
    return extract.extract_words(pdf, pages=[page])[page]


# Notes ------------------------------------------------------------------------------


def _notes_sheet(extra: list[tuple] | None = None):
    """A plan-like sheet with a KEYNOTES block in the right column whose
    numbers sit in the gutter and whose text wraps, next to title-block
    lines on the same baselines."""
    x_num, x_txt = 34.5, 34.75
    texts: list[tuple] = [
        (x_num, 7.0, 7, "KEYNOTES :"),
        (35.6, 7.0, 7, "#"),
        (x_num, 7.3, 7, "1."),
        (x_txt, 7.3, 7, "FURNISH AND INSTALL NEW GFCI"),
        (x_txt, 7.45, 7, "TYPE CIRCUIT BREAKER WITH ALL"),
        (x_txt, 7.6, 7, "REQUIRED MOUNTING HARDWARE."),
        (x_txt, 7.75, 7, "BREAKER MANUFACTURER AND AIC"),
        (x_txt, 7.9, 7, "RATING TO MATCH EXISTING."),
        (x_num, 8.2, 7, "2."),
        (x_txt, 8.2, 7, "EXISTING OUTLET TO BE REMOVED. REMOVAL"),
        (x_txt, 8.35, 7, "SHALL INCLUDE THE JUNCTION BOX."),
        (x_num, 8.65, 7, "3. REFER TO ARCHITECTURAL DRAWINGS FOR"),
        (x_txt, 8.8, 7, "FLOOR HEIGHTS."),
        (x_num, 10.5, 7, "SHEET NOTES:"),
        (x_num, 10.8, 7, "1."),
        (x_txt, 10.8, 7, "ALL CIRCUITS TO PANEL LA UON."),
        (x_num, 11.1, 7, "2."),
        (x_txt, 11.1, 7, "POWER CONNECTION BY ELECTRICAL CONTRACTOR."),
        (15.0, 28.6, 18, "OVERALL POWER PLAN - NEW WORK"),
        (14.4, 28.9, 18, "1"),
        (15.0, 29.0, 7, '1/4" = 1\'-0"'),
    ]
    texts.extend(title_block_texts("E2.01", "OVERALL POWER PLAN - NEW WORK"))
    texts.extend(extra or [])
    return sheet(*texts)


def test_keynote_block_joins_columns_into_exact_strings():
    df = _extract(make_pdf([_notes_sheet()]))
    page = df.pages[0]
    blocks = {b["kind"]: b for b in page.notes}
    assert set(blocks) == {"keynotes", "sheet_notes"}
    keynotes = blocks["keynotes"]
    assert keynotes["label"] == "KEYNOTES :"
    assert [it["number"] for it in keynotes["items"]] == ["1", "2", "3"]
    assert keynotes["items"][0]["text"] == KEYNOTE_1
    assert keynotes["items"][1]["text"] == (
        "EXISTING OUTLET TO BE REMOVED. REMOVAL SHALL INCLUDE THE JUNCTION BOX."
    )
    assert keynotes["items"][2]["text"] == "REFER TO ARCHITECTURAL DRAWINGS FOR FLOOR HEIGHTS."
    assert keynotes["unnumbered"] == []  # the "#" column marker never becomes a note
    # Title-block lines on the same baselines (KEY PLAN, REVISION) never join a note.
    assert all("KEY PLAN" not in it["text"] for it in keynotes["items"])
    assert notes.keynote_texts(page.notes) == {
        "1": KEYNOTE_1,
        "2": keynotes["items"][1]["text"],
        "3": keynotes["items"][2]["text"],
    }
    assert blocks["sheet_notes"]["items"][0]["text"] == "ALL CIRCUITS TO PANEL LA UON."


def test_continuation_requires_x_overlap_and_consistent_leading():
    """A line far below (big gap) or in another column never continues an
    item; the next item's baseline is never swallowed by the previous one."""
    extra = [
        (34.75, 9.6, 7, "FAR AWAY LINE AFTER A GAP."),  # 0.8 in below item 3: a new paragraph
        (30.0, 8.8, 7, "OTHER COLUMN TEXT"),  # left of the block: a plan word, not a note
    ]
    df = _extract(make_pdf([_notes_sheet(extra)]))
    keynotes = next(b for b in df.pages[0].notes if b["kind"] == "keynotes")
    texts = [it["text"] for it in keynotes["items"]]
    assert texts[2] == "REFER TO ARCHITECTURAL DRAWINGS FOR FLOOR HEIGHTS."
    assert not any("OTHER COLUMN" in t for t in texts)
    assert not any("FAR AWAY" in t for t in texts)


def test_keynote_markers_match_number_height_and_skip_consumed_words():
    extra = [
        (12.0, 12.0, 7, "1"),  # same height as the keynote numbers: a marker candidate
        (20.0, 14.0, 7, "2"),
        (25.0, 16.0, 18, "3"),  # a view bubble, far taller: not a marker
        (16.0, 20.0, 7, "1LI"),  # a panel-only tag ...
        (16.0, 20.2, 7, "1"),  # ... whose attached number must not become a marker
        (22.0, 24.0, 9, "(E) PANEL 1LI"),  # the label that declares the panel name
    ]
    df = _extract(make_pdf([_notes_sheet(extra)]))
    page = df.pages[0]
    numbers = sorted(m["number"] for m in page.keynote_markers)
    assert numbers == ["1", "2"], page.keynote_markers
    assert all(m["confidence"] == 0.6 for m in page.keynote_markers)
    # The tag attached its number (the "1" at 16.0 in, not the marker at 12.0 in).
    tag = next(t for t in page.tags["circuit"] if t["raw"] == "1LI")
    assert tag["circuits"] == [1] and tag["attached_number"] is True


def test_cross_trade_mentions_whole_document_versus_notes_only():
    pdf = make_pdf([_notes_sheet()])
    pw = _words(pdf)
    df = _extract(pdf)
    by_page = {1: df.pages[0].notes}
    only_notes = notes.cross_trade_mentions({1: pw}, notes_by_page=by_page, whole_document=False)
    assert [m["text"] for m in only_notes] == ["POWER CONNECTION BY ELECTRICAL CONTRACTOR."]
    whole = notes.cross_trade_mentions({1: pw}, notes_by_page=by_page, whole_document=True)
    assert any("ELECTRICAL CONTRACTOR" in m["text"] for m in whole)
    assert df.cross_trade == only_notes  # an electrical drawing scans its notes only


def test_plain_text_normalizes_document_dashes():
    assert (
        notes.plain_text("HALO \u2013 #HC620D010 \u2014 HM60525835")
        == "HALO - #HC620D010 - HM60525835"
    )
    assert notes.plain_text("  A   B  ") == "A B"


# Legend ------------------------------------------------------------------------------


def _legend_sheet():
    """Two symbol-list tables side by side (SYMBOL, TAG, SPEC SECTION,
    DESCRIPTION), an abbreviation table, a LINE TYPE AND TAG KEY with gutter
    keys, a DEVICE KEY with subscripts and the luminaire keys."""
    t: list[tuple] = [
        (10.0, 1.2, 14, "ELECTRICAL SYMBOL LIST"),
        (9.5, 1.8, 9, "SYMBOL:"),
        (10.8, 1.8, 9, "TAG:"),
        (11.8, 1.7, 9, "SPEC"),
        (11.8, 1.95, 9, "SECTION:"),
        (12.8, 1.8, 9, "DESCRIPTION:"),
        (17.0, 1.2, 14, "ELECTRICAL SYMBOL LIST"),
        (16.5, 1.8, 9, "SYMBOL:"),
        (17.8, 1.8, 9, "TAG:"),
        (18.8, 1.7, 9, "SPEC"),
        (18.8, 1.95, 9, "SECTION:"),
        (19.8, 1.8, 9, "DESCRIPTION:"),
        (24.0, 1.2, 14, "ELECTRICAL ABBREVIATION KEY"),
        (23.5, 1.8, 9, "ABBR:"),
        (24.5, 1.8, 9, "DESCRIPTION:"),
    ]
    rows_left = [
        ("JB", "JUNCTION BOX"),
        ("ECONN", "ELECTRICAL CONNECTION"),
        ("DS-#/FDS-#", "DISCONNECT SWITCH"),
        ("REC-ISO-", "ISOLATED GROUND QUAD RECEPTACLE WITH"),
    ]
    y = 2.3
    for tag, desc in rows_left:
        t.append((9.6, y, 7, "S"))
        t.append((10.8, y, 7, tag))
        t.append((11.9, y, 7, "N/A"))
        t.append((12.8, y, 7, desc))
        y += 0.3
    t.append((10.8, y - 0.15, 7, "SUR-QUAD"))  # the wrapped tag continues
    t.append((12.8, y - 0.15, 7, "SURGE SUPPRESSION, 125V"))
    rows_right = [
        ("REC-DUP", "DUPLEX RECEPTACLE, 125V"),
        ("REC-DUP-GFI", "DUPLEX GFI RECEPTACLE, 125V"),
        ("REC-TAMP", "DUPLEX RECEPTACLE, TAMPER RESISTANT, 125V"),
        ("REC-TAMP-GFI", "GFI DUPLEX RECEPTACLE,"),
    ]
    y = 2.3
    for tag, desc in rows_right:
        t.append((16.6, y, 7, "S"))
        t.append((17.8, y, 7, tag))
        t.append((18.9, y, 7, "26 27 26"))
        t.append((19.8, y, 7, desc))
        y += 0.3
    t.append((19.8, y - 0.15, 7, "TAMPER RESISTANT, 125V"))  # wrapped description
    for i, (abbr, meaning) in enumerate(
        [("NIC", "NOT IN CONTRACTED SCOPE"), ("UON", "UNLESS OTHERWISE NOTED")]
    ):
        t.append((23.6, 2.3 + 0.3 * i, 7, abbr))
        t.append((24.5, 2.3 + 0.3 * i, 7, meaning))
    # Key-value legends.
    t += [
        (2.0, 8.0, 14, "LINE TYPE AND TAG KEY:"),
        (2.1, 8.5, 9, "(E) TAG or 'TAG'-E"),
        (3.6, 8.5, 7, "TAGS WITH DASH 'E' OR PARANTHESE 'E' INDICATES THE"),
        (3.6, 8.65, 7, "REFERENCED OBJECT IS EXISTING AND SHALL REMAIN."),
        (2.1, 9.0, 9, "(X) TAG or 'TAG'-X"),
        (3.6, 9.0, 7, "TAGS WITH DASH 'X' OR PARANTHESE 'X' INDICATES THE"),
        (3.6, 9.15, 7, "REFERENCED OBJECT IS TO BE REMOVED."),
        (2.1, 9.5, 9, "(R) TAG or 'TAG'-R"),
        (3.6, 9.5, 7, "TAGS WITH DASH 'R' OR PARANTHESE 'R' INDICATES THE"),
        (3.6, 9.65, 7, "REFERENCED OBJECT IS TO BE RELOCATED."),
        (2.1, 10.0, 7, "TAG-1"),
        (3.1, 10.0, 7, "UNDERLINED TAG INDICATES OBJECT IS IN-SCOPE. IF NEW, ADDITIONAL"),
        (3.1, 10.15, 7, "INFORMATION IS AVAILABLE IN A SCHEDULE."),
        (2.0, 11.5, 14, "DEVICE KEY:"),
        (2.1, 12.0, 7, "G"),
        (2.9, 12.0, 7, "GFCI WIRING DEVICE"),
        (2.1, 12.3, 7, "O"),
        (2.9, 12.3, 7, "WIRING DEVICE, OCCUPANCY CONTROLLED"),
        (2.1, 12.6, 7, "WP"),
        (2.9, 12.6, 7, "WEATHERPROOF"),
        (2.0, 12.9, 7, "SHADED DEVICE INDICATES DEVICE IS CONNECTED TO AN EMERGENCY CIRCUIT."),
        (2.0, 14.5, 14, "LUMINAIRE SHADING KEY"),
        (3.0, 15.0, 7, "LIFE SAFETY BRANCH LUMINAIRE"),
        (3.0, 15.3, 7, "NORMAL BRANCH LUMINAIRE"),
        (2.0, 16.5, 14, "LUMINAIRE CIRCUIT AND CONTROL KEY"),
        (3.0, 17.0, 7, "F1 = FIXTURE TAG"),
        (3.0, 17.2, 7, "1 = CIRCUIT NUMBER"),
        (3.0, 17.4, 7, "a = SWITCH DESIGNATION"),
        (3.0, 17.6, 7, '"NL" INDICATES LUMINAIRE IS UNSWITCHED FOR NIGHT LIGHT.'),
        (3.0, 17.8, 7, '"SE" INDICATES LUMINAIRE IS SWITCHED/CONTROLLED DURING NORMAL'),
        (3.0, 17.95, 7, "OPERATION AND OPERATES FROM EMERGENCY CIRCUIT UPON LOSS OF POWER."),
    ]
    t.extend(title_block_texts("E0.00", "ELECTRICAL COVERSHEET"))
    return sheet(*t)


def test_table_legend_tags_descriptions_and_wrapped_rows():
    df = _extract(make_pdf([_legend_sheet()]))
    entries = {e["tag"]: e for e in df.legend["entries"]}
    assert {
        "JB",
        "ECONN",
        "DS-#/FDS-#",
        "REC-DUP",
        "REC-DUP-GFI",
        "REC-TAMP",
        "REC-TAMP-GFI",
    } <= set(entries)
    assert entries["JB"]["description"] == "JUNCTION BOX"
    assert entries["REC-TAMP-GFI"]["description"] == "GFI DUPLEX RECEPTACLE, TAMPER RESISTANT, 125V"
    assert entries["REC-ISO-SUR-QUAD"]["description"] == (
        "ISOLATED GROUND QUAD RECEPTACLE WITH SURGE SUPPRESSION, 125V"
    )
    assert (
        entries["REC-DUP-GFI"]["spec_section"] == "26 27 26"
        and entries["JB"]["spec_section"] is None
    )
    assert entries["REC-DUP-GFI"]["device_type"] == "gfci_receptacle"
    assert entries["JB"]["device_type"] == "junction_box"
    assert entries["ECONN"]["device_type"] == "motor_connection"
    assert entries["REC-TAMP"]["device_type"] == "duplex_receptacle"
    assert (
        entries["JB"]["symbol_bbox"] is not None
        and entries["JB"]["symbol_bbox"][2] < entries["JB"]["bbox"][2]
    )
    assert entries["JB"]["group"] == "ELECTRICAL SYMBOL LIST" and entries["JB"][
        "entry_id"
    ].startswith("lg")
    assert all(e["page"] == 1 for e in df.legend["entries"])
    # The two tables never bleed into each other.
    assert "DUPLEX" not in entries["JB"]["description"]
    abbr = {a["abbr"]: a["meaning"] for a in df.legend["abbreviations"]}
    assert abbr == {"NIC": "NOT IN CONTRACTED SCOPE", "UON": "UNLESS OTHERWISE NOTED"}
    assert "legend_symbols" in df.pages[0].roles


def test_key_value_legends_declare_modifier_effects_and_subscripts():
    df = _extract(make_pdf([_legend_sheet()]))
    mods = df.legend["modifiers"]
    by_effect: dict[str, list[dict]] = {}
    for m in mods:
        by_effect.setdefault(m["effect"], []).append(m)
    for effect in (
        "existing_remain",
        "remove",
        "relocate",
        "in_scope_underline",
        "gfci",
        "occupancy_controlled",
        "weatherproof",
        "emergency_circuit",
        "life_safety_branch",
        "normal_branch",
        "fixture_tag",
        "circuit_number",
        "switch_designation",
        "night_light",
        "switched_emergency",
    ):
        assert effect in by_effect, (effect, sorted(by_effect))
    remove = by_effect["remove"][0]
    assert remove["key"] == "(X) TAG or 'TAG'-X" and remove["text"].endswith("TO BE REMOVED.")
    assert by_effect["switched_emergency"][0]["text"].endswith("UPON LOSS OF POWER.")
    assert by_effect["life_safety_branch"][0]["applies_to"] == "luminaire"
    assert legend.subscript_map(df.legend) == {
        "G": "gfci",
        "O": "occupancy_controlled",
        "WP": "weatherproof",
    }
    assert legend.declared_status_forms(df.legend) == {"X", "E", "R"}
    assert all(m["modifier_id"].startswith("md") for m in mods)
    hit = legend.lookup(df.legend, "GFI")
    assert {e["tag"] for e in hit["entries"]} == {"REC-DUP-GFI", "REC-TAMP-GFI"}
    assert [m["effect"] for m in legend.lookup(df.legend, effect="remove")["modifiers"]] == [
        "remove"
    ]


def test_device_type_and_effect_tables():
    assert legend.device_type_of("DUPLEX GFI RECEPTACLE, 125V") == "gfci_receptacle"
    assert legend.device_type_of("QUAD RECEPTACLE, 125V") == "quad_receptacle"
    assert legend.device_type_of("FLOOR BOX or POKE THROUGH") == "floor_box"
    assert legend.device_type_of("TECHNOLOGY OUTLET ROUGH-IN") == "data_outlet"
    assert legend.device_type_of("OCCUPANCY SENSOR, CEILING") == "occupancy_sensor"
    assert legend.device_type_of("GROUND BUS") == "other"
    assert (
        legend.effect_of("(E) TAG REFERENCED OBJECT IS EXISTING AND SHALL REMAIN")
        == "existing_remain"
    )
    assert legend.effect_of("(X) TAG REFERENCED OBJECT IS TO BE REMOVED") == "remove"
    assert (
        legend.effect_of('"NL" INDICATES LUMINAIRE IS UNSWITCHED FOR NIGHT LIGHT') == "night_light"
    )
    assert legend.effect_of("NIC NOT IN CONTRACTED SCOPE") == "not_in_contract"
    assert legend.effect_of("GROUND BUS") is None


def _synthetic_legend(n_simplex: int = 40) -> dict:
    entries = [
        {"tag": "GB", "description": "GROUND BUS", "used_on_pages": []},
        {"tag": "REC-DUP", "description": "DUPLEX RECEPTACLE, 125V", "used_on_pages": [9]},
    ]
    entries += [
        {"tag": f"REC-SIM-{i}R", "description": f"RECEPTACLE, {i}R, 250V", "used_on_pages": []}
        for i in range(n_simplex)
    ]
    entries += [
        {"tag": "REC-DUP-GFI", "description": "DUPLEX GFI RECEPTACLE, 125V", "used_on_pages": []},
        {
            "tag": "REC-TAMP",
            "description": "DUPLEX RECEPTACLE, TAMPER RESISTANT, 125V",
            "used_on_pages": [],
        },
        {"tag": "JB", "description": "JUNCTION BOX", "used_on_pages": []},
        {"tag": "ECONN", "description": "ELECTRICAL CONNECTION", "used_on_pages": []},
        {"tag": "RI-TECH", "description": "TECHNOLOGY OUTLET ROUGH-IN", "used_on_pages": [9]},
    ]
    for i, e in enumerate(entries):
        e["entry_id"] = f"lg{i + 1}"
        e["device_type"] = legend.device_type_of(e["description"])
        e["page"] = 1
    modifiers = [
        {"key": "G", "text": "GFCI WIRING DEVICE", "effect": "gfci"},
        {"key": "", "text": "LIFE SAFETY BRANCH LUMINAIRE", "effect": "life_safety_branch"},
    ]
    return {"entries": entries, "modifiers": modifiers, "abbreviations": []}


def test_rank_entries_puts_modifier_and_device_relevance_before_usage():
    ranked = [e["tag"] for e in legend.rank_entries(_synthetic_legend()["entries"])]
    # Entries carrying a declared modifier first (GFI, tamper), by device priority.
    assert ranked[:2] == ["REC-DUP-GFI", "REC-TAMP"]
    # Then one entry per device class before a second receptacle variant.
    first_round = ranked[2 : 2 + 5]
    assert "JB" in first_round and "ECONN" in first_round and "RI-TECH" in first_round
    assert first_round.index("REC-DUP") < ranked.index("REC-SIM-0R")  # used on a plan page first
    assert ranked.index("GB") > ranked.index("ECONN")  # "other" comes last within its round


def test_render_legend_text_under_a_small_cap_keeps_the_key_entries():
    text = legend.render_legend_text(
        _synthetic_legend(60), max_tokens=400, sheet_label="E0.00 D1 P1"
    )
    assert block.estimate_tokens(text) <= 400
    for tag in ("REC-DUP-GFI", "REC-TAMP", "JB", "ECONN"):
        assert f"- {tag}:" in text, tag
    assert "MODIFIERS:" in text and "G = GFCI WIRING DEVICE [gfci]" in text
    assert "more legend entries not shown" in text
    assert "\u2013" not in text and "\u2014" not in text


def test_used_on_pages_matches_tags_in_plan_areas():
    plan = sheet(
        (10.0, 10.0, 7, "JB"),
        (12.0, 10.0, 7, "ECONN/1"),
        (15.0, 28.6, 18, "POWER PLAN"),
        (15.0, 29.0, 7, '1/4" = 1\'-0"'),
        *title_block_texts("E2.01", "POWER PLAN"),
    )
    df = _extract(make_pdf([_legend_sheet(), plan]))
    used = {e["tag"]: e["used_on_pages"] for e in df.legend["entries"]}
    assert used["JB"] == [2] and used["ECONN"] == [2] and used["REC-DUP"] == []


# Golden-local 7154 (the backup PDF, $0) --------------------------------------------


@pytest.fixture(scope="module")
def elec() -> DocFacts:
    if not ELEC_PDF.exists():
        pytest.skip("7154 backup PDF not on this machine")
    return _extract(ELEC_PDF.read_bytes(), name=ELEC_PDF.name.split("-", 1)[1])


@golden
@needs_backup
def test_pf5_legend_entries_and_modifiers(elec):
    entries = {e["tag"]: e for e in elec.legend["entries"]}
    print(
        f"PF5 measured: {len(entries)} legend entries, {len(elec.legend['modifiers'])} modifiers, {len(elec.legend['abbreviations'])} abbreviations"
    )
    assert len(entries) >= 60
    for tag in ("REC-DUP-GFI", "REC-TAMP", "JB", "ECONN"):
        assert tag in entries, tag
    assert entries["REC-DUP-GFI"]["description"] == "DUPLEX GFI RECEPTACLE, 125V"
    assert (
        entries["JB"]["description"] == "JUNCTION BOX"
        and entries["ECONN"]["description"] == "ELECTRICAL CONNECTION"
    )
    assert entries["REC-TAMP"]["description"] == "DUPLEX RECEPTACLE, TAMPER RESISTANT, 125V"
    effects = {m["effect"] for m in elec.legend["modifiers"]}
    assert {
        "life_safety_branch",
        "night_light",
        "switched_emergency",
        "remove",
        "existing_remain",
        "relocate",
        "in_scope_underline",
        "emergency_circuit",
    } <= effects, effects
    assert legend.subscript_map(elec.legend)["G"] == "gfci"
    assert legend.declared_status_forms(elec.legend) == {"X", "E", "R"}
    assert all(e["page"] == 1 for e in elec.legend["entries"])
    text = legend.render_legend_text(elec.legend, max_tokens=1000, sheet_label="E0.00 D1 P1")
    assert block.estimate_tokens(text) <= 1000
    for tag in ("REC-DUP-GFI", "REC-TAMP", "JB", "ECONN"):
        assert f"- {tag}:" in text, tag


@golden
@needs_backup
def test_keynote_exact_string_on_e003_and_plan_notes(elec):
    by = {p.sheet_number: p for p in elec.pages}
    texts = notes.keynote_texts(by["E0.03"].notes)
    assert texts == {"1": KEYNOTE_1}
    e201 = {b["kind"]: b for b in by["E2.01"].notes}
    assert [it["number"] for it in e201["keynotes"]["items"]] == ["1", "2", "3", "4"]
    assert e201["keynotes"]["items"][0]["text"].startswith(
        "EXTEND AND CONNECT CIRCUITING FROM JUNCTION BOX"
    )
    assert len(e201["sheet_notes"]["items"]) == 12
    assert (
        e201["sheet_notes"]["items"][0]["text"]
        == "ALL CIRCUITS SHALL CARRY A GREEN GROUND CONDUCTOR."
    )
    ed201 = next(b for b in by["ED2.01"].notes if b["kind"] == "keynotes")
    assert (
        ed201["items"][3]["text"]
        == "ALL EXISTING OUTLETS AND OTHER ELECTRICAL IN THIS AREA TO REMAIN."
    )
    assert (
        by["E0.00"].notes[0]["items"][0]["number"] == "1"
        and len(by["E0.00"].notes[0]["items"]) == 11
    )
    assert by["ED2.01"].keynote_markers and all(
        m["confidence"] == 0.6 for m in by["ED2.01"].keynote_markers
    )

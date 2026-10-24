"""pagefacts.schedules and pagefacts.tags on synthetic PDFs (two-sided panel
schedules with stacked headers, positive-evidence circuit status in both
directions, luminaire schedules, the circuit grammar, status tags, type
marks, panel labels, clusters) plus the golden-local 7154 checks PF6
(marked `golden`; skipped when the backup is absent).

No network, no database, no model.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from app.services.pagefacts import extract, schedules, tags
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

NEW_BREAKER_NOTE = (
    "FURNISH AND INSTALL NEW GFCI TYPE CIRCUIT BREAKER WITH ALL REQUIRED MOUNTING HARDWARE."
)


def _extract(pdf: bytes, **kw) -> DocFacts:
    return extract.extract_document(pdf, label="electrical_drawings", **BUDGETS, **kw)


# Synthetic panel schedule --------------------------------------------------------------

LEFT = {
    "key": 10.0,
    "ckt": 10.5,
    "desc": 11.3,
    "amps": 14.0,
    "p": 14.8,
    "h": 15.1,
    "n": 15.4,
    "g": 15.7,
    "vd": 16.3,
}
PHASE = {"A": 17.0, "B": 17.6, "C": 18.2}
RIGHT = {
    "vd": 19.0,
    "g": 19.6,
    "n": 19.9,
    "h": 20.2,
    "p": 20.8,
    "amps": 21.2,
    "desc": 22.2,
    "ckt": 25.0,
    "key": 25.6,
}
ROW_PITCH = 0.16
FIRST_ROW_Y = 7.0


def _header(x: float, y: float, s: float = 7) -> list[tuple]:
    return [
        (LEFT["key"], y - 0.3, s, "K"),
        (LEFT["key"], y - 0.15, s, "E"),
        (LEFT["key"], y, s, "Y"),
        (LEFT["ckt"], y - 0.15, s, "CKT"),
        (LEFT["ckt"], y, s, "NO."),
        (LEFT["desc"], y, s, "LOAD DESCRIPTION"),
        (LEFT["amps"], y - 0.15, s, "OCPD"),
        (LEFT["amps"], y, s, "AMPS"),
        (LEFT["p"], y, s, "P"),
        (LEFT["n"] - 0.08, y - 0.3, s, "WIRE"),
        (LEFT["n"] - 0.08, y - 0.15, s, "SIZE"),
        (LEFT["h"], y, s, "H"),
        (LEFT["n"], y, s, "N"),
        (LEFT["g"], y, s, "G"),
        (LEFT["vd"], y - 0.15, s, "VD"),
        (LEFT["vd"], y, s, "%"),
        (PHASE["A"], y, s, "A"),
        (PHASE["B"], y, s, "B"),
        (PHASE["C"], y, s, "C"),
        (RIGHT["vd"], y - 0.15, s, "VD"),
        (RIGHT["vd"], y, s, "%"),
        (RIGHT["n"] - 0.08, y - 0.3, s, "WIRE"),
        (RIGHT["n"] - 0.08, y - 0.15, s, "SIZE"),
        (RIGHT["g"], y, s, "G"),
        (RIGHT["n"], y, s, "N"),
        (RIGHT["h"], y, s, "H"),
        (RIGHT["p"], y, s, "P"),
        (RIGHT["amps"], y - 0.15, s, "OCPD"),
        (RIGHT["amps"], y, s, "AMPS"),
        (RIGHT["desc"], y, s, "LOAD DESCRIPTION"),
        (RIGHT["ckt"], y - 0.15, s, "CKT"),
        (RIGHT["ckt"], y, s, "NO."),
        (RIGHT["key"], y - 0.3, s, "K"),
        (RIGHT["key"], y - 0.15, s, "E"),
        (RIGHT["key"], y, s, "Y"),
    ]


def _row(
    y: float,
    side: str,
    *,
    ckt: int | None,
    desc: str = "",
    amps: str = "",
    poles: str = "",
    wire: tuple[str, str, str] | None = None,
    vd: str = "",
    load: str = "",
    key: str = "",
    phase: str = "A",
) -> list[tuple]:
    col = LEFT if side == "L" else RIGHT
    out: list[tuple] = []
    if ckt is not None:
        out.append((col["ckt"], y, 7, str(ckt)))
    if key:
        out.append((col["key"], y, 7, key))
    if desc:
        out.append((col["desc"], y, 7, desc))
    if amps:
        out.append((col["amps"], y, 7, amps))
    if poles:
        out.append((col["p"], y, 7, poles))
    if wire:
        out += [(col["h"], y, 7, wire[0]), (col["n"], y, 7, wire[1]), (col["g"], y, 7, wire[2])]
    if vd:
        out.append((col["vd"], y, 7, vd))
    if load:
        x = PHASE[phase] - 0.22 if side == "L" else PHASE[phase] + 0.12
        out.append((x, y, 7, load))
    return out


def _panel_sheet(
    rows: list[tuple],
    *,
    title: str = "(E) PANEL LA",
    keynotes: list[tuple[str, str]] | None = None,
    extra: list[tuple] | None = None,
):
    texts: list[tuple] = [
        (12.0, 5.5, 14, title),
        (10.0, 5.9, 7, "MOUNTING: SURFACE"),
        (13.0, 5.9, 7, "MAIN:"),
        (13.5, 5.9, 7, "100 A MLO"),
        (16.0, 5.9, 7, "VOLTS: 120/208 Wye"),
        (16.0, 6.1, 7, "PHASE: 3"),
        (19.0, 6.1, 7, "FED FROM: 100 A/3P @ MDP"),
        (10.0, 6.3, 7, "NOTES:"),
    ]
    texts += _header(10.0, 6.75)
    texts += rows
    y_end = FIRST_ROW_Y + ROW_PITCH * 30
    texts += [(10.0, y_end, 7, "LOAD CLASSIFICATION"), (14.0, y_end, 7, "CONNECTED LOAD")]
    if keynotes:
        texts.append((34.5, 1.0, 7, "KEYNOTES:"))
        for i, (num, text) in enumerate(keynotes):
            texts.append((34.5, 1.3 + 0.3 * i, 7, f"{num}."))
            texts.append((34.75, 1.3 + 0.3 * i, 7, text))
    texts.extend(title_block_texts("E0.03", "PANEL SCHEDULES"))
    texts.extend(extra or [])
    return sheet(*texts)


def _ry(i: int) -> float:
    return FIRST_ROW_Y + ROW_PITCH * i


def _standard_rows() -> list[tuple]:
    rows: list[tuple] = []
    rows += _row(
        _ry(0), "L", ckt=1, desc="EXISTING LOAD", amps="20 A", poles="1", load="0", key="1"
    )
    rows += _row(_ry(0), "R", ckt=2, desc="EXISTING LOAD", amps="20 A", poles="1", load="0")
    rows += _row(
        _ry(1),
        "L",
        ckt=3,
        desc="PHARMACY RECEPTS",
        amps="20 A",
        poles="1",
        wire=("12", "12", "12"),
        vd="1.2",
        load="0.5",
        key="1",
        phase="B",
    )
    rows += _row(
        _ry(1),
        "R",
        ckt=4,
        desc="(E) BREAK RM RECEPTS",
        amps="20 A",
        poles="1",
        load="0.3",
        phase="B",
    )
    rows += _row(_ry(2), "L", ckt=5, desc="SPARE", amps="20 A", poles="1", phase="C")
    rows += _row(_ry(2), "R", ckt=6, desc="SPACE", phase="C")
    rows += _row(
        _ry(3),
        "L",
        ckt=7,
        desc="COMPOUND AREA RECEPTS",
        amps="20 A",
        poles="1",
        wire=("12", "12", "12"),
        vd="2.1",
        load="0.5",
    )
    rows += _row(_ry(3), "R", ckt=8, desc="NEW ICE MACHINE", amps="20 A", poles="1", load="0.9")
    # A 2-pole breaker: circuits 9 and 11, content on the baseline between them.
    rows += _row(_ry(4), "L", ckt=9)
    rows += _row(_ry(4) + ROW_PITCH / 2, "L", ckt=None, desc="WATER HEATER", amps="30 A", poles="2")
    rows += _row(_ry(5), "L", ckt=11)
    rows += _row(_ry(4), "R", ckt=10, desc="EXISTING LOAD", amps="20 A", poles="1", load="0")
    rows += _row(
        _ry(5), "R", ckt=12, desc="RELOCATED COPIER", amps="20 A", poles="1", load="0.4", phase="B"
    )
    return rows


def test_panel_two_sided_columns_rows_and_keynote_new():
    pdf = make_pdf([_panel_sheet(_standard_rows(), keynotes=[("1", NEW_BREAKER_NOTE)])])
    df = _extract(pdf)
    page = df.pages[0]
    assert page.schedule_ids == ["s1"] and len(df.schedules) == 1
    s = df.schedules[0]
    assert s["kind"] == "panel" and s["source"] == "text_layer" and s["schedule_id"] == "s1"
    cols = s["columns"]
    assert cols[:3] == ["KEY", "CKT NO.", "LOAD DESCRIPTION"] and cols.count("CKT NO.") == 2
    assert "OCPD AMPS" in cols and "VD %" in cols and {"H", "N", "G"} <= set(cols)
    p = s["panel"]
    assert p["name"] == "LA" and p["existing"] is True and p["status_basis"] == "(E) prefix"
    assert p["volts"] == "120/208" and p["phase"] == 3 and p["main"] == {"type": "MLO", "amps": 100}
    assert p["mounting"] == "SURFACE" and p["fed_from"] == "100 A/3P @ MDP"
    by = {c["ckt"]: c for c in p["circuits"]}
    assert sorted(by) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12]  # 11 is the continuation of 9
    assert by[9]["ckts"] == [9, 11] and by[9]["poles"] == 2 and by[9]["trip_a"] == 30
    assert by[9]["desc"] == "WATER HEATER"
    assert by[1]["side"] == "L" and by[2]["side"] == "R"
    assert (
        by[1]["status"] == "new"
        and by[1]["gfci"] is True
        and by[1]["status_basis"] == f"keynote: {NEW_BREAKER_NOTE}"
    )
    assert by[3]["status"] == "new" and by[3]["keynotes"] == ["1"]
    assert by[2]["status"] == "existing" and by[2]["keynotes"] == []
    assert by[4]["status"] == "existing_reused" and "(E)" in by[4]["desc"]
    assert by[5]["status"] == "spare" and by[6]["status"] == "space"
    assert by[7]["status"] == "unknown" and by[7]["signals"] == {
        "wire_filled": True,
        "vd_filled": True,
    }
    assert (
        by[7]["wire"] == {"hot": "12", "neutral": "12", "ground": "12"} and by[7]["vd_pct"] == 2.1
    )
    assert by[8]["status"] == "new" and by[8]["status_basis"] == "description NEW"
    assert by[12]["status"] == "relocated"
    assert by[3]["load_va"] == {"A": None, "B": 0.5, "C": None} and by[4]["load_va"]["B"] == 0.3
    assert [c["ckt"] for c in p["circuits"] if c["status"] == "new"] == [1, 3, 8]
    v = p["validation"]
    assert v["odd_even_ok"] and v["continuity_ok"] and v["poles_ok"] and v["trip_values_ok"]
    assert s["parse_confidence"] == 1.0 and s["unparsed_rows"] == []
    assert page.page_role == "schedule"
    assert df.panels[0]["name"] == "LA" and df.panels[0]["new_circuits"] == [1, 3, 8]


def test_existing_reused_needs_positive_evidence_in_both_directions():
    """A named load on an existing panel is unknown unless the row carries an
    (E) or EXISTING marker or matches a demolition-plan load; a marker on a
    bare EXISTING LOAD row is plain existing."""
    rows: list[tuple] = []
    rows += _row(
        _ry(0),
        "L",
        ckt=1,
        desc="PHARMACY RECEPTS",
        amps="20 A",
        poles="1",
        wire=("12", "12", "12"),
        vd="1.0",
        load="0.5",
    )
    rows += _row(
        _ry(0), "R", ckt=2, desc="EXISTING PHARMACY RECEPTS", amps="20 A", poles="1", load="0.5"
    )
    rows += _row(_ry(1), "L", ckt=3, desc="EXISTING LOAD", amps="20 A", poles="1", phase="B")
    rows += _row(_ry(1), "R", ckt=4, desc="BREAK RM FRIDGE", amps="20 A", poles="1", phase="B")
    pdf = make_pdf([_panel_sheet(rows)])
    pw = extract.extract_words(pdf)[1]
    df = _extract(pdf)
    by = {c["ckt"]: c for c in df.schedules[0]["panel"]["circuits"]}
    assert by[1]["status"] == "unknown" and by[1]["signals"] == {
        "wire_filled": True,
        "vd_filled": True,
    }
    assert (
        by[2]["status"] == "existing_reused"
        and by[2]["status_basis"] == "(E) or EXISTING marker on a named load"
    )
    assert by[3]["status"] == "existing" and by[3]["status_basis"] == "row marked EXISTING LOAD"
    assert by[4]["status"] == "unknown" and by[4]["signals"] == {}
    # The demolition-plan direction: the same table with the load named on ED sheets.
    tb = df.pages[0].region("title_block")
    parsed = schedules.parse_page(pw, 1, limit_x=tb.bbox[0], demo_loads={"BREAK RM FRIDGE"})
    by2 = {c["ckt"]: c for c in parsed[0]["panel"]["circuits"]}
    assert by2[4]["status"] == "existing_reused" and "demolition-plan" in by2[4]["status_basis"]
    assert by2[1]["status"] == "unknown"
    # A panel title without (E) is not evidence either way: named loads stay
    # unknown and the panel's state is only a signal (firms that never stamp
    # (E) would otherwise get every row as a new breaker, the E12 failure).
    df_new = _extract(make_pdf([_panel_sheet(rows, title="PANEL LB")]))
    byn = {c["ckt"]: c for c in df_new.schedules[0]["panel"]["circuits"]}
    assert df_new.schedules[0]["panel"]["existing"] is False
    assert byn[1]["status"] == "unknown" and byn[1]["status_basis"] is None
    assert byn[1]["signals"] == {
        "wire_filled": True,
        "vd_filled": True,
        "panel_not_marked_existing": True,
    }
    assert byn[4]["status"] == "unknown" and byn[4]["signals"] == {
        "panel_not_marked_existing": True
    }
    assert byn[3]["status"] == "existing"  # the row says so, in either panel
    assert df_new.panels[0]["new_circuits"] == []


def test_new_breaker_keynote_rule_is_new_or_furnish_and_install_breaker():
    """Keyed rows are new only for NEW ... BREAKER or FURNISH AND INSTALL ...
    BREAKER; PROVIDE/INSTALL ... CIRCUIT notes leave the row unknown with
    the keynote signal."""
    rows: list[tuple] = []
    rows += _row(_ry(0), "L", ckt=1, desc="PHARMACY RECEPTS", amps="20 A", poles="1", key="1")
    rows += _row(_ry(0), "R", ckt=2, desc="BREAK RM RECEPTS", amps="20 A", poles="1", key="2")
    rows += _row(_ry(1), "L", ckt=3, desc="COPIER", amps="20 A", poles="1", key="3", phase="B")
    notes = [
        ("1", "PROVIDE CIRCUIT TO NEW RECEPTACLE SHOWN ON PLANS."),
        ("2", "INSTALL CIRCUIT AND CONNECT TO EXISTING LOAD."),
        ("3", "NEW 20 A BREAKER IN EXISTING SPACE."),
    ]
    df = _extract(make_pdf([_panel_sheet(rows, keynotes=notes)]))
    by = {c["ckt"]: c for c in df.schedules[0]["panel"]["circuits"]}
    assert by[1]["status"] == "unknown" and by[1]["signals"] == {"keynote": True}
    assert by[2]["status"] == "unknown" and by[2]["signals"] == {"keynote": True}
    assert by[3]["status"] == "new" and by[3]["status_basis"] == f"keynote: {notes[2][1]}"
    assert schedules.NEW_BREAKER_NOTE_RE.search("FURNISH AND INSTALL NEW GFCI TYPE CIRCUIT BREAKER")
    assert not schedules.NEW_BREAKER_NOTE_RE.search("PROVIDE NEW CIRCUIT FROM PANEL")


def test_revision_delta_signal_sits_beside_the_row_outside_the_table():
    """A word equal to the sheet's revision number on the row band, just
    outside the table, is the `revision_delta` signal; the same digit inside
    the table (a keynote key) or on another row is not."""
    rows: list[tuple] = []
    rows += _row(_ry(0), "L", ckt=1, desc="PHARMACY RECEPTS", amps="20 A", poles="1")
    rows += _row(_ry(0), "R", ckt=2, desc="BREAK RM RECEPTS", amps="20 A", poles="1")
    rows += _row(_ry(1), "L", ckt=3, desc="COPIER", amps="20 A", poles="1", key="1", phase="B")
    rows += _row(_ry(1), "R", ckt=4, desc="ICE MACHINE", amps="20 A", poles="1", phase="B")
    extra = [
        (LEFT["key"] - 0.45, _ry(0), 7, "1"),  # delta beside circuit 1, left of the table
        (RIGHT["key"] + 0.55, _ry(0), 7, "2"),  # another number beside circuit 2: not the revision
        (LEFT["key"] - 0.45, _ry(1) + 0.9, 7, "1"),  # far below every row
    ]
    pdf = make_pdf([_panel_sheet(rows, extra=extra)])
    pw = extract.extract_words(pdf)[1]
    tb = _extract(pdf).pages[0].region("title_block")
    parsed = schedules.parse_page(pw, 1, limit_x=tb.bbox[0], revision="1")
    by = {c["ckt"]: c for c in parsed[0]["panel"]["circuits"]}
    assert by[1]["status"] == "unknown" and by[1]["signals"] == {"revision_delta": True}
    assert by[2]["signals"] == {} and by[4]["signals"] == {}
    assert by[3]["signals"] == {"keynote": True}  # the key column digit is a keynote, not a delta
    without = schedules.parse_page(pw, 1, limit_x=tb.bbox[0], revision=None)
    assert {c["ckt"]: c for c in without[0]["panel"]["circuits"]}[1]["signals"] == {}


def test_validators_and_unparsed_rows_are_never_guessed():
    rows: list[tuple] = []
    rows += _row(_ry(0), "L", ckt=1, desc="LOAD ONE", amps="27 A", poles="1")  # not a standard trip
    rows += _row(
        _ry(0), "R", ckt=2, desc="LOAD TWO", amps="20 A", poles="1", wire=("12", "12", "13")
    )
    rows += _row(_ry(1), "L", ckt=3, desc="LOAD THREE", amps="20 A", poles="1", phase="B")
    rows += _row(_ry(1), "R", ckt=4, desc="LOAD FOUR", amps="20 A", poles="1", phase="B")
    df = _extract(make_pdf([_panel_sheet(rows)]))
    s = df.schedules[0]
    by = {c["ckt"]: c for c in s["panel"]["circuits"]}
    assert by[1]["trip_a"] == 27 and by[1]["problems"] == ["trip 27 A not a standard value"]
    assert by[2]["wire"]["ground"] == "13" and by[2]["problems"] == ["wire ground 13"]
    assert s["panel"]["validation"] == {
        "odd_even_ok": True,
        "continuity_ok": True,
        "poles_ok": True,
        "trip_values_ok": False,
        "rows_total": 4,
        "rows_valid": 2,
    }
    assert s["parse_confidence"] == 0.5
    reasons = sorted(u["reason"] for u in s["unparsed_rows"])
    assert reasons == ["trip 27 A not a standard value", "wire ground 13"]
    assert all(isinstance(u["raw"], list) for u in s["unparsed_rows"])


def test_panel_lookup_and_summaries():
    df = _extract(make_pdf([_panel_sheet(_standard_rows())]))
    assert [s["schedule_id"] for s in schedules.lookup(df.schedules, kind="panel", name="la")] == [
        "s1"
    ]
    assert schedules.lookup(df.schedules, circuit=11)[0]["panel"]["name"] == "LA"
    assert (
        schedules.lookup(df.schedules, circuit=99) == []
        and schedules.lookup(df.schedules, kind="luminaire") == []
    )
    assert schedules.panel_names(df.schedules) == {"LA"}
    assert schedules.confident_count(df.schedules) == 1


# Luminaire schedule --------------------------------------------------------------------

LUM = {
    "tag": 2.0,
    "desc": 3.0,
    "mtg": 7.0,
    "lamp": 8.0,
    "lumens": 9.2,
    "watts": 10.4,
    "volts": 11.4,
    "control": 12.6,
    "mfr": 14.2,
}


def _luminaire_sheet(rows: list[dict], *, extra: list[tuple] | None = None):
    y = 3.0
    texts: list[tuple] = [
        (6.0, 2.3, 14, "LUMINAIRE SCHEDULE"),
        (LUM["tag"], y, 7, "TAG"),
        (LUM["desc"], y, 7, "DESCRIPTION"),
        (LUM["mtg"], y, 7, "MTG"),
        (LUM["lamp"], y, 7, "LAMP TYPE"),
        (LUM["lumens"], y - 0.15, 7, "DELIVERED"),
        (LUM["lumens"], y, 7, "LUMENS"),
        (LUM["watts"], y, 7, "WATTS"),
        (LUM["volts"], y, 7, "VOLTAGE"),
        (LUM["control"], y - 0.15, 7, "CONTROL"),
        (LUM["control"], y, 7, "METHOD"),
        (LUM["mfr"], y, 7, "MANUFACTURER - SERIES"),
    ]
    ry = y + 0.3
    for r in rows:
        for key, x in LUM.items():
            val = r.get(key)
            if val:
                texts.append((x, ry, 7, val))
        for i, cont in enumerate(r.get("wrap", [])):
            texts.append((LUM["desc"], ry + 0.15 * (i + 1), 7, cont))
        ry += 0.3 + 0.15 * len(r.get("wrap", []))
    texts.append((6.0, ry + 0.4, 14, "LUMINAIRE SCHEDULE LEGEND"))
    texts.append((2.0, ry + 0.8, 7, "PAF - PAINT AFTER FABRICATION"))
    texts.extend(title_block_texts("E0.04", "LIGHTING FIXTURE SCHEDULE"))
    texts.extend(extra or [])
    return sheet(*texts)


LUM_ROWS = [
    {
        "tag": "L1",
        "desc": "2' X 4' RECESSED TROFFER",
        "mtg": "RE",
        "lamp": "LED",
        "lumens": "3,071 lm",
        "watts": "40 / FIX",
        "volts": "277",
        "control": "0-10V",
        "mfr": "METALUX - #24GR-LD5",
    },
    {
        "tag": "L4",
        "desc": "TASK LED LIGHTING",
        "mtg": "WL",
        "lamp": "LED",
        "lumens": "0 lm",
        "watts": "9 / FIX",
        "volts": "120",
        "control": "LOCAL",
        "mfr": "WAC LIGHTING #BL-29903",
    },
    {
        "tag": "L5",
        "desc": "UNDERCABINET LED LIGHTING",
        "mtg": "UC",
        "lamp": "LED",
        "lumens": "345 lm",
        "watts": "5 / FT",
        "volts": "120",
        "control": "LOCAL",
        "mfr": "WAC LIGHTING #LS-LED14P",
    },
    {
        "tag": "X1",
        "desc": "LED EDGE LIT EXIT SIGN. NUMBER OF",
        "wrap": ["FACES AND CHEVRONS PER PLANS"],
        "mtg": "CL",
        "lamp": "LED",
        "watts": "5 / FIX",
        "volts": "277",
        "mfr": "SURE-LITES - #EUS70R",
    },
    {
        "tag": "L6-EM",
        "desc": "LINEAR PENDANT WITH BATTERY",
        "mtg": "SP",
        "lamp": "LED",
        "lumens": "4,000 lm",
        "watts": "30 / FIX",
        "volts": "277",
        "control": "0-10V",
        "mfr": "ACME - #LP-4",
    },
]


def test_luminaire_schedule_control_linear_and_emergency_hints():
    df = _extract(make_pdf([_luminaire_sheet(LUM_ROWS)]))
    assert len(df.schedules) == 1 and df.schedules[0]["kind"] == "luminaire"
    s = df.schedules[0]
    assert s["title"] == "LUMINAIRE SCHEDULE" and s["columns"][:2] == ["TAG", "DESCRIPTION"]
    by = {lum["tag"]: lum for lum in s["luminaires"]}
    assert list(by) == ["L1", "L4", "L5", "X1", "L6-EM"]
    assert (
        by["L1"]["lumens"] == 3071
        and by["L1"]["watts"] == 40.0
        and by["L1"]["watts_per"] == "fixture"
    )
    assert by["L1"]["manufacturer"] == "METALUX" and by["L1"]["catalog"] == "#24GR-LD5"
    assert (
        by["L4"]["control"] == "LOCAL"
        and by["L4"]["mounting"] == "WL"
        and by["L4"]["volts"] == "120"
    )
    assert by["L4"]["linear_hint"] is False
    assert (
        by["L5"]["linear_hint"] is True
        and by["L5"]["watts_per"] == "ft"
        and by["L5"]["mounting"] == "UC"
    )
    assert (
        by["X1"]["description"] == "LED EDGE LIT EXIT SIGN. NUMBER OF FACES AND CHEVRONS PER PLANS"
    )
    assert by["X1"]["control"] is None and by["X1"]["lumens"] is None
    assert by["L6-EM"]["emergency_hint"] is True and by["L6-EM"]["linear_hint"] is True
    assert by["L1"]["emergency_hint"] is False and by["X1"]["emergency_hint"] is False
    assert s["parse_confidence"] == 1.0 and df.pages[0].page_role == "schedule"
    fixtures = {f["tag"]: f for f in df.fixture_types}
    assert fixtures["L4"]["control"] == "LOCAL" and fixtures["L5"]["linear_hint"] is True
    assert schedules.luminaire_tags(df.schedules) == {"L1", "L4", "L5", "X1", "L6-EM"}
    assert [lum["tag"] for lum in schedules.lookup(df.schedules, tag="l5")[0]["luminaires"]][
        0
    ] == "L1"


# Tags ---------------------------------------------------------------------------------


def _plan_sheet(
    texts: list[tuple],
    *,
    number: str = "E2.01",
    title: str = "OVERALL POWER PLAN - NEW WORK",
    notes: list[str] | None = None,
):
    out: list[tuple] = [
        (15.0, 28.6, 18, title),
        (14.4, 28.9, 18, "1"),
        (15.0, 29.0, 7, '1/4" = 1\'-0"'),
    ]
    if notes:
        out.append((34.5, 1.0, 7, "SHEET NOTES:"))
        for i, text in enumerate(notes):
            out.append((34.5, 1.3 + 0.3 * i, 7, f"{i + 1}."))
            out.append((34.75, 1.3 + 0.3 * i, 7, text))
    out += texts
    out.extend(title_block_texts(number, title))
    return sheet(*out)


def test_circuit_grammar_mods_panel_and_attached_numbers():
    plan = _plan_sheet(
        [
            (5.0, 10.0, 7, "G/A/1LI-4"),
            (8.0, 10.0, 7, "A/1LI-12,14"),
            (11.0, 10.0, 7, "L5/1LH"),  # fixture type mod, number attached below
            (11.0, 10.2, 7, "7"),
            (14.0, 10.0, 7, "1EELC"),  # bare panel, dash and number as separate words
            (14.5, 10.0, 7, "-"),
            (14.65, 10.0, 7, "3"),
            (18.0, 10.0, 7, "A/1LI-8", 90),  # rotated tag with its own number
            (22.0, 12.0, 7, "1LH"),  # too far from any number: stays without one
            (30.0, 12.0, 7, "PANEL"),  # a panel label, not a circuit tag
            (30.5, 12.0, 7, "1LI"),
            (6.0, 14.0, 7, "1LI"),  # number more than 0.35 in away: not attached
            (7.0, 14.0, 7, "9"),
            (20.0, 20.0, 9, "(E) PANEL 1LH"),  # labels declare the other panel names
            (20.0, 20.5, 9, "(E) PANEL 1EELC"),
        ],
        notes=["ALL CIRCUITS SHALL CARRY A GREEN GROUND CONDUCTOR."],
    )
    df = _extract(make_pdf([_panel_sheet(_standard_rows(), title="(E) PANEL 1LI"), plan]))
    page = df.pages[1]
    circuit = page.tags["circuit"]
    by_raw = {t["raw"]: t for t in circuit}
    assert len(circuit) == 7, [t["raw"] for t in circuit]  # the PANEL 1LI label is not a tag
    assert not any(
        t["raw"] in ("1EELC", "1LH") and t["y"] > 0.6 for t in circuit
    )  # labels are not tags
    assert by_raw["G/A/1LI-4"] == {
        **by_raw["G/A/1LI-4"],
        "panel": "1LI",
        "circuits": [4],
        "mods": ["G", "A"],
        "attached_number": False,
        "rotation": 0,
    }
    assert by_raw["A/1LI-12,14"]["circuits"] == [12, 14]
    assert (
        by_raw["L5/1LH"]["panel"] == "1LH"
        and by_raw["L5/1LH"]["circuits"] == [7]
        and by_raw["L5/1LH"]["attached_number"]
    )
    assert by_raw["1EELC"]["circuits"] == [3] and by_raw["1EELC"]["attached_number"] is True
    assert by_raw["A/1LI-8"]["rotation"] == 90 and by_raw["A/1LI-8"]["circuits"] == [8]
    assert by_raw["1LH"]["circuits"] == []
    assert "PANEL" not in by_raw and sum(1 for t in circuit if t["raw"] == "1LI") == 1
    assert by_raw["1LI"]["circuits"] == [] and by_raw["1LI"]["attached_number"] is False
    labels = {lab["panel"]: lab for lab in page.tags["panel_labels"]}
    assert set(labels) == {"1LI", "1LH", "1EELC"}
    assert labels["1LI"] == {
        "panel": "1LI",
        "x": pytest.approx(0.725, abs=0.02),
        "y": pytest.approx(0.4, abs=0.02),
        "raw": "PANEL 1LI",
        "existing": False,
        "source": "plan_label",
    }
    assert labels["1LH"]["existing"] is True and labels["1LH"]["raw"] == "(E) PANEL 1LH"
    assert "default_panel" not in page.tags  # the 4-key shape unless a note declares one
    occ = tags.occurrences_by_circuit(page.tags)
    assert occ[("1LI", 4)] and occ[("1LI", 12)] and occ[("1LH", 7)] and ("1EELC", 3) in occ
    assert "1EELC" in {t["panel"] for t in circuit}  # a label on the plan sheet is a panel name too
    # The schedule's panel plus the labels built the grammar: 1LH, 1EELC come from labels.
    assert sorted(loc["panel"] for loc in df.panel_locations) == ["1EELC", "1LH", "1LI"]
    assert all(loc["page"] == 2 for loc in df.panel_locations)


def test_one_letter_panel_names_only_from_a_parsed_schedule_and_default_panel_notes():
    plan_a = _plan_sheet(
        [(5.0, 10.0, 7, "A-12"), (8.0, 10.0, 7, "G/14"), (10.0, 10.0, 7, "A")],
        number="E2.02",
        title="POWER PLAN TWO",
    )
    df = _extract(make_pdf([plan_a]))
    assert df.pages[0].tags["circuit"] == []  # no schedule declares "A": nothing matches
    df2 = _extract(make_pdf([_panel_sheet(_standard_rows(), title="PANEL A"), plan_a]))
    raws = sorted(t["raw"] for t in df2.pages[1].tags["circuit"])
    assert raws == ["A", "A-12"]  # the schedule declares A; "G/14" still has no panel
    plan_default = _plan_sheet(
        [
            (5.0, 10.0, 7, "G/14"),
            (8.0, 10.0, 7, "A/2"),
            (10.0, 10.0, 7, "CKT-5"),
            (12.0, 10.0, 7, "16"),
        ],
        number="E2.03",
        title="POWER PLAN THREE",
        notes=["ALL CIRCUITS TO PANEL LA UON."],
    )
    df3 = _extract(make_pdf([plan_default]))
    t = df3.pages[0].tags
    assert t["default_panel"] == "LA"
    got = {x["raw"]: x for x in t["circuit"]}
    assert set(got) == {"G/14", "A/2", "CKT-5"}  # a bare "16" never becomes a tag
    assert (
        got["G/14"]["panel"] == "LA"
        and got["G/14"]["circuits"] == [14]
        and got["G/14"]["default_panel"] is True
    )
    assert got["CKT-5"]["circuits"] == [5]


def test_status_tags_type_marks_in_plan_area_only_and_label_height():
    plan = _plan_sheet(
        [
            (5.0, 10.0, 7, "(X)"),
            (6.0, 10.0, 7, "(X)"),
            (7.0, 10.0, 7, "(E)"),
            (8.0, 10.0, 7, "(R)"),
            (9.0, 10.0, 7, "(N)"),
            (10.0, 10.0, 7, "L5"),
            (11.0, 10.0, 7, "L5/1LH-2"),
            (12.0, 10.0, 7, "L1"),
            (36.5, 1.3, 7, "(X)"),  # inside the SHEET NOTES block: not a plan symbol
            (40.0, 20.0, 7, "(X)"),  # inside the title block strip: not a plan symbol
            (20.0, 15.0, 12, "(E) PANEL 1LH"),
            (24.0, 15.0, 7, "PANEL 1LH"),
            (26.0, 16.0, 7, "PANEL DIRECTORIES"),
        ],
        number="ED3.01",
        title="OVERALL LIGHTING PLAN - DEMOLITION WORK",
        notes=["REMOVE FIXTURES AS SHOWN."],
    )
    lum = _luminaire_sheet(LUM_ROWS)
    df = _extract(make_pdf([lum, _panel_sheet(_standard_rows(), title="(E) PANEL 1LH"), plan]))
    t = df.pages[2].tags
    # The (E) of "(E) PANEL 1LH" is an existing-item marker too.
    assert {k: len(v) for k, v in t["status"].items()} == {"E": 2, "N": 1, "R": 1, "X": 2}
    assert {k: len(v) for k, v in t["type_marks"].items()} == {"L1": 1, "L5": 2}
    assert [t_["raw"] for t_ in t["circuit"]] == ["L5/1LH-2"]
    assert t["circuit"][0]["fixture_type"] == "L5"
    labels = t["panel_labels"]
    assert len(labels) == 1 and labels[0]["panel"] == "1LH" and labels[0]["existing"] is True
    assert labels[0]["raw"] == "(E) PANEL 1LH"  # the taller label wins
    # The luminaire schedule table is a schedule region: its type marks are
    # table rows, never plan symbols, and the table extent is on the page.
    lum_page = df.pages[0]
    assert lum_page.tags["type_marks"] == {}
    sched_regions = [r for r in lum_page.regions if r.kind == "schedule" and r.label]
    sb = df.schedules[0]["bbox"]
    grown = [
        r
        for r in sched_regions
        if r.bbox[0] == sb[0] and r.bbox[2] == sb[2] and r.bbox[3] == sb[3] and r.bbox[1] < sb[1]
    ]
    assert len(grown) == 1 and grown[0].label == "LUMINAIRE SCHEDULE"  # grown up to its title
    assert lum_page.regions[-1].kind == "plan_area"
    # The panel sheet: the header fields above the column header (FED FROM ...)
    # are inside the schedule region, so a panel name there is not a plan tag.
    panel_page = df.pages[1]
    region = next(r for r in panel_page.regions if r.kind == "schedule" and r.label)
    assert region.label == "(E) PANEL 1LH" and region.contains(16.0 / 42.0, 5.9 / 30.0)
    assert panel_page.tags["circuit"] == []
    assert df.panel_locations == [
        {
            "panel": "1LH",
            "page": 3,
            "x": labels[0]["x"],
            "y": labels[0]["y"],
            "existing": True,
            "source": "plan_label",
        }
    ]


def test_panel_labels_skip_prose_and_admit_letter_names_only_with_evidence():
    """PANEL followed by a prose word (AND, LOAD, TYPE) is never a label and
    never enters the circuit grammar; a letter-only name counts when quoted,
    prefixed by (E)/(N)/(R) or ending its row."""
    texts = [
        (5.0, 10.0, 7, "BOXES SHALL BE MARKED WITH PANEL AND CIRCUIT NUMBERS"),
        (5.0, 10.4, 7, "4. PANELBOARD LOAD SUMMARIES INCLUDE"),
        (5.0, 10.8, 7, "PANEL TYPE AND COLOR WITH ARCHITECT."),
        (5.0, 11.2, 7, "ALL CIRCUITS TO PANEL LA UON."),
        (5.0, 11.6, 7, "(E) PANEL ILL"),
        (5.0, 12.0, 7, "DISTRIBUTION PANEL 'EM2'"),
        (5.0, 12.4, 7, "PANEL HB."),
        (5.0, 12.8, 7, 'PANEL "LC" SHOWN'),
        (5.0, 13.2, 7, "PANEL DIRECTORIES"),
        (9.0, 14.0, 7, "AND-3"),
        (9.0, 14.4, 7, "LOAD-5"),
        (9.0, 14.8, 7, "ILL-7"),
    ]
    df = _extract(make_pdf([_plan_sheet(texts, number="E2.05", title="POWER PLAN FIVE")]))
    pw = extract.extract_words(make_pdf([_plan_sheet(texts, number="E2.05", title="X")]))[1]
    names = sorted(lab["panel"] for lab in tags.panel_labels_on_page(pw))
    assert names == ["EM2", "HB", "ILL", "LC"]
    t = df.pages[0].tags
    assert sorted(lab["panel"] for lab in t["panel_labels"]) == ["EM2", "HB", "ILL", "LC"]
    assert [x["raw"] for x in t["circuit"]] == ["ILL-7"]  # AND-3 and LOAD-5 never parse
    assert all(w not in str(df.to_dict()) for w in ("PANEL LABELS ON THIS SHEET: AND",))


def test_clusters_and_tag_regions_in_sheet_inches():
    pts = [[0.1, 0.1], [0.11, 0.12], [0.5, 0.5], [0.52, 0.5], [0.9, 0.9]]
    groups = tags.clusters(pts, 42.0, 30.0, eps_in=2.0)
    assert sorted(sorted(g) for g in groups) == [[0, 1], [2, 3], [4]]
    regions = tags.tag_regions(pts, 42.0, 30.0, pad_in=1.0, max_regions=2)
    assert len(regions) == 2
    x0, y0, x1, y1 = regions[0]
    assert x0 == pytest.approx(0.1 - 1 / 42, abs=1e-3) and y1 == pytest.approx(
        0.12 + 1 / 30, abs=1e-3
    )
    assert tags.tag_regions([[0.0, 0.0]], 42.0, 30.0) == [
        [0.0, 0.0, round(1 / 42, 4), round(1 / 30, 4)]
    ]
    assert tags.clusters([], 42.0, 30.0) == []


def test_parse_tag_word_grammar():
    g = tags.circuit_grammar(["1LI", "1LH", "A"])
    assert tags.parse_tag_word("G/A/1LI-4", g) == {
        "panel": "1LI",
        "circuits": [4],
        "mods": ["G", "A"],
        "tail": None,
    }
    assert tags.parse_tag_word("1LH-12,14&16/EM", g) == {
        "panel": "1LH",
        "circuits": [12, 14, 16],
        "mods": [],
        "tail": "EM",
    }
    assert tags.parse_tag_word("A", g)["panel"] == "A" and tags.parse_tag_word("A:7", g)[
        "circuits"
    ] == [7]
    assert tags.parse_tag_word("1LIX", g) is None and tags.parse_tag_word("REC-DUP", g) is None
    assert tags.parse_tag_word("12", g) is None and tags.parse_tag_word("G/12", g) is None
    assert tags.parse_tag_word("G/12", g, default_panel="LA") == {
        "panel": "LA",
        "circuits": [12],
        "mods": ["G"],
        "tail": None,
        "default_panel": True,
    }
    assert tags.circuit_grammar([]) is None and tags.parse_tag_word("1LI-1", None) is None


# Golden-local 7154 (the backup PDF, $0) --------------------------------------------


@pytest.fixture(scope="module")
def elec() -> DocFacts:
    if not ELEC_PDF.exists():
        pytest.skip("7154 backup PDF not on this machine")
    return _extract(ELEC_PDF.read_bytes(), name=ELEC_PDF.name.split("-", 1)[1])


@golden
@needs_backup
def test_pf6_panels_keynote_gfci_and_luminaires(elec):
    panels = {s["panel"]["name"]: s for s in elec.schedules if s["kind"] == "panel"}
    assert set(panels) == {"1EELC", "1HD", "1LH", "1LI"}
    for name, s in panels.items():
        print(
            f"PF6 measured: panel {name} parse_confidence {s['parse_confidence']} rows {len(s['panel']['circuits'])} unparsed {len(s['unparsed_rows'])}"
        )
        assert s["parse_confidence"] >= 0.95, (name, s["parse_confidence"])
        assert s["panel"]["existing"] is True and s["panel"]["volts"] in ("120/208", "480/277")
    keyed = {
        (name, c["ckt"]): c
        for name, s in panels.items()
        for c in s["panel"]["circuits"]
        if "1" in c["keynotes"]
    }
    assert sorted(keyed) == [("1EELC", 1), ("1EELC", 3), ("1EELC", 5), ("1EELC", 7)]
    for c in keyed.values():
        assert (
            c["status"] == "new"
            and c["gfci"] is True
            and c["status_basis"].startswith("keynote: FURNISH AND INSTALL NEW GFCI")
        )
    others_new = [
        (name, c["ckt"])
        for name, s in panels.items()
        for c in s["panel"]["circuits"]
        if c["status"] == "new" and (name, c["ckt"]) not in keyed
    ]
    assert others_new == []
    statuses = Counter(c["status"] for s in panels.values() for c in s["panel"]["circuits"])
    print(f"PF6 measured: circuit statuses {dict(statuses)}")
    assert (
        statuses["existing_reused"] <= 1 and statuses["unknown"] >= 20
    )  # named loads stay unknown without evidence
    non_spare = [
        c
        for s in panels.values()
        for c in s["panel"]["circuits"]
        if c["status"] not in ("spare", "space")
    ]
    wired = sum(1 for c in non_spare if all(c["wire"].values()))
    print(
        f"PF6 measured: wire H/N/G present on {wired} of {len(non_spare)} non-spare rows (the document fills wire only on named loads)"
    )
    assert panels["1EELC"]["panel"]["main"] == {"type": "MCB", "amps": 150}
    assert (
        panels["1HD"]["panel"]["volts"] == "480/277" and panels["1HD"]["panel"]["sccr_ka"] == 65.0
    )
    lum = [s for s in elec.schedules if s["kind"] == "luminaire"]
    assert len(lum) == 1 and [x["tag"] for x in lum[0]["luminaires"]] == [
        "L1",
        "L2",
        "L3",
        "L4",
        "L5",
        "X1",
    ]
    by = {x["tag"]: x for x in lum[0]["luminaires"]}
    assert by["L4"]["control"] == "LOCAL" and by["L4"]["description"] == "TASK LED LIGHTING"
    assert by["L5"]["linear_hint"] is True and by["L5"]["mounting"] == "UC"
    assert (
        by["X1"]["description"] == "LED EDGE LIT EXIT SIGN. NUMBER OF FACES AND CHEVRONS PER PLANS"
    )
    assert not any(x["emergency_hint"] for x in lum[0]["luminaires"])
    assert [f["tag"] for f in elec.fixture_types] == ["L1", "L2", "L3", "L4", "L5", "X1"]
    assert [p["name"] for p in elec.panels] == ["1HD", "1EELC", "1LH", "1LI"]
    assert elec.panels[1]["new_circuits"] == [1, 3, 5, 7]
    sheet_by = {p.sheet_number: p for p in elec.pages}
    assert sheet_by["E0.03"].schedule_ids == ["s1", "s2", "s3", "s4"] and sheet_by[
        "E0.04"
    ].schedule_ids == ["s5"]
    assert "\u2013" not in str(elec.to_dict()) and "\u2014" not in str(elec.to_dict())


@golden
@needs_backup
def test_pf6_tags_x34_x39_and_74_circuit_tokens(elec):
    by = {p.sheet_number: p for p in elec.pages}
    x_counts = {n: len(by[n].tags["status"].get("X", [])) for n in ("ED2.01", "ED3.01")}
    circuit = by["E2.01"].tags["circuit"]
    print(
        f"PF6 measured: ED2.01 (X) {x_counts['ED2.01']}, ED3.01 (X) {x_counts['ED3.01']}, E2.01 circuit tokens {len(circuit)} (WS1-tokenizer regression values)"
    )
    assert x_counts == {"ED2.01": 34, "ED3.01": 39}
    assert len(circuit) == 74
    assert Counter(t["panel"] for t in circuit) == {"1EELC": 32, "1LI": 30, "1LH": 12}
    assert all(t["circuits"] for t in circuit)  # every tag found its number on this sheet
    attached = sum(1 for t in circuit if t["attached_number"])
    print(
        f"PF6 measured: E2.01 tags with an attached number {attached}, rotated {sum(1 for t in circuit if t['rotation'])}"
    )
    assert attached == 49
    assert tags.occurrences_by_circuit(by["E2.01"].tags)[("1EELC", 1)]
    marks = {k: len(v) for k, v in by["E3.01"].tags["type_marks"].items()}
    print(
        f"PF6 measured: E3.01 type marks {marks}, circuit tokens {len(by['E3.01'].tags['circuit'])}"
    )
    assert marks["L5"] == 7 and marks["L4"] == 3 and marks["L1"] >= 30
    labels = sorted(lab["panel"] for lab in by["E1.00"].tags["panel_labels"])
    assert {"1EELC", "1HD", "1LH", "1LI"} <= set(labels)
    assert all(lab["page"] == 6 for lab in elec.panel_locations)
    assert by["E1.00"].tags["circuit"] == []
    assert by["E2.01"].facts_block_tokens > 0 and by["E2.01"].facts_block_tokens <= 2500


def test_branch_panel_titles_and_plain_word_panel_names_five_star():
    """Five Star (Revit) titles read "Branch Panel: A" and "Branch Panel: B";
    a spec sentence fragment "PANEL AND" is never a panel title (it named a
    phantom panel AND with four breakers on 7172 before the fix)."""
    for line, name in (("Branch Panel: A", "A"), ("Branch Panel: B", "B"), ("DISTRIBUTION PANEL MDP", "MDP"), ("PANEL LA", "LA")):
        m = schedules.PANEL_TITLE_RE.match(line)
        assert m and m.group("name").upper() == name, line
        assert name not in schedules.PANEL_TITLE_STOP_NAMES
    for word in ("AND", "OR", "THE", "TOTALS"):
        m = schedules.PANEL_TITLE_RE.match(f"PANEL {word}")
        assert m is not None and m.group("name").upper() in schedules.PANEL_TITLE_STOP_NAMES
    df = _extract(make_pdf([_panel_sheet(_standard_rows(), title="Branch Panel: A")]))
    assert schedules.panel_names(df.schedules) == {"A"}
    assert df.schedules[0]["panel"]["name"] == "A" and len(df.schedules[0]["panel"]["circuits"]) == 11
    df2 = _extract(make_pdf([_panel_sheet(_standard_rows(), title="PANEL AND")]))
    assert "AND" not in schedules.panel_names(df2.schedules)
    assert not any((s.get("panel") or {}).get("name") == "AND" for s in df2.schedules)

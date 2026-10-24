"""app/services/electrical_items.py: item identity (parse_item), hard
conflicts, canonical units and conversions, and the golden script's
re-exports of the moved names. Pure, deterministic, no DB, no network."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from app.services import electrical_items as ei

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "plans_reader_golden.py"


def _load_golden():
    # Same loading path as tests/test_plans_reader_golden.py (scripts/ is not
    # a package); reuse the module when that test already loaded it.
    if "plans_reader_golden" in sys.modules:
        return sys.modules["plans_reader_golden"]
    spec = importlib.util.spec_from_file_location("plans_reader_golden", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plans_reader_golden"] = mod
    spec.loader.exec_module(mod)
    return mod


# parse_item -------------------------------------------------------------------

def test_duplex_receptacle_is_a_count_item():
    ident = ei.parse_item("Duplex receptacle", "EA")
    assert ident.item_class == "receptacle"
    assert ident.family == "receptacle:duplex"
    assert ident.uom == "EA"
    assert ident.modifiers == ("DUPLEX",)
    assert ei.item_class(ident) == "receptacle"
    assert not ei.is_linear(ident)
    assert not ei.linear_evidence_required(ident)


def test_thhn_conductor_carries_its_size():
    ident = ei.parse_item("#12 THHN/THWN Cu", "FT")
    assert ident.item_class == "conductor"
    assert ident.attrs["conductor_size"] == "12"
    assert ident.attrs["material"] == "CU"
    assert ident.attrs["insulation"] == "THHN/THWN"
    assert ident.uom == "FT"
    assert ei.is_linear(ident)
    assert "conductor" in ei.LINEAR_CLASSES


def test_kcmil_conductor_size():
    ident = ei.parse_item("500 KCMIL XHHW Al", "FT")
    assert ident.item_class == "conductor"
    assert ident.attrs["conductor_size"] == "500KCMIL"
    assert ident.attrs["material"] == "AL"


def test_emt_conduit_size_and_type():
    ident = ei.parse_item('3/4"C EMT', "FT")
    assert ident.item_class == "conduit"
    assert ident.family == "conduit:emt"
    assert ident.attrs["conduit_size"] == "3/4"
    assert ident.attrs["conduit_type"] == "EMT"
    # Spelled-out inches and a unicode fraction land on the same identity.
    same = ei.parse_item("3/4 inch EMT conduit", "LF")
    assert same.item_key == ident.item_key
    assert same.uom == "FT"


def test_conduit_accessories_are_not_linear():
    ident = ei.parse_item('3/4" EMT CONNECTOR', "EA")
    assert ident.item_class == "other"
    assert ident.family == "other:accessory"
    assert not ei.is_linear(ident)


def test_undercabinet_led_is_a_linear_luminaire():
    ident = ei.parse_item("L5 undercabinet LED", "FT")
    assert ident.item_class == "luminaire_linear"
    assert ident.family == "luminaire_linear:undercabinet"
    assert ident.attrs["tag"] == "L5"
    assert ei.linear_evidence_required(ident)
    # FT only on evidence: the class itself is not an always-FT class.
    assert "luminaire_linear" not in ei.LINEAR_CLASSES
    assert not ei.is_linear(ident)
    for word in ("LED strip light", "LED tape light", "linear LED pendant", "cove LED"):
        assert ei.parse_item(word, "EA").item_class == "luminaire_linear", word


def test_lighting_track_removal_is_demolition_with_a_linear_subject():
    # Decision: the demolition section owns the row (material 0, its own
    # labor), and the removed thing is kept as attrs subject plus a linear
    # flag so FT checks still apply to it.
    ident = ei.parse_item("REMOVAL OF LIGHTING TRACK", "FT")
    assert ident.item_class == "demolition"
    assert ident.family == "demolition:lighting_track"
    assert ident.attrs == {"subject": "lighting_track", "linear": "1"}
    assert ei.is_linear(ident)
    fixtures = ei.parse_item("Remove existing light fixtures", "EA")
    assert fixtures.item_class == "demolition"
    assert fixtures.attrs["subject"] == "luminaire"
    assert fixtures.attrs["linear"] == "0"
    assert not ei.is_linear(fixtures)


def test_track_without_head_is_linear_and_track_head_is_a_fixture():
    track = ei.parse_item("Lighting track", "FT")
    assert track.item_class == "lighting_track"
    head = ei.parse_item("Track head, LED", "EA")
    assert head.item_class == "luminaire"


def test_other_classes():
    cases = {
        "20A 1P breaker": "breaker",
        "Panel 'A' 225A MLO 42 CKT": "panel",
        "Disconnect switch 60A 3P NEMA 3R": "panel",
        "Occupancy sensor, ceiling": "control",
        "#22/3C control cable": "cable",
        "CAT6 cable": "cable",
        "MC cable 12/2": "cable",
        "Wiremold raceway": "raceway",
        "Trench and backfill 24\" deep": "trench",
        "Saw cut asphalt": "sawcut",
        "Connection to HVAC unit": "equipment_connection",
        "Single pole switch": "switch",
        "Allowance for permits": "allowance",
        "E3, 2X4 LED troffer": "luminaire",
        "Fire extinguisher": "other",
    }
    for text, cls in cases.items():
        assert ei.parse_item(text, "EA").item_class == cls, text
    assert ei.parse_item("Disconnect switch 60A 3P NEMA 3R", "EA").family == "panel:disconnect"
    assert ei.parse_item("Single pole switch", "EA").attrs["poles"] == "1"


def test_parse_item_is_deterministic_and_keeps_grade_and_modifiers():
    a = ei.parse_item("GFCI WP duplex receptacle, hospital grade, 20A", "EA")
    b = ei.parse_item("GFCI WP duplex receptacle, hospital grade, 20A", "EA")
    assert a == b
    assert a.modifiers == ("DUPLEX", "GFCI", "WP")
    assert a.attrs["grade"] == "hospital"
    assert a.attrs["amps"] == "20"
    plain = ei.parse_item("Duplex receptacle 20A", "EA")
    assert plain.item_key != a.item_key


def test_unknown_unit_does_not_break_parse_item():
    ident = ei.parse_item("Fire extinguisher", "SET")
    assert ident.uom == ""
    assert ident.item_class == "other"


def test_count_key_groups_tagged_fixtures_by_tag():
    a = ei.parse_item("E3, 2X4 LED troffer", "EA")
    b = ei.parse_item("E3, 2X4 LED troffer with battery", "EA")
    assert ei.count_key(a) == "luminaire|tag=E3"
    assert ei.count_key(b) == "luminaire|tag=E3|mod=EMERGENCY"
    rec = ei.parse_item("Duplex receptacle", "EA")
    assert ei.count_key(rec) == rec.item_key


# hard_conflict ----------------------------------------------------------------

def test_hard_conflict_conductor_size():
    a = ei.parse_item("#12 THHN/THWN Cu", "FT")
    b = ei.parse_item("#10 THHN/THWN Cu", "FT")
    c = ei.parse_item("12 AWG THHN copper", "FT")
    assert ei.hard_conflict(a, b)
    assert not ei.hard_conflict(a, c)


def test_hard_conflict_conduit_size_and_type():
    emt34 = ei.parse_item('3/4"C EMT', "FT")
    emt1 = ei.parse_item('1"C EMT', "FT")
    pvc34 = ei.parse_item('3/4"C PVC', "FT")
    assert ei.hard_conflict(emt34, emt1)
    assert ei.hard_conflict(emt34, pvc34)
    assert not ei.hard_conflict(emt34, ei.parse_item("3/4 inch EMT conduit", "LF"))


def test_hard_conflict_voltage():
    a = ei.parse_item("Panel 'A' 120/208V 225A MLO", "EA")
    b = ei.parse_item("Panel 'A' 277/480V 225A MLO", "EA")
    assert ei.hard_conflict(a, b)


def test_hard_conflict_linear_class_versus_count_class_and_units():
    wire = ei.parse_item("#12 THHN/THWN Cu", "FT")
    wire_ea = ei.parse_item("#12 THHN/THWN Cu", "EA")
    recept = ei.parse_item("Duplex receptacle", "EA")
    assert ei.hard_conflict(wire, recept)
    assert ei.hard_conflict(wire, wire_ea)  # an EA row of an always-FT class
    assert not ei.hard_conflict(recept, ei.parse_item("Duplex receptacle", ""))
    # Linear luminaires in EA and FT are decided by evidence, not blocked.
    strip_ea = ei.parse_item("LED strip light", "EA")
    strip_ft = ei.parse_item("LED strip light", "FT")
    assert not ei.hard_conflict(strip_ea, strip_ft)


# Units ------------------------------------------------------------------------

@pytest.mark.parametrize(
    "unit, expected",
    [
        ("EA", "EA"), ("each", "EA"), ("pcs", "EA"),
        ("FT", "FT"), ("LF", "FT"), ("lf", "FT"), ("Lin. Ft.", "FT"), ("feet", "FT"),
        ("C", "C"), ("CLF", "C"), ("M", "M"), ("MFT", "M"),
        ("CU FT", "CU FT"), ("CFT", "CU FT"), ("CY", "CY"), ("cu yd", "CY"),
        ("SQ FT", "SQ FT"), ("SF", "SQ FT"), ("LS", "LS"), ("lump sum", "LS"),
        ("", ""), (None, ""),
    ],
)
def test_canon_uom(unit, expected):
    assert ei.canon_uom(unit) == expected
    if expected:
        assert expected in ei.CANON_UOMS


def test_canon_uom_rejects_unknown_units():
    with pytest.raises(ValueError):
        ei.canon_uom("SET")


def test_convert_unit_value():
    assert ei.convert_unit_value(250.0, "FT", "C") == pytest.approx(2.5)
    assert ei.convert_unit_value(2.5, "C", "FT") == pytest.approx(250.0)
    assert ei.convert_unit_value(1500.0, "LF", "M") == pytest.approx(1.5)
    assert ei.convert_unit_value(3.0, "M", "C") == pytest.approx(30.0)
    assert ei.convert_unit_value(40.0, "LF", "FT") == 40.0
    assert ei.convert_unit_value(7.0, "EA", "EA") == 7.0
    for src, dst in (("EA", "FT"), ("CY", "FT"), ("LS", "EA"), ("SQ FT", "CU FT")):
        with pytest.raises(ValueError):
            ei.convert_unit_value(1.0, src, dst)
    with pytest.raises(ValueError):
        ei.convert_unit_value(1.0, "", "FT")


def test_linear_classes_per_spec():
    assert ei.LINEAR_CLASSES == {
        "conductor", "conduit", "cable", "raceway", "trench", "sawcut", "lighting_track"
    }
    assert set(ei.ITEM_CLASSES) >= ei.LINEAR_CLASSES | {"luminaire_linear", "other"}


# Script re-exports ------------------------------------------------------------

def test_golden_script_reexports_the_moved_names():
    golden = _load_golden()
    assert golden.BoqRow is ei.BoqRow
    assert golden.similarity is ei.similarity
    assert golden.normalize_description is ei.normalize_description
    assert golden.normalize_unit is ei.normalize_unit
    assert golden.key_tokens is ei.key_tokens
    assert golden.UNITS is ei.UNITS
    assert golden.MATCH_THRESHOLD == ei.MATCH_THRESHOLD
    for name in (
        "_FRACTIONS", "_QUOTES", "_DASHES", "_clean_text", "_strip_caveats", "_normalize",
        "_heading_name", "_canonical", "_KEY_PATTERNS", "_TAG_RE", "_POLE_WORDS", "_MODIFIERS",
        "_CONFLICT_PENALTY", "_VOLTAGE_RE", "_SYNONYMS", "_ratio_text", "_KNOWN_SUBGROUPS",
        "_SUBGROUP_PARENT", "_ACCESSORY_WORDS", "_CAVEAT_WORDS", "_UNIT_ALIASES",
    ):
        assert getattr(golden, name) is getattr(ei, name), name
    # The identity module stands on its own: it imports nothing from scripts/
    # and holds no em or en dash (CLAUDE.md).
    src = Path(ei.__file__).read_text(encoding="utf-8")
    assert not re.search(r"^\s*(?:from|import)\s+scripts\b", src, flags=re.M)
    assert chr(0x2014) not in src and chr(0x2013) not in src


def test_conduit_accessory_counts_follow_the_renderer_ratios():
    """Spec 3.4 / E23: 6,337.71 FT EMT x 1.10 = 6,971.48 FT -> 140 connectors,
    698 couplings, 698 straps, 349 anchors, 349 screws, 70 boxes; the
    renderer's rows carry the same numbers and accessory_role reads the
    estimator abbreviations ("Compression Conn Stl" is a connector)."""
    from app.services import plans_reader_render as render

    counts = ei.conduit_accessory_counts(6971.481)
    assert counts == {"connectors": 140, "couplings": 698, "straps": 698, "anchors": 349, "screws": 349, "boxes": 70}
    fittings, straps = render.conduit_fittings('3/4"C EMT', 6971.481)
    assert [q for _d, q, _u in fittings] == [140, 698, 698] and straps == 698
    assert [q for _d, q, _u in render.other_conduit_accessories(straps)] == [349, 349, 70]
    assert [ei.accessory_role(d) for d, _q, _u in fittings] == ["connector", "coupling", "strap"]
    assert [ei.accessory_role(d) for d, _q, _u in render.other_conduit_accessories(straps)] == ["anchor", "screw", "box"]
    assert ei.conduit_accessory_counts(7175.19) == {"connectors": 144, "couplings": 718, "straps": 718, "anchors": 359, "screws": 359, "boxes": 72}
    assert ei.conduit_accessory_counts(0) == {k: 0 for k in counts} and ei.conduit_accessory_counts("x") == {k: 0 for k in counts}
    assert ei.accessory_role('3/4"C EMT') is None and ei.accessory_role('3/4" ELBOW 90 DEG - RMC - GALV') == "elbow"
    assert ei.accessory_role("3/4\" CONNECTORS - EMT") == "connector" and ei.accessory_role("") is None
    assert ei.CONDUIT_ACCESSORY_RATIOS["connector_ft"] == 50 and ei.CONDUIT_ACCESSORY_RATIOS["coupling_ft"] == 10


def test_identity_matches_and_luminaire_variants():
    """identity_matches: the same class and identifying attribute (the rule
    behind the consolidator's measured-item naming); luminaire_variant and
    luminaire_tag_parts: the EM / NL words and tag suffixes."""
    track = ei.parse_item("REMOVAL OF LIGHTING TRACK", "FT")
    assert ei.identity_matches(ei.parse_item("REMOVE EXISTING TRACK-LIGHTING ASSEMBLIES", "FT"), track)
    assert not ei.identity_matches(ei.parse_item("REMOVE EXISTING TRACK-LIGHTING ASSEMBLIES INCLUDING HEADS", "FT"), track)
    assert ei.identity_matches(ei.parse_item("#12 THHN CU", "FT"), ei.parse_item("#12 THHN/THWN Cu", "FT"))
    assert not ei.identity_matches(ei.parse_item("#10 THHN/THWN CU", "FT"), ei.parse_item("#12 THHN/THWN Cu", "FT"))
    assert ei.identity_matches(ei.parse_item('3/4"C EMT', "FT"), ei.parse_item('3/4"C EMT CONDUIT', "FT"))
    assert not ei.identity_matches(ei.parse_item('1"C EMT', "FT"), ei.parse_item('3/4"C EMT', "FT"))
    assert not ei.identity_matches(ei.parse_item('3/4"C RGS', "FT"), ei.parse_item('3/4"C EMT', "FT"))
    assert ei.identity_matches(ei.parse_item("L5 UNDERCABINET LED LIGHTING", "FT"), ei.parse_item("L5 - UNDERCABINET LED LIGHTING (CONTROL LOCAL)", "FT"))
    assert not ei.identity_matches(ei.parse_item("UNDERCABINET LED LIGHTING", "FT"), ei.parse_item("L5 - UNDERCABINET LED LIGHTING", "FT"))
    assert ei.identity_matches(ei.parse_item("DUPLEX RECEPTACLE, 125V", "EA"), ei.parse_item("DUPLEX RECEPTACLE", "EA"))
    assert not ei.identity_matches(ei.parse_item("DUPLEX RECEPTACLE", "EA"), ei.parse_item("#12 THHN/THWN Cu", "FT"))
    assert ei.luminaire_variant("2x4 recessed LED troffer, shaded emergency/life-safety branch") == "EM"
    assert ei.luminaire_variant("New round luminaire type L3, unshaded") is None
    assert ei.luminaire_variant("Type A with night light") == "NL" and ei.luminaire_variant("EM battery pack") == "EM"
    assert ei.luminaire_tag_parts("L1-EM") == ("L1", "EM") and ei.luminaire_tag_parts("l1") == ("L1", None) and ei.luminaire_tag_parts("X1-NL") == ("X1", "NL")
    assert ei.LUMINAIRE_CLASSES == {"luminaire", "luminaire_linear", "lighting_track"}


def test_demolition_primary_noun_decides_the_subject_over_a_trailing_wire_clause():
    """E22 remove-to-source rows: devices removed with their wire and conduit
    are priced by the each (the first live WS4 iteration classed the
    canonical power removal row as demolition of conductor, so linear_in_ea
    and arithmetic blocked it). Only a row whose primary noun is wire,
    cable, conduit, raceway or track is linear."""
    power = ei.parse_item("REMOVAL OF POWER DEVICES W/ BACKBOX & ASSOCIATE WIRE, CONDUIT BACK TO SOURCE", "EA")
    assert power.item_class == "demolition" and power.attrs["linear"] == "0" and not ei.is_linear(power)
    lighting = ei.parse_item("REMOVAL OF LIGHTING FIXTURE AND CONTROLS W/ ASSOCIATE WIRE & CONDUIT BACK TO SOURCE", "EA")
    assert lighting.attrs == {"subject": "luminaire", "linear": "0"}
    recept = ei.parse_item("REMOVE EXISTING DUPLEX RECEPTACLES, WIRE AND CONDUIT BACK TO SOURCE", "EA")
    assert recept.attrs["subject"] == "receptacle" and not ei.is_linear(recept)
    floor = ei.parse_item("REMOVAL OF FLOOR BOX / FLOOR OUTLET ASSEMBLY W/ ASSOCIATE WIRE, CONDUIT BACK TO SOURCE", "EA")
    assert not ei.is_linear(floor)
    for text in (
        "REMOVE EXISTING WIRE AND CONDUIT BACK TO PANEL", "REMOVAL OF 3/4\" CONDUIT AND WIRE",
        "REMOVE ABANDONED WIRE AND CONDUIT", "REMOVE EXISTING WIREMOLD RACEWAY AND DEVICES",
        "REMOVE EXISTING LIGHTING TRACK ROW", "REMOVE LIGHTING TRACK ROW",
    ):
        ident = ei.parse_item(text, "FT")
        assert ident.item_class == "demolition" and ei.is_linear(ident), text


# Device vocabulary (spec 3.4; Z2) ----------------------------------------------

def test_parse_device_names_the_estimator_vocabulary():
    """Every vocabulary name is its own canonical form; the 7154 run 2
    wordings (rows C50 and C52) read as DIMMER SWITCH and DIMMER &
    OCCUPANCY SENSOR SWITCH; "occupancy-sensor OVERRIDE dimmers" are plain
    dimmers; an exclusion clause is dropped before the modifiers are read;
    unpriced modifiers (emergency, normal, 125V, hospital grade, locations)
    never reach the name; placement decides sensor vs sensor switch."""
    for name in ei.DEVICE_VOCABULARY:
        assert ei.canonical_device_description(name) == name, name
    cases = {
        "WALL DIMMER, INCLUDING OCCUPANCY-SENSOR OVERRIDE DIMMERS": "DIMMER SWITCH",
        "Wall dimmer, including occupancy-sensor override dimmers": "DIMMER SWITCH",
        "WALL OCCUPANCY-SENSING DIMMER": "DIMMER & OCCUPANCY SENSOR SWITCH",
        "Wall occupancy-sensing dimmer": "DIMMER & OCCUPANCY SENSOR SWITCH",
        "Wall occupancy-sensor dimmer": "DIMMER & OCCUPANCY SENSOR SWITCH",
        "Wall dimmer, excluding occupancy-integral and lockable-cover dimmers": "DIMMER SWITCH",
        "Wall dimmer with lockable cover": "DIMMER SWITCH W/ LOCKABLE COVER",
        "Dimmer switch, 3-way": "DIMMER SWITCH",
        "Wall occupancy-sensor switch": "OCCUPANCY SENSOR SWITCH",
        "Wall switch occupancy sensor": "OCCUPANCY SENSOR SWITCH",
        "Ceiling-mounted dual-technology occupancy sensor": "OCCUPANCY SENSOR",
        "Occupancy sensor, corner mount": "OCCUPANCY SENSOR",
        "Duplex receptacle, 125V, emergency circuit": "DUPLEX RECEPTACLE",
        "DUPLEX RECEPTACLE, 125V, HOSPITAL GRADE": "DUPLEX RECEPTACLE",
        "GFCI duplex receptacle, 125V, normal circuit": "DUPLEX RECEPTACLE, GFCI",
        "GFCI duplex receptacle for refrigerator, emergency circuit": "DUPLEX RECEPTACLE, GFCI",
        "Tamper-resistant duplex receptacle": "DUPLEX RECEPTACLE, TR",
        "DUPLEX RECEPTACLE, 125V, TR": "DUPLEX RECEPTACLE, TR",
        "Occupancy-controlled duplex receptacle, normal circuit": "DUPLEX RECEPTACLE CONTROLLED BY OCCUPANCY",
        "Occupancy-controlled GFCI duplex receptacle": "DUPLEX RECEPTACLE CONTROLLED BY OCCUPANCY, GFCI",
        "DUPLEX RECEPTACLE, GFCI, OCCUPANCY CONTROLLED": "DUPLEX RECEPTACLE CONTROLLED BY OCCUPANCY, GFCI",
        "Occupancy-controlled quad receptacle, normal circuit": "QUADPLEX RECEPTACLE CONTROLLED BY OCCUPANCY",
        "Quad receptacle for TV, normal circuit": "QUADPLEX RECEPTACLE",
        "New quad receptacle for television": "QUADPLEX RECEPTACLE",
        "New duplex receptacle, open symbol, without G or O prefix": "DUPLEX RECEPTACLE",
        "Electrical connection to new fire/smoke damper": "ELECTRICAL CONNECTION",
        "Electrical connection to equipment": "ELECTRICAL CONNECTION",
    }
    for text, expected in cases.items():
        assert ei.canonical_device_description(text) == expected, text
    # not standard devices: the caller keeps the model's wording
    for text in (
        "LOCKABLE DIMMER COVER", "Dimmer rack", "0-10V dimming module", "Single pole switch",
        "Remove existing duplex receptacle", "Existing roll-up door electrical connections to remain",
        "New thermostat sensor location with wiring and conduit to new VV", "Relocate existing thermostat sensors",
        "Simplex receptacle, 20A", "NEMA L6-30R receptacle",
        "Duplex receptacle, WP, in-use cover", "USB duplex receptacle", "Photocell", "Daylight sensor",
        "Occupancy sensor power pack", "Service connection", "Disconnect switch 30A",
        "#12 THHN/THWN Cu", "L1 - 2X4 RECESSED TROFFER", "",
    ):
        assert ei.parse_device(text) is None, text
    # 7172 (wave 3): floor boxes and low-voltage outlets joined the vocabulary
    assert ei.canonical_device_description("Duplex receptacle in floor box") == "FLOOR BOX RECEPTACLE"
    assert ei.canonical_device_description("Data outlet") == "TELE/DATA OUTLET"
    assert ei.canonical_device_description("TV outlet") == "TV OUTLET"
    dev = ei.parse_device("Wall occupancy-sensing dimmer")
    assert (dev.family, set(dev.modifiers), dev.description) == ("dimmer", {"OCC"}, "DIMMER & OCCUPANCY SENSOR SWITCH")
    assert ei.device_description("dimmer", {"OCC", "LOCK"}) == "DIMMER & OCCUPANCY SENSOR SWITCH"  # OCC beats LOCK
    assert ei.device_description("receptacle:duplex", ["gfci", "tr"]) == "DUPLEX RECEPTACLE, GFCI, TR"
    with pytest.raises(ValueError):
        ei.device_description("luminaire")
    assert ei.same_device_wording("DUPLEX RECEPTACLE, 125V", "DUPLEX RECEPTACLE")
    assert ei.same_device_wording("Dimmer switch w/ lockable cover", "DIMMER SWITCH W/ LOCKABLE COVER")
    assert not ei.same_device_wording("WALL DIMMER", "DIMMER SWITCH")
    src = Path(ei.__file__).read_text(encoding="utf-8")
    assert chr(0x2014) not in src and chr(0x2013) not in src


def test_a_negated_gfci_never_keys_as_gfci():
    from app.services import electrical_items as ei

    n = ei.normalize_description
    assert ("mod", "GFCI") not in ei.key_tokens(n("Duplex receptacle, 125V, normal circuit, non-GFCI symbol"))
    assert ("mod", "GFCI") not in ei.key_tokens(n("DUPLEX RECEPTACLE, NON GFCI"))
    assert ("mod", "GFCI") in ei.key_tokens(n("GFCI duplex receptacle"))
    assert ("mod", "GFCI") in ei.key_tokens(n("DUPLEX RECEPTACLE, 125V, GFCI"))


def test_track_subject_reads_the_primary_noun_and_guards_heads():
    """Spec 3.5 E15: the 7154 track rows of runs 51ec302b, 2492ba7a and b87f0ffd."""
    from app.services.electrical_items import is_lighting_track, track_subject

    track = ["Remove existing track-lighting assemblies", "Remove existing lighting track assemblies",
             "Remove existing track-lighting assemblies including heads", "REMOVE EXISTING LIGHTING TRACK AND HEADS",
             "REMOVE LIGHTING TRACK ROW", "LIGHTING TRACK", "REMOVE TRACK AND HEADS"]
    heads = ["Visible existing track-lighting heads to remove", "Remove existing track-light heads, components of counted tracks",
             "Remove existing track-light heads \u2014 visible subset", "REMOVE TRACK-LIGHT HEAD ASSEMBLY"]
    for d in track:
        assert track_subject(d) == "track" and is_lighting_track(ei.parse_item(d, "EA")), d
    for d in heads:
        assert track_subject(d) == "head" and not is_lighting_track(ei.parse_item(d, "EA")), d
    assert track_subject("REMOVE CABLE TRAY") is None and track_subject("DUPLEX RECEPTACLE") is None
    # A track word on a non-lighting subject (strut) is never a lighting track.
    assert not is_lighting_track(ei.parse_item("REMOVE UNISTRUT TRACK", "EA"))
    # parse_item itself is unchanged: the b87f0ffd row keeps its stored item_key.
    assert ei.parse_item("Remove existing track-lighting assemblies including heads", "EA").item_key == "demolition:luminaire|linear=0|subject=luminaire"


# 7172 (Five Star) vocabulary: gear by rating, outlets, legend marks ------------------

def test_a_wire_count_phrase_never_makes_gear_a_conductor():
    """"3-phase, 4-wire" names the system; the panel stays a panel (7172 C14
    and C15 classed as conductors and were refused in EA), while a real
    conductor row keeps its class."""
    panel = ei.parse_item("New panel A, 200A, 277/480V, 3-phase, 4-wire, 42 circuits, surface Type 1, 14kAIC", "EA")
    assert (panel.item_class, panel.family, panel.attrs.get("panel")) == ("panel", "panel:panel", "A")
    assert ei.parse_item("Existing main switchgear MSA, 2000A, 480/277V, 3-phase, 4-wire, 65kAIC, NEMA 1", "EA").item_class == "panel"
    assert ei.parse_item("4-WIRE FEEDER #12 THHN", "FT").item_class == "conductor"
    assert ei.parse_item("#12 THHN/THWN Cu wire", "FT").item_class == "conductor"


def test_hyphenated_lighting_control_and_low_voltage_outlets_class():
    assert ei.parse_item("Wall-mounted lighting-control symbols marked S/OS", "EA").item_class == "control"
    assert ei.parse_item("Green triangular low-voltage outlet symbols, type unconfirmed", "EA").family == "other:low_voltage_outlet"
    assert ei.parse_item("TV outlet locations", "EA").family == "other:low_voltage_outlet"


def test_parse_device_names_outlets_floor_boxes_and_legend_marks():
    """The 7172 E2.00 symbol rows read as the estimator's devices: tele/data
    and TV outlets, the floor box, the quad family, and the S/OS and M
    lighting-control marks (wall switch sensor, ceiling sensor)."""
    assert ei.canonical_device_description("Green triangular low-voltage outlet symbols, type unconfirmed") == "TELE/DATA OUTLET"
    assert ei.canonical_device_description("Data outlet, dual port") == "TELE/DATA OUTLET"
    assert ei.canonical_device_description("TV outlet locations") == "TV OUTLET"
    assert ei.canonical_device_description("Conference-table floor-box receptacle symbols") == "FLOOR BOX RECEPTACLE"
    assert ei.canonical_device_description("Four-line receptacle symbols, apparent quad receptacle family") == "QUADPLEX RECEPTACLE"
    assert ei.canonical_device_description("Two-line receptacle symbols, half-filled circle") == "DUPLEX RECEPTACLE"
    assert ei.canonical_device_description("Wall-mounted lighting-control symbols marked S/OS") == "OCCUPANCY SENSOR SWITCH"
    assert ei.canonical_device_description("Lighting-control symbols marked M") == "OCCUPANCY SENSOR"
    assert ei.canonical_device_description("Ceiling lighting-control symbols marked OS") == "OCCUPANCY SENSOR"
    # not devices: a cable, a rack, a power outlet of unknown kind, a mark outside a lighting-control text
    assert ei.parse_device("CAT6 data cable to outlet") is None
    assert ei.parse_device("Circular power-outlet symbols with internal rectangle") is None
    assert ei.parse_device("Door hardware marked M") is None
    for name in ("FLOOR BOX RECEPTACLE", "TELE/DATA OUTLET", "TV OUTLET"):
        assert ei.canonical_device_description(name) == name
    assert ei.device_description("receptacle:floor", {"GFCI"}) == "FLOOR BOX RECEPTACLE, GFCI"


def test_gear_description_reads_the_ratings_or_gives_none():
    """Breakers, disconnects and transformers are named by their ratings in
    the estimator's form (the scorer keys gear on those tokens); text
    without the ratings keeps its wording (None), and a panel is composed
    elsewhere from its schedule header."""
    assert ei.gear_description("Panel A branch breaker, 20A 1-pole") == "20A/1P CIRCUIT BREAKER"
    assert ei.gear_description("Panel B branch breaker, 30A 2-pole") == "30A/2P CIRCUIT BREAKER"
    assert ei.gear_description("Panel A branch breaker, 100A 3-pole") == "100A/3P CIRCUIT BREAKER"
    assert ei.gear_description("20A single-pole GFCI breaker") == "20A/1P CIRCUIT BREAKER, GFCI"
    assert ei.gear_description("New 75 kVA dry-type transformer T-B, 480V to 120/208V three-phase") == "75KVA TRANSFORMER"
    assert ei.gear_description("75 kVA transformer T-B mounted above panel B") == "75KVA TRANSFORMER"
    assert ei.gear_description("30A/3P disconnect switch, NEMA 3R") == "30A/3P DISCONNECT SWITCH, NEMA 3R"
    assert ei.gear_description("60A 2P fused disconnect, NEMA 1") == "60A/2P FUSED DISCONNECT SWITCH, NEMA 1"
    assert ei.gear_description("Rooftop-unit disconnect symbols; ratings not shown") is None
    assert ei.gear_description("Circuit breaker") is None
    assert ei.gear_description("New panel A, 200A, 277/480V, 3-phase, 4-wire") is None
    assert ei.gear_description("Removal of 30A/3P disconnect switch") is None
    assert ei.gear_ratings("Rooftop-unit disconnect symbols; ratings not shown") == {"outdoor": True}
    assert ei.gear_ratings("30A/3P fused disconnect, NEMA 3R, GFCI") == {"amps": 30, "poles": 3, "nema": "3R", "fused": True, "gfci": True}
    assert ei.disconnect_description(30, 2, nema="3R") == "30A/2P DISCONNECT SWITCH, NEMA 3R"
    assert ei.disconnect_description(20, 2, nema="1") == "20A/2P DISCONNECT SWITCH, NEMA 1"
    assert ei.disconnect_description(None, None) == "DISCONNECT SWITCH"
    assert ei.transformer_description(75.0) == "75KVA TRANSFORMER" and ei.transformer_description(112.5) == "112.5KVA TRANSFORMER"
    assert ei.breaker_description(20, 1, gfci=True, afci=True) == "20A/1P CIRCUIT BREAKER, GFCI, AFCI"
    # the names classify to the 7172 truth keys
    from app.services import plans_reader_score as S

    expect = {
        "20A/1P CIRCUIT BREAKER": ("breaker", {"20A/1P"}),
        "30A/3P DISCONNECT SWITCH, NEMA 3R": ("disconnect", {"30A/3P", "NEMA3R"}),
        "20A/2P DISCONNECT SWITCH, NEMA 1": ("disconnect", {"20A/2P", "NEMA1"}),
        "75KVA TRANSFORMER": ("transformer", {"75KVA"}),
    }
    for desc, (kind, attrs) in expect.items():
        key = S.classify_line(desc, "NEW WORK", "SWITCHGEAR", "EA")
        assert (key.kind, set(key.attrs)) == (kind, attrs), desc
    for desc, (kind, attrs) in {"TELE/DATA OUTLET": ("low_voltage", {"tele_data_outlet"}), "TV OUTLET": ("low_voltage", {"tv_outlet"})}.items():
        key = S.classify_line(desc, "NEW WORK", "LOW VOLTAGE WORK", "EA", subgroup="OUTLETS/DEVICES")
        assert (key.kind, set(key.attrs)) == (kind, attrs), desc


# Library-facing classification of the sent BOQs (LOPO golden band) ------------------
#
# The cost library and scripts/pricing_eval.py classify every estimator line
# with parse_item, so a vocabulary rule added for the readers moves which
# library items a golden line prices from. These pin the estimator lines
# whose class the 7172 vocabulary had moved (measured over the 68 backup
# workbooks: 14 line keys moved, the golden Verify band fell from 63.6% to
# 54.5% within 10%), while the reader wording keeps its class.

def test_plural_lighting_controls_keeps_the_pre_reader_class_on_estimator_lines():
    demo = ei.parse_item("REMOVAL OF LIGHT FIXTURE & LIGHTING CONTROLS", "EA")
    assert (demo.item_class, demo.attrs["subject"]) == ("demolition", "luminaire")
    assert ei.parse_item("DISCONNECT EXISTING LIGHTING CONTROLS FOR LIGHT FIXTURE", "EA").family == "panel:disconnect"
    demo2 = ei.parse_item("DISCONNECT EXISTING LIGHTING, LIGHTING CONTROLS, AND ASSOCIATED BRANCH CIRCUIT CONDUCTORS AND RACEWAYS BACK TO SOURCE FOR COMPLETE DEMOLITION", "SQ FT")
    assert (demo2.item_class, demo2.attrs["subject"]) == ("demolition", "panel")
    # the reader's singular, hyphenated or not, is still a control
    assert ei.parse_item("Wall-mounted lighting-control symbols marked S/OS", "EA").item_class == "control"
    assert ei.parse_item("LIGHTING CONTROL DEVICES", "EA").item_class == "control"
    assert ei.parse_item("Lighting control station", "EA").item_class == "control"
    # the device naming path keeps the plural vocabulary
    assert ei.canonical_device_description("Lighting controls marked S/OS") == "OCCUPANCY SENSOR SWITCH"


def test_a_disconnect_and_reconnect_is_not_a_new_equipment_connection():
    text = "ELECTRICAL CONNECTIONS WOULD BE DISCONNECTED AND RECONNECTED AFTER UNITS ARE SET. THIS WOULD CONSIST OF AHU'S, SPLIT-SYSTEMS, AND EXHAUST FANS"
    assert ei.parse_item(text, "EA").item_class == "other"
    assert ei.parse_item("RECONNECT POWER TO EXISTING ROOFTOP UNIT", "EA").item_class == "other"
    assert ei.parse_item("DISCONNECT AND RE-CONNECT EXISTING EQUIPMENT WHIPS", "EA").item_class == "panel"  # DISCONNECT wins as before
    # the plural count rows of the readers and the singular estimator lines stay connections
    for desc in ("Rooftop-unit electrical connections, RTU-1 through RTU-6", "ROOFTOP UNIT POWER CONNECTIONS",
                 "Exhaust-fan-type electrical connections", "Connection to HVAC unit", "ELECTRICAL CONNECTION"):
        assert ei.parse_item(desc, "EA").item_class == "equipment_connection", desc
    assert ei.canonical_device_description("Rooftop-unit electrical connections, RTU-1 through RTU-6") == "ELECTRICAL CONNECTION"
    # the device naming path (reader count rows) is untouched by the library precedence
    assert ei.canonical_device_description(text) == "ELECTRICAL CONNECTION"


def test_gear_wire_count_and_low_voltage_outlet_rules_stay_for_estimator_lines():
    """The two other 7172 rules move 8 estimator line keys (five 91c65c7c
    panels and a generator out of conductor, a relay panel to control, a
    low-voltage outlet out of receptacle) with no effect on any LOPO
    metric, and the new classes are the right ones, so they stay."""
    panel = ei.parse_item("PANEL EH1, 225A MCB, 225A BUSS RATING, 480/277 WYE, 3 PHASE, 4 WIRE, 65000 AIC RATING, SURFACE MOUNTED, NEMA 1", "EA")
    assert (panel.item_class, panel.attrs.get("panel"), panel.attrs.get("amps")) == ("panel", "EH1", "225")
    assert ei.parse_item("RELOCATION OF LOW VOLTAGE 4-PORT OUTLET TO NEW LOCATION", "EA").family == "other:low_voltage_outlet"

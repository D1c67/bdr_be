"""Scorer v2 for AI Estimating BOQs (docs/AI_ESTIMATING_BUILD_SPEC.md WS0,
sections 3.4 to 3.6, E25).

What it fixes over the v1 scorer in scripts/plans_reader_golden.py (E25):

- `classify_line` puts every BOQ line into a closed kind vocabulary with the
  identity attributes that make a different priced line (fixture tag plus EM
  or NL variant, receptacle form plus priced modifiers, breaker amps and
  poles, conductor size and class, conduit size and type, ...). Style words
  the estimator does not price (EMERGENCY, DEDICATED, 125V, colours, the
  NEED TO CONFIRM caveats) are dropped, so a split row still lands on its
  truth line. Aliases are data: the global EM and NL sets here plus a case
  alias file (7154: SHADED means EM).
- `assign` is many-to-one: several AI rows may land on one truth line and are
  summed when their unit matches (unit mismatches are counted, never
  summed). An AI row that is MORE specific than a truth line (extra priced
  modifiers) is a child of that line; a row that is less specific, or that
  conflicts in kind or identity (electrical_items.hard_conflict), never
  matches. Demolition rows never pair with new-work rows.
- Metrics are the ones of section 3.6: count lines exact (EXACT_TOLERANCE
  relative) over count lines total, length bands on wastage-inclusive
  quantities (good <= 10%, acceptable <= 25%), dollar coverage and the
  dollar-weighted error with the estimator line value v, bid error against
  the G3 sent, corrected and estimator totals. A refused priced estimate is
  a `safe_refusal` for the bid and counts as a miss.

Dollar coverage, two readings (integrator decision, recorded in the build
log Deviations). `dollar_coverage` is the section 3.6 definition: the value
v of truth lines that are matched AND in band (count exact, or length
good/acceptable) over sum(v). The 1.3 item 1 smoke gate (>= 0.85) reads it.
`dollar_coverage_partial` is the prototype formula sum(min(ai, truth) x v)
/ sum(v) over same-unit assigned quantities, which gives partial credit to
out-of-band lines; the E25 baseline 0.319 +/- 0.005 was measured with it,
so the E25 check reads this field. Reports print both.

This module is pure: no database, no model, no network. It imports item
identity from electrical_items and nothing from scripts/. The estimator
workbook reader (`parse_estimator_boq`) and the BOQ spec flattener
(`rows_from_spec`) moved here from scripts/plans_reader_golden.py, which
should re-export them (hook edit for the integrator).

Reports never print dollar amounts unless asked (`include_dollars=True`),
so a committed test or a shared markdown never carries real pricing data.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import openpyxl

from app.services.electrical_items import (
    BOQ_GROUPS,
    BOQ_LEVEL0,
    EXACT_TOLERANCE,
    WASTAGE_FACTOR,
    BoqRow,
    _ACCESSORY_WORDS,
    _canonical,
    _clean_text,
    _heading_name,
    _KNOWN_SUBGROUPS,
    _SUBGROUP_PARENT,
    hard_conflict,
    normalize_description,
    normalize_unit,
    parse_item,
    similarity,
)

MATCHER_VERSION = 2

# Length bands (3.6) on wastage-inclusive quantities.
GOOD_BAND = 0.10
ACCEPTABLE_BAND = 0.25
# Bid and hours bands (WS0 design 5.3): good <= 5%, acceptable <= 10%.
BID_GOOD_BAND = 0.05
BID_ACCEPTABLE_BAND = 0.10

DEFAULT_BACKUP_DIR = Path("/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01")

# Kinds -------------------------------------------------------------------------

KINDS: tuple[str, ...] = (
    "fixture",
    "pole_light",
    "receptacle",
    "switch",
    "dimmer",
    "occ_sensor",
    "lighting_control_other",
    "equipment_connection",
    "breaker",
    "panelboard",
    "disconnect",
    "transformer",
    "service_equipment",
    "ups",
    "ev_charger",
    "conductor",
    "control_cable",
    "cable",
    "conduit",
    "raceway",
    "box",
    "fire_alarm",
    "low_voltage",
    "grounding",
    "trench",
    "sawcut",
    "concrete",
    "pole",
    "thermostat",
    "demo_lighting",
    "demo_power",
    "demo_track",
    "demo_other",
    "allowance",
    "other",
)
DEMO_KINDS: frozenset[str] = frozenset({"demo_lighting", "demo_power", "demo_track", "demo_other"})
# Fixture kinds whose single tag attr may carry an EM or NL variant suffix.
FIXTURE_KINDS: frozenset[str] = frozenset({"fixture", "pole_light"})
# Tier of a variant fallback pair (after every modifier tier): an AI fixture
# tag without the variant the truth text implies (or with one the truth text
# lacks) when the case prices only one variant of that tag.
VARIANT_FALLBACK_TIER = 90
_VARIANT_ATTR_RE = re.compile(r"^(.+)-(EM|NL)$")
# Kinds whose attrs carry the text slug when nothing else identifies the
# line (gear and site work); simple devices keep empty attrs so that an AI
# row worded differently still lands on the truth line.
_SLUG_FALLBACK_KINDS: frozenset[str] = frozenset(
    {"panelboard", "disconnect", "transformer", "service_equipment", "breaker", "cable", "raceway",
     "trench", "sawcut", "concrete", "pole", "box", "grounding", "ups", "ev_charger"}
)

# Global alias sets: a word in the text after a fixture tag that marks the
# EM (life safety) or NL (night light) variant of the tag. Case files add to
# these (7154: {"SHADED": "EM"}, the E0.00 legend's shaded luminaire).
GLOBAL_ALIASES: dict[str, str] = {
    "EM": "EM",
    "EMERGENCY": "EM",
    "LIFE SAFETY": "EM",
    "LIFE-SAFETY": "EM",
    "BATTERY": "EM",
    "EGRESS": "EM",
    "NL": "NL",
    "NIGHT LIGHT": "NL",
    "NIGHTLIGHT": "NL",
}
VARIANTS: tuple[str, ...] = ("EM", "NL")

# Priced modifiers, in tie-break priority order (highest first). When an AI
# row carries extra modifiers over several candidate truth lines, it goes to
# the line that keeps the HIGHEST-priority ones (it loses the lowest).
MODIFIER_PRIORITY: tuple[str, ...] = (
    "QUAD",
    "DUPLEX",
    "SINGLE",
    "TRIPLEX",
    "SPECIAL",
    "GFCI",
    "TR",
    "HG",
    "CTRL",
    "SWITCHED",
    "HALF_SWITCHED",
    "WP",
    "USB",
    "FLOOR",
    "CEILING",
    "IG",
    "LOCK",
    "OCC",
    "VAC",
    "AFCI",
    "FUSED",
    "3W",
    "4W",
    "KEY",
    "DP",
    "PILOT",
    "TIMER",
    "MOTOR",
    "EPO",
    "DT",
    "LV",
)
_PRIORITY_INDEX = {m: i for i, m in enumerate(MODIFIER_PRIORITY)}

_RECEPTACLE_MODIFIERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GFCI", re.compile(r"\bGFC?I\b|\bGROUND FAULT\b")),
    ("TR", re.compile(r"\bTR\b|\bTAMPER\b")),
    ("HG", re.compile(r"\bHOSPITAL GRADE\b|\bHG\b")),
    ("CTRL", re.compile(r"\bCONTROLLED\b")),
    ("HALF_SWITCHED", re.compile(r"\bHALF[\s-]?SWITCHED\b|\bSPLIT[\s-]?WIRED\b|\bSPLIT\b")),
    ("SWITCHED", re.compile(r"\bSWITCHED\b")),
    ("WP", re.compile(r"\bWP\b|\bWEATHER ?PROOF\b|\bWEATHER RESISTANT\b|\bWR\b|\bIN[\s-]?USE COVER\b")),
    ("USB", re.compile(r"\bUSB\b")),
    ("FLOOR", re.compile(r"\bFLOOR\b|\bPOKE[\s-]?THRU\b")),
    ("CEILING", re.compile(r"\bCEILING\b")),
    ("IG", re.compile(r"\bISOLATED GROUND\b|\bIG\b")),
)
_RECEPTACLE_RE = re.compile(
    r"\bRECEPTACLES?\b|\bRECEPT\b|\bRECEPTICLE\b|\bRECEPTCLE\b|\bRECEP\b|\bDUPLEX\b|"
    r"\bQUAD(?:RU)?PLEX\b|\bFOURPLEX\b|\bTRIPLEX\b|\bGFC?I\b|\bOUTLET\b"
)
_RECEPTACLE_STRONG_RE = re.compile(r"\bRECEPT\w*\b|\bRECEP\b|\bDUPLEX\b|\bQUAD(?:RU)?PLEX\b|\bFOURPLEX\b|\bTRIPLEX\b")
_LV_OUTLET_RE = re.compile(
    r"\bDATA\b|\bTELE\b|\bTELEPHONE\b|\bPHONE\b|\bVOICE\b|\bTV\b|\bCATV\b|\bAV\b|\bCOMM\b|\bCHARGING OUTLET\b"
)
_NEMA_CONFIG_RE = re.compile(r"\b(L?\d{1,2}-\d{2}[RP])\b")
_AMPS_RE = re.compile(r"\b(\d{1,4})\s?A(?:MP|MPS)?\b|\b(\d{1,4})\s?AT\b|\b(\d{1,4})\s?AS\b")
_POLES_RE = re.compile(r"\b(\d{1,2})[\s-]?(?:P|POLES?)\b")
_AP_RE = re.compile(r"\b(\d{1,4})\s?(?:A|AT|AS)?\s*/\s*(?:(\d{1,4})\s?AF\s*/\s*)?(\d)\s?P\b")
_FRAME_RE = re.compile(r"\b(\d{1,4})\s?AF\b")
_KVA_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s?KVA\b")
_VOLTS_RE = re.compile(r"\b(\d{3}(?:\s?Y?\s?/\s?\d{3})?)\s?(?:V|VOLT|VOLTS|Y)\b")
_PHASE_RE = re.compile(r"\b([13])\s?-?\s?PH(?:ASE)?\b")
_NEMA_RE = re.compile(r"\bNEMA[\s-]?(\d+[A-Z]?)\b")
_AIC_RE = re.compile(r"\b(\d+(?:,\d{3})?)\s?K?\s?AIC\b|\b(\d+)\s?K\s?A?I?C?\s?RATING\b")
_PANEL_NAME_RE = re.compile(
    r"\b(?:PANEL(?:BOARD)?|SWITCHBOARD|SWBD|SW\.?BD\.?|DIST\.?\s?BD\.?|MCC)\s+'?(?!TYPE\b|SCHEDULE\b|BOARD\b|\d+A\b|\d{3}\b)"
    r"([A-Z][A-Z0-9/.&-]{0,11})'?(?=[,:\s]|$)"
)
_WIRE_RE = re.compile(r"#\s?(\d+/0|\d+)(?!\s*X)\b|(?<![#/\d])\b(\d+/0|\d{1,2})\s?AWG\b")
_KCMIL_RE = re.compile(r"\b(\d{3,4})\s?(?:KCMIL|MCM)\b")
_CORES_RE = re.compile(r"/\s?(\d+)\s?C\b|\b(\d+)\s?(?:COND|CONDUCTOR|CONDUCTORS)\b|\b(\d+)\s?PR\b|\b(\d+)[\s-]?STRAND\b")
_SIZE_RE = re.compile(r'(?<![#\w/-])(\d+(?:-\d+/\d+)?|\d+/\d+)"')
_CONDUIT_TYPE_RE = re.compile(
    r"\b(EMT|PVC|RGS|RMC|IMC|FMC|LFMC|LFNC|ENT|GRC|HDPE|RNC|ARC|SCH\s?\d+|SEAL-?TIGHT|SEALTITE|LIQUID-?TIGHT|FLEX)\b"
)
_CONDUIT_RE = re.compile(
    r'\d"\s?C\b|\bCONDUIT\b|\bFLEX\b|\bSEAL-?TIGHT\b|\bSEALTITE\b|\bLIQUID-?TIGHT\b|'
    r"\bSCH\s?\d+\b|\bEMT\b|\bPVC\b|\bRGS\b|\bRMC\b|\bIMC\b|\bFMC\b|\bLFMC\b|\bGRC\b|\bHDPE\b|\bSLEEVE\b"
)
_PULL_STRING_RE = re.compile(r"\bPULL ?(?:STRING|WIRE|ROPE|TAPE)\b")
_MATERIAL_RE = re.compile(r"\b(CU|COPPER|AL|ALUMINUM|ALUMINIUM)\b")
_INSULATION_RE = re.compile(r"\b(THHN|THWN|THW|XHHW|XHHW-2|USE-2|RHH|RHW|BARE|MTW|TFFN)\b")
_CONDUCTOR_ROLE_RE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GND", re.compile(r"\bGND\b|\bGROUND(?:ING)?\b|\bGREEN\b|\bGEC\b|\bBARE\b|\bEGC\b")),
    ("NEUTRAL", re.compile(r"\bNEUTRAL\b|\bNEU\b")),
    ("LUG", re.compile(r"\bLUGS?\b")),
)
_CONDUCTOR_WORDS_RE = re.compile(
    r"\bTHHN\b|\bTHWN\b|\bTHW\b|\bXHHW\b|\bUSE-2\b|\bRHH\b|\bRHW\b|\bBARE\b|\bWIRE\b|"
    r"\bCONDUCTORS?\b|\bGEC\b|\bAWG\b|\bKCMIL\b|\bMCM\b"
)
_CONTROL_CABLE_RE = re.compile(r"\bCONTROL CABLE\b|\bCONTROL\b.*\bCABLE\b|\bTHERMOSTAT (?:WIRE|CABLE)\b")
_CABLE_WORD_RE = re.compile(
    r"\bCABLE\b|\bCABLES\b|\bCORD\b|\bROMEX\b|\bNM-?B\b|\bMC\b|\bCAT\s?\d|\bRG\s?\d+\b|\bCOAX(?:IAL)?\b|"
    r"\bSOOW\b|\bSO CORD\b|\bTC-?ER\b|\bUF-?B\b|\bSTRANDED COPPER\b|\bTRAY CABLE\b|\bFIBER OPTIC\b"
)
_CABLE_SUB_RE = re.compile(
    r"\bCAT\s?\d+[AE]?\b|\bRG\s?\d+\b|\bMC\b|\bROMEX\b|\bNM-?B\b|\bFIBER\b|\bCOAX(?:IAL)?\b|\bCORD\b|\bSOOW\b|\bTC-?ER\b|\bUF-?B\b"
)
_BREAKER_RE = re.compile(r"\bBREAKERS?\b|\bBKR\b|\bCB\b|\bFUSES?\b|\bCURRENT LIMITER\b")
_PANEL_RE = re.compile(
    r"\bPANEL(?:BOARD)?S?\b|\bSWITCHBOARD\b|\bSWBD\b|\bSW\.?BD\b|\bSWITCHGEAR\b|\bMCC\b|\bLOAD CENTER\b|"
    r"\bDIST(?:RIBUTION)?\.?\s?B(?:OAR)?D\b|\bMDP\b|\bMSB\b|\bDISTRIBUTION BOARD\b"
)
_DISCONNECT_RE = re.compile(r"\bDISCONNECT(?:S|ING)?\b|\bSAFETY SWITCH(?:ES)?\b|\bNON[\s-]?FUSED SWITCH\b|\bFUSIBLE SWITCH\b|\bFUSED SWITCH\b")
_TRANSFORMER_RE = re.compile(r"\bTRANSFORMERS?\b|\bTRANSFOTMER\b|\bXFMR\b|\bXFRMR\b")
_SERVICE_RE = re.compile(
    r"\bMETER(?:ING)?\b|\bCT CABINET\b|\bSERVICE PEDESTAL\b|\bPEDESTAL\b|\bGENERATOR\b|\bGENSET\b|\bATS\b|"
    r"\bTRANSFER SWITCH\b|\bSURGE\b|\bTVSS\b|\bSPD\b|\bSHUNT TRIP\b|\bSTARTER\b|\bVFD\b|\bCONTACTOR\b|"
    r"\bCOMPANY SWITCH\b|\bPOWER MODULE\b|\bSERVICE ENTRANCE\b|\bELEVATOR\b|\bRELAYS?\b|\bEPO\b|\bE-?STOP\b|\bSHUTDOWN SWITCH\b"
)
_UPS_RE = re.compile(r"\bUPS\b|\bINVERTER\b|\bBATTERY\b.*\bAH\b|\bBATTERY BANK\b")
_EV_RE = re.compile(r"\bEVSE\b|\bEV CHARG\w*\b|\bCHARGING STATION\b|\bCHARGING OUTLET\b|\bEV =\b|\bEV\b.*\bCHARG")
_EQUIPMENT_RE = re.compile(
    r"\bCONN?ECTIONS?\b|\bCONNECT\b|\bHOOK-?UP\b|\bWHIP\b|\bPOWER TO\b|\bFEED TO\b|\bRECONNECT\b"
)
_LIGHTING_CONTROL_RE = re.compile(
    r"\bPHOTOCELL\b|\bPHOTO CELL\b|\bTIME ?CLOCK\b|\bTIME SWITCH\b|\bLIGHTING CONTROL\b|\bCONTROL PANEL\b|"
    r"\bRELAY PANEL\b|\bPOWER PACK\b|\bPOWER/RELAY PACK\b|\bSWITCH PACK\b|\bDAYLIGHT\b|\bKEYPAD\b|\bCONTROLLER\b|"
    r"\bDALI\b|\bROOM CONTROLLER\b|\bLIGHTING CONTACTOR\b|\bWAVELINX\b|\bNLIGHT\b|\bDLM\b|\bBRIDGE\b|\bGATEWAY\b|"
    r"\bEMERGENCY LIGHTING CONTROL\b|\bUL924\b|\bSWITCHBANK\b|\bSTATION\b"
)
_DIMMER_RE = re.compile(r"\bDIMMERS?\b|\bDIMMING SWITCH\b|\bWALL SWITCH DIMMING\b")
_OCC_RE = re.compile(r"\bSENSORS?\b|\bOCCUPANCY\b|\bVACANCY\b|\bMOTION\b")
_SWITCH_RE = re.compile(r"\bSWITCH(?:ES)?\b|\b[34]-WAY\b|\bTOGGLE\b")
_SWITCH_EXCLUDE_RE = re.compile(
    r"\bTRANSFER SWITCH\b|\bDOOR POSITION\b|\bPOSITION SWITCH\b|\bFLOW SWITCH\b|\bTAMPER SWITCH\b|"
    r"\bPRESSURE SWITCH\b|\bETHERNET\b|\bNETWORK SWITCH\b|\bLIMIT SWITCH\b|\bFLOAT SWITCH\b|\bLEVEL SWITCH\b"
)
_SWITCH_ATTR: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("3W", re.compile(r"\b3[\s-]?WAY\b|\bTHREE[\s-]?WAY\b")),
    ("4W", re.compile(r"\b4[\s-]?WAY\b|\bFOUR[\s-]?WAY\b")),
    ("KEY", re.compile(r"\bKEY(?:ED)?\b")),
    ("DP", re.compile(r"\bDOUBLE POLE\b|\bTWO POLE\b|\b2[\s-]?P\b|\bDP\b|\b3 POLE\b|\b3P\b")),
    ("PILOT", re.compile(r"\bPILOT\b")),
    ("TIMER", re.compile(r"\bTIMER\b")),
    ("LOCK", re.compile(r"\bLOCK(?:ABLE|ING)?\b")),
    ("WP", re.compile(r"\bWP\b|\bWEATHER ?PROOF\b|\bNEMA 3R\b")),
    ("MOTOR", re.compile(r"\bMOTOR RATED\b|\bMOTOR\b")),
    ("EPO", re.compile(r"\bEPO\b|\bE-?STOP\b|\bEMERGENCY (?:SHUTDOWN|OVERR?IDE|STOP)\b|\bBYPASS\b")),
    ("LV", re.compile(r"\bLOW VOLTAGE\b|\bLV\b|\b0-10V\b")),
    ("OCC", re.compile(r"\bOCCUPANCY\b|\bVACANCY\b|\bSENSOR\b|\bMOTION\b")),
)
_FIRE_ALARM_RE = re.compile(
    r"\bFIRE ALARM\b|\bFA\b|\bSMOKE DETECTOR\b|\bHEAT DETECTOR\b|\bHORN\b|\bSTROBE\b|\bPULL STATION\b|"
    r"\bFACP\b|\bANNUNCIATOR\b|\bDUCT DETECTOR\b|\bNAC\b|\bFIRE ALARM ROUGH"
)
_LV_DEVICE_RE = re.compile(
    r"\bDATA\b|\bTELE(?:PHONE|COM)?\b|\bVOICE\b|\bTV\b|\bCATV\b|\bCAMERAS?\b|\bCCTV\b|\bCARD READER\b|"
    r"\bACCESS CONTROL\b|\bDOOR (?:CONTACTS?|CONTECT|POSITION|STRIKE|LOCK|ACCESS)\b|\bELECTRIC STRIKE\b|"
    r"\bWIRELESS ACCESS POINT\b|\bWAP\b|\bSPEAKERS?\b|\bINTERCOM\b|\bPAGING\b|\bAUDIO\b|\bVIDEO\b|"
    r"\bSECURITY\b|\bNURSE CALL\b|\bPANIC BUTTON\b|\bPUSH BUTTON\b|\bCHIME\b|\bDOORBELL\b|\bRACK\b|\bSERVERS?\b|"
    r"\bNETWORK\b|\bETHERNET\b|\bBACKBOARD\b|\bPATCH PANEL\b|\bPATCH MODULES?\b|\bCHASSIS\b|\bAV\b|\bDISPLAY\b|"
    r"\bSIGNAGE\b|\bTOUCH PANEL\b|\bAMPLIFIER\b|\bMICROPHONE\b|\bCLOCK\b|\bMAGLOCK\b|\bREX\b|\bDETECTOR LOOPS?\b|"
    r"\bDETECTION LOOPS?\b|\bTRAFFIC SIGN\w*\b|\bSIGNAL HEAD\b|\bRSU\b|\bPREEMPTION\b|\bCOMPUTER\b|\bMOUSE\b|"
    r"\bCABINET\b|\bCABLE MANAGER\b|\bLIU\b|\bFIBER\b|\bSPLICE\b|\bSNO-SHOE\b|\bGLAND\b|\bMONITOR\b|\bPROJECTOR\b|"
    r"\bSOUND\b|\bPA SYSTEM\b|\bANTENNA\b|\bDAS\b|\bBDA\b"
)
_GROUNDING_RE = re.compile(
    r"\bGROUND ROD\b|\bGROUNDING ROD\b|\bGROUND BAR\b|\bGROUNDING BAR\b|\bBUSBAR\b|\bBUS BAR\b|"
    r"\bEXOTHERMIC\b|\bCADWELD\b|\bUFER\b|\bGROUND(?:ING)? (?:ELECTRODE|CLAMP|PLATE|RING|LOOP|GRID|WELL|BUSHING|KIT)\b|"
    r"\bLIGHTNING\b|\bGROUND(?:ING)?\b"
)
_TRENCH_RE = re.compile(
    r"\bTRENCH(?:ING|ES)?\b|\bEXCAVAT(?:E|ION|ING)\b|\bBACKFILL\b|\bBOR(?:E|ING)\b|\bDIRECTIONAL\b|"
    r"\bDUCT ?BANK\b|\bDETECTABLE\b|\bMULE ?TAPE\b|\bMULTETAPE\b|\bWARNING TAPE\b|\bSAND\b|\bSLURRY\b|\bCDCA\b"
)
_SAWCUT_RE = re.compile(r"\bSAW[\s-]?CUT(?:TING)?\b|\bCORE DRILL\b|\bASPHALT\b|\bPATCH(?:ING)?\b")
_CONCRETE_RE = re.compile(
    r"\bCONCRETE\b|\bPAD\b|\bFOUNDATION\b|\bVAULT\b|\bMANHOLE\b|\bHANDHOLE\b|\bBOLLARD\b|\bFOOTING\b"
)
_POLE_RE = re.compile(r"\bPOLES?\b|\bMAST\b|\bSTREETLIGHT\b|\bSTREET LIGHT\b|\bSIGN POST\b|\bPOST\b|\bTENON\b")
_POLE_LIGHT_RE = re.compile(r"\bHEAD\b|\bAREA LIGHT\b|\bPOLE MOUNTED\b|\bSTREETLIGHT\b|\bPEDESTRIAN\b|\bLUMINAIRE\b|\bLIGHT\b")
_BOX_RE = re.compile(
    r"\bJ-?BOX(?:ES)?\b|\bJUNCTION BOX(?:ES)?\b|\bPULL ?BOX(?:ES)?\b|\bBACK ?BOX(?:ES)?\b|\bFLOOR BOX(?:ES)?\b|"
    r"\bPOKE[\s-]?THRU\b|\bGUTTER\b|\bWIREWAY\b|\bBOX(?:ES)?\b|\bFACEPLATE\b|\bCOVER PLATE\b|\bBLANK PLATE\b|\bPLATE\b"
)
_RACEWAY_RE = re.compile(r"\bWIREMOLD\b|\bRACEWAY\b|\bCABLE TRAY\b|\bLADDER TRAY\b|\bTRAY\b|\bPLUGMOLD\b|\bLADDER RACK\b|\bINNERDUCT\b")
_THERMOSTAT_RE = re.compile(r"\bTHERMOSTATS?\b|\bT-?STATS?\b")
_LUMINAIRE_RE = re.compile(
    r"\bFIXTURES?\b|\bLUMINAIRES?\b|\bLIGHTS?\b|\bLIGHTING\b|\bLED\b|\bLAMPS?\b|\bDOWNLIGHT\b|"
    r"\bHIGH ?BAY\b|\bTROFFER\b|\bSCONCE\b|\bPENDANT\b|\bEXIT\b|\bWALL ?PACK\b|\bFLOOD\b|\bCANOPY\b|"
    r"\bBATTERY PACK\b|\bEM PACK\b|\bTRACK\b|\bSTRIP\b|\bCOVE\b|\bUNDERCABINET\b|\bUNDER CABINET\b"
)
_TRACK_HEAD_RE = re.compile(r"\bHEADS?\b")
_DEMO_RE = re.compile(
    r"^(?:REMOVE|REMOVAL|DEMO|DEMOLISH|DEMOLITION|DISCONNECT AND REMOVE|DISCONNECT & REMOVE|REMOVING)\b"
)
_DEMO_LIGHT_RE = re.compile(
    r"\bLIGHT(?:ING|S)?\b|\bFIXTURES?\b|\bLUMINAIRES?\b|\bDOWNLIGHTS?\b|\bTROFFERS?\b|\bLAMPS?\b|\bEXIT\b|"
    r"\bTRACK\b|\bDIMMERS?\b|\bOCCUPANCY\b|\bLIGHTING CONTROLS?\b"
)
_DEMO_POWER_RE = re.compile(
    r"\bRECEPT\w*\b|\bOUTLETS?\b|\bPOWER DEVICES?\b|\bDEVICES?\b|\bFLOOR BOX(?:ES)?\b|\bDUPLEX\b|\bQUAD\w*\b|"
    r"\bPOWER\b|\bJ-?BOX(?:ES)?\b|\bJUNCTION BOX(?:ES)?\b|\bDISCONNECTS?\b|\bPANELS?\b|\bFEEDERS?\b|\bEQUIPMENT\b|"
    r"\bCIRCUITS?\b|\bCONDUITS?\b|\bWIRING\b"
)
_DEMO_SWITCH_RE = re.compile(r"\bSWITCH(?:ES)?\b")
_ALLOWANCE_RE = re.compile(r"\bALLOWANCE\b|\bLUMP SUM\b")
_CAVEAT_RE = re.compile(
    r"\((?:NEED TO CONFIRM|NEED CONFIRMATION|ASSUMED|VERIFY|CONFIRM|TBD|NOT SHOWN|BY OTHERS|PLEASE CONFIRM|"
    r"OR EQUAL|OR EQUIVALENT|NOTE \d+)[^)]*\)"
)
# A leading tag: letters only up to 3 chars needs a strict separator; a tag
# with a digit may be followed by a space (L1 2X4 ...). TYPE A: ... is a tag.
_TAG_STRICT_RE = re.compile(
    r"^(?:TYPE\s+)?\$?([A-Z]{1,3}\d{1,3}(?:\.\d)?[A-Z]{0,2}|\d{1,3}[A-Z]{0,2}|[A-Z]{1,3}-\d{1,2}[A-Z]?|[A-Z]{1,3}\d{0,2}-[A-Z]{1,2})"
    r"(?:-(EM|NL|E|N|STAIR))?\s*(?:,|:|\s-\s|\s(?=[A-Z0-9(\"']))"
)
_TAG_LETTERS_RE = re.compile(r"^(?:TYPE\s+)?\$?([A-Z]{1,3}|[A-Z]{1,3}/[A-Z]{1,3})(?:-(EM|NL|E|N|STAIR))?\s*(?:,|:|\s-\s)")
_SLUG_RE = re.compile(r"[^A-Z0-9]+")


def _slug(text: str, limit: int = 60) -> str:
    return _SLUG_RE.sub("_", text).strip("_")[:limit].rstrip("_").lower()


# Data shapes ------------------------------------------------------------------


@dataclass(frozen=True)
class LineKey:
    """Identity of a BOQ line for matching: a kind plus the attributes that
    make a different priced line. attrs are plain strings (`L1-EM`, `DUPLEX`,
    `GFCI`, `#12`, `3/4"`, `EMT`, `20A/1P`, ...)."""

    kind: str
    attrs: frozenset[str]

    @property
    def modifiers(self) -> frozenset[str]:
        return frozenset(a for a in self.attrs if a in _PRIORITY_INDEX)

    @property
    def base(self) -> frozenset[str]:
        return self.attrs - self.modifiers

    def as_text(self) -> str:
        return f"{self.kind}[{', '.join(sorted(self.attrs))}]"


Aliases = Mapping[str, str]


def merge_aliases(case_aliases: Mapping[str, str] | None = None) -> dict[str, str]:
    """Global aliases plus a case's additions (upper-cased keys), the case winning."""
    out = dict(GLOBAL_ALIASES)
    for word, variant in (case_aliases or {}).items():
        v = str(variant).upper()
        if v not in VARIANTS:
            raise ValueError(f"alias {word!r} maps to unknown variant {variant!r}")
        out[str(word).upper().strip()] = v
    return out


def aliases_sha256(aliases: Mapping[str, str]) -> str:
    payload = json.dumps(dict(sorted(aliases.items())), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# Classification -----------------------------------------------------------------


def _amps_poles(norm: str) -> set[str]:
    """`20A/1P`, or separate `30A` and `3P` tokens, plus the frame `15AF`."""
    attrs: set[str] = set()
    m = _AP_RE.search(norm)
    if m:
        attrs.add(f"{m.group(1)}A/{m.group(3)}P")
        if m.group(2):
            attrs.add(f"{m.group(2)}AF")
    else:
        amps = _AMPS_RE.search(norm)
        poles = _POLES_RE.search(norm)
        if amps:
            attrs.add(f"{amps.group(1) or amps.group(2) or amps.group(3)}A")
        if poles:
            attrs.add(f"{poles.group(1)}P")
    frame = _FRAME_RE.search(norm)
    if frame and not any(a.endswith("AF") for a in attrs):
        attrs.add(f"{frame.group(1)}AF")
    return attrs


def _first_size(norm: str) -> str | None:
    m = _SIZE_RE.search(norm)
    return f'{m.group(1)}"' if m else None


def _wire_size(norm: str) -> str | None:
    k = _KCMIL_RE.search(norm)
    if k:
        return f"{k.group(1)}KCMIL"
    m = _WIRE_RE.search(norm)
    if m:
        return f"#{m.group(1) or m.group(2)}"
    return None


def _section_class(section: str, group: str, subgroup: str) -> str | None:
    """A coarse class from the BOQ position for sized items (conduit, wire,
    cable) so a low voltage EMT run and a branch EMT run stay distinct."""
    g = _canonical(group or "")
    s = _canonical(subgroup or "")
    if "LOW VOLTAGE" in g:
        return "LV"
    if "POLE" in g or "SOLAR" in g or "NV ENERGY" in g:
        return "SITE"
    if "FEEDER" in s or "SERVICE" in s:
        return "FEEDER"
    if "TRENCH" in s or "UNDERGROUND" in s:
        return "UG"
    if "GROUND" in s:
        return "GND"
    if "RACK" in s:
        return "RACK"
    return None


def _variant(text_after_tag: str, aliases: Aliases) -> str | None:
    """EM or NL when a word of the alias set appears in the text after the tag."""
    for word in sorted(aliases, key=len, reverse=True):
        if re.search(rf"(?<![A-Z0-9]){re.escape(word)}(?![A-Z0-9])", text_after_tag):
            return aliases[word]
    return None


def _leading_tag(norm: str) -> tuple[str, str | None, str] | None:
    """(tag, suffix, rest) when the line starts with a schedule tag."""
    m = _TAG_STRICT_RE.match(norm) or _TAG_LETTERS_RE.match(norm)
    if not m:
        return None
    return m.group(1), m.group(2), norm[m.end() :]


def _fixture_key(norm: str, aliases: Aliases) -> LineKey | None:
    lead = _leading_tag(norm)
    if lead is None:
        return None
    tag, suffix, rest = lead
    variant: str | None = None
    if suffix in ("EM", "E"):
        variant = "EM"
    elif suffix in ("NL", "N"):
        variant = "NL"
    elif suffix == "STAIR":
        tag = f"{tag}-STAIR"
    if variant is None:
        variant = _variant(rest, aliases)
    return LineKey("fixture", frozenset({f"{tag}-{variant}" if variant else tag}))


def _tag_attr(norm: str) -> set[str]:
    """`TAG:<tag>` for control devices that start with a schedule tag."""
    lead = _leading_tag(norm)
    if lead is None:
        return set()
    tag, suffix, _rest = lead
    return {f"TAG:{tag}{'-' + suffix if suffix else ''}"}


def _receptacle_key(norm: str) -> LineKey:
    attrs: set[str] = set()
    if re.search(r"\bQUAD(?:RU)?PLEX\b|\bFOURPLEX\b|\bQUAD\b|\bDOUBLE DUPLEX\b", norm):
        attrs.add("QUAD")
    elif re.search(r"\bTRIPLEX\b", norm):
        attrs.add("TRIPLEX")
    elif re.search(r"\bSINGLE\b|\bSIMPLEX\b", norm):
        attrs.add("SINGLE")
    elif _NEMA_CONFIG_RE.search(norm) or re.search(r"\b(?:30|50|60|100)\s?A\b", norm) or re.search(r"\bPIN AND SLEEVE\b|\bTWIST[\s-]?LOCK\b|\bSPECIAL PURPOSE\b", norm):
        attrs.add("SPECIAL")
        cfg = _NEMA_CONFIG_RE.search(norm)
        if cfg:
            attrs.add(cfg.group(1))
        amps = _AMPS_RE.search(norm)
        if amps and (amps.group(1) or "") not in ("20", "15", ""):
            attrs.add(f"{amps.group(1)}A")
    else:
        attrs.add("DUPLEX")
    for name, pat in _RECEPTACLE_MODIFIERS:
        if pat.search(norm):
            attrs.add(name)
    if "HALF_SWITCHED" in attrs:
        attrs.discard("SWITCHED")
    return LineKey("receptacle", frozenset(attrs))


def _demo_key(norm: str) -> LineKey:
    body = _DEMO_RE.sub("", norm).strip()
    body = re.sub(r"^(?:OF\s+)?(?:THE\s+|ALL\s+)?(?:EXISTING\s+|\(E\)\s*)?", "", body)
    if re.search(r"\bTRACK\b", body) and not _TRACK_HEAD_RE.search(body):
        return LineKey("demo_track", frozenset())
    light = _DEMO_LIGHT_RE.search(body)
    power = _DEMO_POWER_RE.search(body)
    if light and (power is None or light.start() <= power.start()):
        return LineKey("demo_lighting", frozenset())
    if power:
        return LineKey("demo_power", frozenset())
    if _DEMO_SWITCH_RE.search(body):
        return LineKey("demo_lighting", frozenset())
    return LineKey("demo_other", frozenset({_slug(body, 30) or "blank"}))


def _conductor_key(norm: str, sec: str | None, in_lv: bool) -> LineKey:
    attrs: set[str] = set()
    size = _wire_size(norm)
    if size:
        attrs.add(size)
    mat = _MATERIAL_RE.search(norm)
    if mat:
        attrs.add({"COPPER": "CU", "ALUMINUM": "AL", "ALUMINIUM": "AL"}.get(mat.group(1), mat.group(1)))
    ins = sorted({i.group(1) for i in _INSULATION_RE.finditer(norm)})
    if ins:
        attrs.add("/".join(ins))
    for role, pat in _CONDUCTOR_ROLE_RE:
        if pat.search(norm):
            attrs.add(role)
            break
    if sec:
        attrs.add(f"SEC:{sec}")
    elif in_lv:
        attrs.add("SEC:LV")
    if not size:
        attrs.add(_slug(norm, 40))
    return LineKey("conductor", frozenset(attrs))


def _conduit_key(norm: str, sec: str | None) -> LineKey:
    attrs: set[str] = set()
    size = _first_size(norm)
    if size:
        attrs.add(size)
    types = sorted({t.group(1).replace(" ", "") for t in _CONDUIT_TYPE_RE.finditer(norm)})
    if types:
        attrs.add(types[0])
    if re.search(r"\bELBOW\b|\bSWEEP\b|\bBEND\b", norm):
        attrs.add("ELBOW")
    if re.search(r"\bCAP\b|\bPLUG\b", norm):
        attrs.add("CAP")
    if re.search(r"\bWRAP(?:PED)?\b|\bCOATED\b", norm):
        attrs.add("WRAPPED")
    if _PULL_STRING_RE.search(norm):
        attrs.add("PULL")
    if re.search(r"\bMULE ?TAPE\b|\bMULTETAPE\b|\bDETECTABLE\b", norm):
        attrs.add("TAPE")
    if re.search(r"\bSLEEVE\b", norm):
        attrs.add("SLEEVE")
    if sec:
        attrs.add(f"SEC:{sec}")
    if not size and not types:
        attrs.add(_slug(norm, 40))
    return LineKey("conduit", frozenset(attrs))


def _cable_key(norm: str, sec: str | None, kind: str = "cable") -> LineKey:
    attrs: set[str] = set()
    size = _wire_size(norm)
    if size:
        attrs.add(size)
    cores = _CORES_RE.search(norm)
    if cores:
        attrs.add(f"{cores.group(1) or cores.group(2) or cores.group(3) or cores.group(4)}C")
    sub = _CABLE_SUB_RE.search(norm)
    if sub:
        attrs.add(sub.group(0).replace(" ", "").replace("-", ""))
    if re.search(r"\bPLENUM\b", norm):
        attrs.add("PLENUM")
    if re.search(r"\bSHIELDED\b|\bSTP\b", norm):
        attrs.add("SHIELDED")
    if sec:
        attrs.add(f"SEC:{sec}")
    if kind == "cable" and not (cores or sub):
        attrs.add(_slug(norm, 40))
    return LineKey(kind, frozenset(attrs))


def _gear_attrs(norm: str) -> set[str]:
    attrs = _amps_poles(norm)
    v = _VOLTS_RE.search(norm)
    if v:
        attrs.add(f"{v.group(1).replace(' ', '')}V")
    ph = _PHASE_RE.search(norm)
    if ph:
        attrs.add(f"{ph.group(1)}PH")
    nema = _NEMA_RE.search(norm)
    if nema:
        attrs.add(f"NEMA{nema.group(1)}")
    aic = _AIC_RE.search(norm)
    if aic:
        attrs.add(f"{(aic.group(1) or aic.group(2)).replace(',', '')}AIC")
    for name in sorted({m.group(1) for m in _PANEL_NAME_RE.finditer(norm)}):
        attrs.add(f"PANEL:{name}")
    if re.search(r"\bMLO\b", norm):
        attrs.add("MLO")
    if re.search(r"\bMCB\b|\bMAIN BREAKER\b", norm):
        attrs.add("MCB")
    return attrs


def _other_key(norm: str) -> LineKey:
    return LineKey("other", frozenset({_slug(norm, 120) or "blank"}))


def _with_slug(kind: str, attrs: set[str], norm: str, limit: int = 40) -> LineKey:
    if not attrs and kind in _SLUG_FALLBACK_KINDS:
        attrs = {_slug(norm, limit) or "blank"}
    return LineKey(kind, frozenset(attrs))


def classify_line(
    description: Any,
    section: str = "",
    group: str = "",
    unit: str = "",
    *,
    subgroup: str = "",
    aliases: Aliases | None = None,
) -> LineKey:
    """The LineKey of one BOQ line (either side). Deterministic text rules in
    a fixed order over the normalized description plus the BOQ position:
    demolition first, then fixtures by tag, receptacles, controls, gear,
    wire and conduit, low voltage, site work, and `other` (the full text,
    so only near-identical text matches). Caveats and style words the
    estimator does not price are dropped before the rules run."""
    aliases = aliases if aliases is not None else GLOBAL_ALIASES
    raw = normalize_description(description)
    norm = _CAVEAT_RE.sub(" ", raw)
    norm = re.sub(r"\s+", " ", norm).strip()
    if not norm:
        return _other_key("")
    sec_canon = _canonical(section or "")
    grp = _canonical(group or "")
    sec = _section_class(section, group, subgroup)
    in_lv = "LOW VOLTAGE" in grp or "FIRE ALARM" in _canonical(subgroup or "")

    if sec_canon == "DEMOLITION" or _DEMO_RE.match(norm):
        if sec_canon == "DEMOLITION" or not _ALLOWANCE_RE.search(norm):
            return _demo_key(norm)
    if _ALLOWANCE_RE.search(norm) or grp == "ALLOWANCES":
        return LineKey("allowance", frozenset({_slug(norm, 40) or "blank"}))

    fixture_group = "LIGHTING FIXTURE" in grp or "POLE" in grp
    if fixture_group or (_LUMINAIRE_RE.search(norm) and not in_lv and not _RECEPTACLE_STRONG_RE.search(norm)):
        if _POLE_RE.search(norm) and _POLE_LIGHT_RE.search(norm) and not re.search(r"\bTRACK\b", norm):
            fk = _fixture_key(norm, aliases)
            return LineKey("pole_light", fk.attrs if fk else frozenset({_slug(norm, 40)}))
        if re.search(r"\bTRACK\b", norm) and _TRACK_HEAD_RE.search(norm) and not _leading_tag(norm):
            return LineKey("fixture", frozenset({"TRACK-HEAD"}))
        if re.search(r"^(?:LIGHTING )?TRACK\b|\bTRACK ROD\b|\bLIGHTING TRACK\b|\bTRACK LIGHTING\b", norm) and not _leading_tag(norm):
            return LineKey("fixture", frozenset({"TRACK"}))
        fk = _fixture_key(norm, aliases)
        if fk is not None and not _RECEPTACLE_STRONG_RE.search(norm):
            return fk
        if fixture_group and not _RECEPTACLE_STRONG_RE.search(norm):
            if re.search(r"\bEXIT\b", norm):
                return LineKey("fixture", frozenset({"EXIT"}))
            return LineKey("fixture", frozenset({_slug(norm, 40) or "blank"}))
        if not fixture_group and not _RECEPTACLE_STRONG_RE.search(norm) and not _SWITCH_RE.search(norm):
            if not (_CONDUIT_RE.search(norm) or _CABLE_WORD_RE.search(norm) or _CONDUCTOR_WORDS_RE.search(norm) or _BOX_RE.search(norm) or _PANEL_RE.search(norm) or _OCC_RE.search(norm) or _LIGHTING_CONTROL_RE.search(norm) or _POLE_RE.search(norm) or _LV_DEVICE_RE.search(norm)):
                if re.search(r"\bEXIT\b", norm):
                    return LineKey("fixture", frozenset({"EXIT"}))
                return LineKey("fixture", frozenset({_slug(norm, 40) or "blank"}))

    if _THERMOSTAT_RE.search(norm) and not _CABLE_WORD_RE.search(norm):
        return LineKey("thermostat", frozenset())
    if _CONTROL_CABLE_RE.search(norm) or (re.search(r"\bCONTROL\b", norm) and _CABLE_WORD_RE.search(norm) and _wire_size(norm)):
        return _cable_key(norm, sec, "control_cable")
    if _EV_RE.search(norm):
        return _with_slug("ev_charger", set(), norm)
    if _UPS_RE.search(norm):
        return _with_slug("ups", set(), norm)
    if _FIRE_ALARM_RE.search(norm) and not _RECEPTACLE_STRONG_RE.search(norm):
        return LineKey("fire_alarm", frozenset({_slug(norm, 40) or "blank"}))
    if _LV_DEVICE_RE.search(norm) and not _RECEPTACLE_STRONG_RE.search(norm) and not _CONDUIT_RE.search(norm) and not _CABLE_WORD_RE.search(norm):
        if not (re.search(r"\bOUTLET\b", norm) and not _LV_OUTLET_RE.search(norm)):
            return LineKey("low_voltage", frozenset({_slug(norm, 40) or "blank"}))

    if _LIGHTING_CONTROL_RE.search(norm) and not _RECEPTACLE_STRONG_RE.search(norm) and not _DIMMER_RE.search(norm):
        return LineKey("lighting_control_other", frozenset({_slug(norm, 40) or "blank"} | _tag_attr(norm)))

    recept = _RECEPTACLE_RE.search(norm)
    if recept and not (re.search(r"\bOUTLET\b", norm) and _LV_OUTLET_RE.search(norm)):
        strong = _RECEPTACLE_STRONG_RE.search(norm)
        if in_lv and not re.search(r"\bRECEPT\w*\b", norm):
            pass
        elif re.search(r"\bOUTLET\b", norm) and not strong and not re.search(r"\bPOWER\b|\bCONVENIENCE\b|\bGFC?I\b", norm):
            pass
        else:
            sw = _SWITCH_RE.search(norm)
            if sw and strong and sw.start() < strong.start() and not re.search(r"\bSWITCHED\b", norm):
                pass
            elif _BREAKER_RE.search(norm) and not strong:
                pass
            else:
                return _receptacle_key(norm)

    if _DIMMER_RE.search(norm):
        attrs = {name for name, pat in _SWITCH_ATTR if pat.search(norm)}
        return LineKey("dimmer", frozenset(attrs | _tag_attr(norm)))
    if _OCC_RE.search(norm):
        attrs = set()
        if re.search(r"\bCEILING\b", norm):
            attrs.add("CEILING")
        elif _SWITCH_RE.search(norm) or re.search(r"\bWALL\b", norm):
            attrs.add("WALL")
        else:
            attrs.add("CEILING")
        if re.search(r"\bVACANCY\b", norm) and not re.search(r"\bOCCUPANCY\b", norm):
            attrs.add("VAC")
        if re.search(r"\bDUAL TECH\w*\b|\bPDT\b|\bPIR/ULTRASONIC\b", norm):
            attrs.add("DT")
        if re.search(r"\bLARGE\b|\bHIGH BAY\b", norm):
            attrs.add("LARGE")
        for name in ("3W", "4W", "LV"):
            if dict(_SWITCH_ATTR)[name].search(norm):
                attrs.add(name)
        return LineKey("occ_sensor", frozenset(attrs | _tag_attr(norm)))
    if _SWITCH_RE.search(norm) and not _DISCONNECT_RE.search(norm) and not _PANEL_RE.search(norm) and not _SERVICE_RE.search(norm):
        if not _SWITCH_EXCLUDE_RE.search(norm):
            attrs = {name for name, pat in _SWITCH_ATTR if pat.search(norm) and name != "OCC"}
            attrs |= {a for a in _amps_poles(norm) if "/" in a}
            if re.search(r"\bSWITCHBANK\b|\bSWITCH BANK\b", norm):
                attrs.add("BANK")
            return LineKey("switch", frozenset(attrs | _tag_attr(norm)))
    if grp == "LIGHTING CONTROLS" and not (_CONDUIT_RE.search(norm) or _CABLE_WORD_RE.search(norm) or _CONDUCTOR_WORDS_RE.search(norm) or _BOX_RE.search(norm)):
        return LineKey("lighting_control_other", frozenset({_slug(norm, 40) or "blank"}))

    if _TRANSFORMER_RE.search(norm) and not _PANEL_RE.search(norm):
        attrs = set()
        kva = _KVA_RE.search(norm)
        if kva:
            attrs.add(f"{kva.group(1)}KVA")
        v = _VOLTS_RE.search(norm)
        if v:
            attrs.add(f"{v.group(1).replace(' ', '')}V")
        return _with_slug("transformer", attrs, norm)
    if _DISCONNECT_RE.search(norm) and not _CABLE_WORD_RE.search(norm):
        attrs = _amps_poles(norm)
        if re.search(r"\bNON[\s-]?FUSED\b|\bNF\b|\bUNFUSED\b", norm):
            attrs.add("NF")
        elif re.search(r"\bFUSED\b|\bFUSIBLE\b", norm):
            attrs.add("FUSED")
        nema = _NEMA_RE.search(norm)
        if nema:
            attrs.add(f"NEMA{nema.group(1)}")
        v = _VOLTS_RE.search(norm)
        if v:
            attrs.add(f"{v.group(1).replace(' ', '')}V")
        return _with_slug("disconnect", attrs, norm)
    if _BREAKER_RE.search(norm) and not _PANEL_RE.search(norm):
        attrs = _amps_poles(norm)
        if re.search(r"\bGFC?I\b|\bGROUND FAULT\b|\bGF\b", norm):
            attrs.add("GFCI")
        if re.search(r"\bAFCI\b|\bARC FAULT\b", norm):
            attrs.add("AFCI")
        if re.search(r"\bSHUNT\b", norm):
            attrs.add("SHUNT")
        if re.search(r"\bFUSES?\b", norm) and not re.search(r"\bBREAKER", norm):
            attrs.add("FUSE")
        if re.search(r"\bFUSED\b", norm):
            attrs.add("FUSED")
        if re.search(r"\bAER\b|\bELECTRONIC TRIP\b|\bLSIG?\b", norm):
            attrs.add("ETRIP")
        for name in sorted({m.group(1) for m in _PANEL_NAME_RE.finditer(norm)}):
            attrs.add(f"PANEL:{name}")
        if re.search(r"\b100\s?%\b", norm):
            attrs.add("RATED")
        return _with_slug("breaker", attrs, norm)
    if _PANEL_RE.search(norm) and not _CABLE_WORD_RE.search(norm) and not re.search(r"\bJ-?BOX\b|\bJUNCTION BOX\b|\bPULL BOX\b", norm):
        return _with_slug("panelboard", _gear_attrs(norm), norm)
    if _SERVICE_RE.search(norm) and not _CABLE_WORD_RE.search(norm) and not _CONDUIT_RE.search(norm):
        attrs = _gear_attrs(norm)
        attrs.add(_slug(norm, 40))
        return LineKey("service_equipment", frozenset(attrs))

    if _GROUNDING_RE.search(norm) and not (_wire_size(norm) and _CONDUCTOR_WORDS_RE.search(norm)) and not _CONDUIT_RE.search(norm):
        attrs = set()
        size = _first_size(norm)
        if size:
            attrs.add(size)
        m = re.search(r"\b(\d+)'", norm)
        if m:
            attrs.add(f"{m.group(1)}FT")
        if re.search(r"\bROD\b", norm):
            attrs.add("ROD")
        elif re.search(r"\bEXOTHERMIC\b|\bCADWELD\b", norm):
            attrs.add("WELD")
        elif re.search(r"\bBAR\b|\bBUSBAR\b|\bBUS BAR\b", norm):
            attrs.add("BAR")
        else:
            attrs.add(_slug(norm, 40))
        return LineKey("grounding", frozenset(attrs))

    has_size = _wire_size(norm) is not None
    if _CONDUIT_RE.search(norm) and not _CABLE_WORD_RE.search(norm):
        return _conduit_key(norm, sec)
    if _CABLE_WORD_RE.search(norm) and not _RACEWAY_RE.search(norm):
        if has_size and _CONDUCTOR_WORDS_RE.search(norm) and not re.search(r"\bCABLE\b", norm):
            return _conductor_key(norm, sec, in_lv)
        return _cable_key(norm, sec)
    if has_size and not _BOX_RE.search(norm):
        return _conductor_key(norm, sec, in_lv)
    if _CONDUCTOR_WORDS_RE.search(norm) and not _BOX_RE.search(norm):
        return _conductor_key(norm, sec, in_lv)
    if _RACEWAY_RE.search(norm):
        attrs = set()
        size = _first_size(norm)
        if size:
            attrs.add(size)
        attrs.add(_slug(norm, 30))
        return LineKey("raceway", frozenset(attrs))

    if _EQUIPMENT_RE.search(norm) and not _BOX_RE.search(norm):
        return LineKey("equipment_connection", frozenset())
    if _TRENCH_RE.search(norm) and not _CONCRETE_RE.search(norm):
        attrs = set()
        m = re.search(r"(\d+)\s?\"?\s?(?:X|BY)\s?(\d+)", norm)
        if m:
            attrs.add(f"{m.group(1)}X{m.group(2)}")
        attrs.add(_slug(norm, 30))
        return LineKey("trench", frozenset(attrs))
    if _SAWCUT_RE.search(norm):
        return LineKey("sawcut", frozenset({_slug(norm, 30)}))
    if _CONCRETE_RE.search(norm):
        return LineKey("concrete", frozenset({_slug(norm, 40)}))
    if _BOX_RE.search(norm):
        attrs = set()
        size = _first_size(norm)
        if size:
            attrs.add(size)
        m = re.search(r"\b(\d+)\s?X\s?(\d+)(?:\s?X\s?(\d+))?\b", norm)
        if m:
            attrs.add("X".join(g for g in m.groups() if g))
        for word in ("JUNCTION", "PULL", "BACK", "FLOOR", "WP", "FACEPLATE", "GUTTER", "CEILING", "WALL"):
            if re.search(rf"\b{word}\b", norm):
                attrs.add(word)
        m2 = re.search(r"\bNO\.?\s?(\d+(?:-\d+/\d+)?)\b", norm)
        if m2:
            attrs.add(f"NO{m2.group(1)}")
        return LineKey("box", frozenset(attrs or {_slug(norm, 30)}))
    if in_lv:
        return LineKey("low_voltage", frozenset({_slug(norm, 40) or "blank"}))
    if _POLE_RE.search(norm):
        return LineKey("pole", frozenset({_slug(norm, 40)}))
    return _other_key(norm)


# Truth and AI line views ---------------------------------------------------------


@dataclass
class TruthLine:
    """One estimator (reference) line as the scorer sees it."""

    line_id: str
    area: str
    section: str
    group: str
    subgroup: str
    description: str
    unit: str
    qty: float
    value: float | None = None  # v of 3.6: qty x (unit material + unit hours x rate)
    unit_item_cost: float | None = None
    is_accessory: bool = False
    key: LineKey | None = None
    merged_from: list[str] = field(default_factory=list)

    @property
    def is_length(self) -> bool:
        return normalize_unit(self.unit) == "FT"


def truth_lines_from_reference(reference: Mapping[str, Any], *, adjudicated: bool = True) -> list[TruthLine]:
    """Truth lines of a reference.json (eval_reference), adjudicated when the
    file carries `quantity_lines_adjudicated` and `adjudicated` is true."""
    lines = None
    if adjudicated:
        lines = reference.get("quantity_lines_adjudicated")
    if lines is None:
        lines = reference.get("quantity_lines") or []
    out: list[TruthLine] = []
    for i, ln in enumerate(lines):
        qty = float(ln.get("qty") or 0.0)
        uic = ln.get("unit_item_cost")
        value = ln.get("value")
        if value is None and uic is not None:
            value = qty * float(uic)
        out.append(
            TruthLine(
                line_id=str(ln.get("line_id") or f"Q{i + 1:03d}"),
                area=str(ln.get("area") or ""),
                section=str(ln.get("section") or ""),
                group=str(ln.get("group") or ""),
                subgroup=str(ln.get("subgroup") or ""),
                description=str(ln.get("description") or ""),
                unit=normalize_unit(ln.get("unit")),
                qty=qty,
                value=None if value is None else float(value),
                unit_item_cost=None if uic is None else float(uic),
                is_accessory=bool(ln.get("is_accessory")),
            )
        )
    return out


def truth_lines_from_rows(rows: Iterable[BoqRow], unit_costs: Mapping[str, float] | None = None) -> list[TruthLine]:
    """Truth lines straight from parsed estimator rows (no estimate join);
    `unit_costs` maps a normalized description to a unit item cost."""
    out: list[TruthLine] = []
    for i, r in enumerate(rows):
        uic = None
        if unit_costs:
            uic = unit_costs.get(normalize_description(r.description))
        out.append(
            TruthLine(
                line_id=f"Q{i + 1:03d}",
                area=r.area,
                section=r.section,
                group=r.group,
                subgroup=r.subgroup,
                description=r.description,
                unit=normalize_unit(r.unit),
                qty=float(r.quantity),
                value=None if uic is None else float(r.quantity) * float(uic),
                unit_item_cost=uic,
                is_accessory=r.is_accessory,
            )
        )
    return out


# Collisions ------------------------------------------------------------------------


UNIT_ATTR_PREFIX = "UNIT:"


def _unit_attr(unit: str) -> str:
    return f"{UNIT_ATTR_PREFIX}{unit}"


def _without_unit_attrs(key: LineKey) -> LineKey:
    return LineKey(key.kind, frozenset(a for a in key.attrs if not a.startswith(UNIT_ATTR_PREFIX)))


def _has_unit_attr(key: LineKey) -> bool:
    return any(a.startswith(UNIT_ATTR_PREFIX) for a in key.attrs)


@dataclass
class CollisionReport:
    """`resolved` = `merged` + `unit_split`; `unresolved` counts collisions
    the scorer cannot route (a line without a unit)."""

    resolved: int = 0
    unresolved: int = 0
    merged: int = 0
    unit_split: int = 0
    detail: list[dict[str, Any]] = field(default_factory=list)


def classify_truth(lines: Sequence[TruthLine], aliases: Aliases) -> tuple[list[TruthLine], CollisionReport]:
    """Classify truth lines and resolve within-case truth-key collisions.

    Two non-accessory lines of one area with the same LineKey are a
    collision (unit is not part of LineKey). Same unit: merged
    deterministically into the first line of that unit (the quantities and
    values are summed, `merged_from` lists the others). Different units
    (the estimator priced the same identity two ways, for example a track
    tag in FT and its heads in EA, or a demolition note per device and per
    square foot): every line of that key gets the identity attribute
    `UNIT:<unit>` (status `unit_split`), so the lines stay distinct and
    `assign` routes an AI row to the line of its own unit (an AI row of a
    third unit still lands on one of them as a unit mismatch). A collision
    where either line has no unit cannot be routed and is `unresolved`."""
    report = CollisionReport()
    groups: dict[tuple[str, LineKey], list[TruthLine]] = {}
    by_unit: dict[tuple[str, LineKey, str], TruthLine] = {}
    out: list[TruthLine] = []
    for src in lines:
        ln = replace(src, merged_from=list(src.merged_from))  # never mutate the caller's lines
        ln.key = classify_line(ln.description, ln.section, ln.group, ln.unit, subgroup=ln.subgroup, aliases=aliases)
        if ln.is_accessory:
            out.append(ln)
            continue
        base = ln.key
        slot = (ln.area, base)
        group = groups.get(slot)
        if group is None:
            groups[slot] = [ln]
            by_unit[(ln.area, base, ln.unit)] = ln
            out.append(ln)
            continue
        same = by_unit.get((ln.area, base, ln.unit))
        if same is not None:
            same.qty += ln.qty
            if same.value is not None and ln.value is not None:
                same.value += ln.value
            elif ln.value is not None:
                same.value = ln.value
            same.merged_from.append(ln.line_id)
            report.resolved += 1
            report.merged += 1
            report.detail.append(
                {
                    "status": "merged",
                    "key": base.as_text(),
                    "into": same.line_id,
                    "line_id": ln.line_id,
                    "descriptions": [same.description, ln.description],
                }
            )
            continue
        prior = group[0]
        if not ln.unit or any(not g.unit for g in group):
            report.unresolved += 1
            report.detail.append(
                {
                    "status": "unresolved",
                    "key": base.as_text(),
                    "line_ids": [prior.line_id, ln.line_id],
                    "units": [prior.unit, ln.unit],
                    "descriptions": [prior.description, ln.description],
                }
            )
            group.append(ln)
            out.append(ln)
            continue
        for g in group:
            g.key = LineKey(base.kind, base.attrs | {_unit_attr(g.unit)})
        ln.key = LineKey(base.kind, base.attrs | {_unit_attr(ln.unit)})
        group.append(ln)
        by_unit[(ln.area, base, ln.unit)] = ln
        out.append(ln)
        report.resolved += 1
        report.unit_split += 1
        report.detail.append(
            {
                "status": "unit_split",
                "key": base.as_text(),
                "line_ids": [g.line_id for g in group],
                "units": [g.unit for g in group],
                "descriptions": [prior.description, ln.description],
            }
        )
    return out, report


# Assignment -------------------------------------------------------------------------


@dataclass
class AiLine:
    index: int
    row: BoqRow
    key: LineKey
    unit: str
    area: str


@dataclass
class Assignment:
    """AI row index -> truth line index (or None), plus per-pair flags."""

    pairs: dict[int, int | None]
    tiers: dict[int, int]
    unit_mismatch: dict[int, bool]
    area_mismatch: dict[int, bool]


def _area_map(ai_rows: Sequence[BoqRow], est_areas: Sequence[str]) -> dict[str, str]:
    """AI area name -> truth area name, or '' when areas are not comparable
    (either side has a single area, or no truth area is close enough)."""
    ai_areas = sorted({r.area for r in ai_rows})
    est_list = sorted(set(est_areas))
    if len(ai_areas) <= 1 or len(est_list) <= 1:
        return {a: "" for a in ai_areas}
    out: dict[str, str] = {}
    for a in ai_areas:
        na = normalize_description(a)
        best, best_s = "", 0.0
        for e in est_list:
            ne = normalize_description(e)
            s = SequenceMatcher(None, na, ne).ratio()
            if ne and (ne in na or na in ne):
                s = max(s, 0.9)
            if s > best_s:
                best, best_s = e, s
        out[a] = best if best_s >= 0.5 else ""
    return out


def _candidate_tier(ai_key: LineKey, truth_key: LineKey) -> int | None:
    """0 when attrs are equal; 1 + k when the truth attrs are a subset of the
    AI attrs and the k extras are all priced modifiers (AI more specific);
    None otherwise."""
    if ai_key.kind != truth_key.kind:
        return None
    if ai_key.attrs == truth_key.attrs:
        return 0
    if truth_key.attrs <= ai_key.attrs:
        extra = ai_key.attrs - truth_key.attrs
        if extra and all(e in _PRIORITY_INDEX for e in extra):
            return 1 + len(extra)
    return None


def _variant_base(attr: str) -> str | None:
    """`AE` for `AE-EM` or `AE-NL`; None for an attr without a variant."""
    m = _VARIANT_ATTR_RE.match(attr)
    return m.group(1) if m else None


def _variant_fallback_tier(ai_key: LineKey, truth_key: LineKey, truth_tags: frozenset[tuple[str, str]]) -> int | None:
    """VARIANT_FALLBACK_TIER when the two fixture keys differ only by the EM
    or NL variant of one tag and the case has no truth line with the AI's
    own tag form. The variant comes from words in the text (an AE schedule
    row that says EMERGENCY BATTERY PACK keys `AE-EM`), so a tag-first AI
    line `AE - 2' X 4' CEILING LIGHT` still lands on it; when the estimator
    priced both `L1` and `L1-EM` each keeps its own line. None otherwise."""
    if ai_key.kind != truth_key.kind or ai_key.kind not in FIXTURE_KINDS:
        return None
    a_units = frozenset(a for a in ai_key.attrs if a.startswith(UNIT_ATTR_PREFIX))
    t_units = frozenset(a for a in truth_key.attrs if a.startswith(UNIT_ATTR_PREFIX))
    if not t_units <= a_units:
        return None
    a_tags = ai_key.attrs - a_units
    t_tags = truth_key.attrs - t_units
    if len(a_tags) != 1 or len(t_tags) != 1:
        return None
    (a_tag,) = a_tags
    (t_tag,) = t_tags
    if (ai_key.kind, a_tag) in truth_tags:
        return None
    if _variant_base(t_tag) == a_tag or _variant_base(a_tag) == t_tag:
        return VARIANT_FALLBACK_TIER
    return None


def _lost_priority(ai_key: LineKey, truth_key: LineKey) -> tuple[int, ...]:
    """Sort key for ties: prefer losing the LOWEST-priority modifiers (the
    highest priority index), so a GFCI+CTRL row keeps GFCI."""
    extra = ai_key.attrs - truth_key.attrs
    return tuple(sorted((-_PRIORITY_INDEX.get(e, 99) for e in extra)))


def assign(
    ai_rows: Sequence[BoqRow],
    truth: Sequence[TruthLine],
    *,
    aliases: Aliases | None = None,
) -> Assignment:
    """Many-to-one assignment of AI rows to truth lines (accessories on
    both sides are skipped). Candidates share the kind, the AI attrs equal
    or extend the truth attrs with priced modifiers only, and the two items
    have no hard identity conflict (electrical_items.hard_conflict). Ties:
    lowest tier, then the lowest-priority modifier lost, then same section
    and group, then v1 similarity, then truth line order. Unit is not part
    of the candidate test: a 7 EA row on a FT line is assigned and flagged
    `unit_mismatch`, so it is counted but never summed. The one exception is
    a unit-split truth key (classify_truth): its lines carry `UNIT:<unit>`,
    an AI row is first matched to the line of its own unit, and only when
    none fits does it fall back to the split lines as a unit mismatch."""
    aliases = aliases if aliases is not None else GLOBAL_ALIASES
    truth_keys: list[LineKey | None] = []
    for t in truth:
        if t.key is None:
            t.key = classify_line(t.description, t.section, t.group, t.unit, subgroup=t.subgroup, aliases=aliases)
        truth_keys.append(t.key)
    truth_ids = [parse_item(t.description, t.unit) for t in truth]
    amap = _area_map(ai_rows, [t.area for t in truth])
    truth_tags = frozenset(
        (k.kind, a)
        for t, k in zip(truth, truth_keys)
        if k is not None and not t.is_accessory and k.kind in FIXTURE_KINDS
        for a in k.attrs
        if not a.startswith(UNIT_ATTR_PREFIX)
    )
    truth_multi = len({t.area for t in truth}) > 1 and any(amap.values())

    pairs: dict[int, int | None] = {}
    tiers: dict[int, int] = {}
    unit_mismatch: dict[int, bool] = {}
    area_mismatch: dict[int, bool] = {}
    for i, row in enumerate(ai_rows):
        if row.is_accessory:
            continue
        akey = classify_line(row.description, row.section, row.group, row.unit, subgroup=row.subgroup, aliases=aliases)
        aid = parse_item(row.description, row.unit)
        aunit = normalize_unit(row.unit)
        mapped_area = amap.get(row.area, "")
        ukey = LineKey(akey.kind, akey.attrs | {_unit_attr(aunit)})
        best = _best_candidate(row, akey, ukey, aid, aunit, truth, truth_keys, truth_ids, truth_multi, mapped_area, strict_units=True, truth_tags=truth_tags)
        if best is None and any(k is not None and _has_unit_attr(k) for k in truth_keys):
            # an AI row of a unit no split line carries still lands as a unit mismatch
            best = _best_candidate(row, akey, ukey, aid, aunit, truth, truth_keys, truth_ids, truth_multi, mapped_area, strict_units=False, truth_tags=truth_tags)
        if best is None:
            pairs[i] = None
            continue
        j = best[1]
        pairs[i] = j
        tiers[i] = best[0][1]
        unit_mismatch[i] = aunit != truth[j].unit
        area_mismatch[i] = best[0][0] == 1
    return Assignment(pairs, tiers, unit_mismatch, area_mismatch)


def _best_candidate(
    row: BoqRow,
    akey: LineKey,
    ukey: LineKey,
    aid: Any,
    aunit: str,
    truth: Sequence[TruthLine],
    truth_keys: Sequence[LineKey | None],
    truth_ids: Sequence[Any],
    truth_multi: bool,
    mapped_area: str,
    *,
    strict_units: bool,
    truth_tags: frozenset[tuple[str, str]] = frozenset(),
) -> tuple[tuple[Any, ...], int] | None:
    """The best truth candidate of one AI row (see `assign` for the order).
    A unit-split truth line (`UNIT:<unit>` attr) is compared with the AI
    attrs plus the AI row's own unit when `strict_units`, else with its unit
    attr dropped."""
    best: tuple[tuple[Any, ...], int] | None = None
    for j, t in enumerate(truth):
        tkey = truth_keys[j]
        if t.is_accessory or tkey is None:
            continue
        if _has_unit_attr(tkey):
            pair = (ukey, tkey) if strict_units else (akey, _without_unit_attrs(tkey))
        else:
            pair = (akey, tkey)
        tier = _candidate_tier(*pair)
        if tier is None:
            tier = _variant_fallback_tier(pair[0], pair[1], truth_tags)
        if tier is None:
            continue
        if akey.kind not in DEMO_KINDS and hard_conflict(aid, truth_ids[j]):
            continue
        area_ok = (not truth_multi) or (not mapped_area) or (t.area == mapped_area)
        same_group = (_canonical(row.section), _canonical(row.group)) == (_canonical(t.section), _canonical(t.group))
        sim = similarity(row, BoqRow(t.area, t.section, t.group, t.subgroup, t.description, t.qty, t.unit))
        key = (
            0 if area_ok else 1,
            tier,
            _lost_priority(akey, _without_unit_attrs(tkey)),
            0 if same_group else 1,
            0 if aunit == t.unit else 1,
            -round(sim, 4),
            j,
        )
        if best is None or key < best[0]:
            best = (key, j)
    return best


# Scores -------------------------------------------------------------------------------


@dataclass
class LineResult:
    line_id: str
    description: str
    kind: str
    attrs: list[str]
    unit: str
    area: str
    section: str
    group: str
    truth_qty: float
    ai_qty: float
    ai_refs: list[str]
    ai_descriptions: list[str]
    status: str  # exact | within_1 | within_10pct | miss | good | acceptable | none | unit_mismatch
    is_length: bool
    unit_mismatch_rows: int
    signed_error: float
    rel_error: float | None
    value: float | None
    covered_value: float | None
    merged_from: list[str] = field(default_factory=list)

    @property
    def in_band(self) -> bool:
        return self.status in ("exact", "good", "acceptable")


@dataclass
class BidScore:
    refused: bool
    status: str  # pass | miss | safe_refusal
    bid_total: float | None
    vs_sent: dict[str, Any]
    vs_corrected: dict[str, Any]
    vs_estimator: dict[str, Any]
    hours: float | None
    hours_vs_g3: dict[str, Any]
    hours_vs_estimator: dict[str, Any]
    labor_rate: float | None
    g3_rate: float | None
    refusal_reasons: list[str] = field(default_factory=list)


@dataclass
class CaseScore:
    matcher_version: int
    case_id: str | None
    adjudicated: bool
    aliases_sha256: str
    truth_lines_total: int
    count_lines_total: int
    count_lines_exact: int
    count_lines_within_1: int
    length_lines_total: int
    length_lines_produced: int
    length_bands: dict[str, int]
    unit_mismatches: int
    extras_count: int
    extras_qty: float
    extras_value: float | None
    dollar_coverage: float | None
    dollar_coverage_partial: float | None
    dollar_weighted_error: float | None
    truth_cost: float | None
    covered_cost: float | None
    under_cost: float | None
    over_cost: float | None
    lines: list[LineResult]
    extras: list[dict[str, Any]]
    buckets: list[dict[str, Any]]
    collisions: CollisionReport
    bid: BidScore | None
    ai_rows_total: int
    ai_rows_assigned: int
    warnings: list[str] = field(default_factory=list)

    @property
    def exact_rate(self) -> float | None:
        return None if not self.count_lines_total else self.count_lines_exact / self.count_lines_total


def _band(rel: float | None, *, good: float = GOOD_BAND, acceptable: float = ACCEPTABLE_BAND) -> str:
    if rel is None:
        return "none"
    if rel <= good:
        return "good"
    if rel <= acceptable:
        return "acceptable"
    return "miss"


def _count_status(ai_qty: float, truth_qty: float) -> str:
    if truth_qty == 0:
        return "exact" if ai_qty == 0 else "miss"
    if abs(ai_qty - truth_qty) <= EXACT_TOLERANCE * abs(truth_qty):
        return "exact"
    if abs(ai_qty - truth_qty) <= 1.0:
        return "within_1"
    if abs(ai_qty - truth_qty) <= 0.10 * abs(truth_qty):
        return "within_10pct"
    return "miss"


def _bucket_key(key: LineKey, section: str) -> str:
    sec = "DEMO" if _canonical(section) == "DEMOLITION" else "NEW"
    base = sorted(_without_unit_attrs(key).base)
    if key.kind == "fixture":
        base = [re.sub(r"-(EM|NL)$", "", a) for a in base]
    return f"{sec}|{key.kind}|{','.join(base)}"


def _compare(ai_value: float | None, truth_value: float | None) -> dict[str, Any]:
    if ai_value is None or truth_value in (None, 0):
        return {"truth": truth_value, "ai": ai_value, "error": None, "abs_error": None, "band": "none"}
    err = (ai_value - truth_value) / truth_value
    return {
        "truth": truth_value,
        "ai": ai_value,
        "error": round(err, 6),
        "abs_error": round(abs(err), 6),
        "band": _band(abs(err), good=BID_GOOD_BAND, acceptable=BID_ACCEPTABLE_BAND),
    }


def _bid_score(priced: Mapping[str, Any] | None, reference: Mapping[str, Any]) -> BidScore | None:
    if priced is None:
        return None
    sent = ((reference.get("g3_sent") or {}).get("total"))
    corrected = (reference.get("derived") or {}).get("g3_corrected_total")
    est_bid = ((reference.get("estimator_recap") or {}).get("totals") or {}).get("bid")
    g3_hours = (reference.get("g3_labor") or {}).get("hours")
    est_hours = ((reference.get("estimator_recap") or {}).get("totals") or {}).get("hours")
    g3_rate = (reference.get("g3_labor") or {}).get("rate")
    refused = bool(priced.get("refused"))
    totals = priced.get("totals") or {}
    bid_total = None if refused else totals.get("bid_total_usd")
    hours = None if refused else totals.get("hours")
    vs_sent = _compare(bid_total, sent)
    if refused:
        status = "safe_refusal"
    elif vs_sent["band"] in ("good", "acceptable"):
        status = "pass"
    else:
        status = "miss"
    return BidScore(
        refused=refused,
        status=status,
        bid_total=bid_total,
        vs_sent=vs_sent,
        vs_corrected=_compare(bid_total, corrected),
        vs_estimator=_compare(bid_total, est_bid),
        hours=hours,
        hours_vs_g3=_compare(hours, g3_hours),
        hours_vs_estimator=_compare(hours, est_hours),
        labor_rate=priced.get("labor_rate_usd"),
        g3_rate=g3_rate,
        refusal_reasons=list(priced.get("refusal_reasons") or []),
    )


def score_case(
    ai_rows: Sequence[BoqRow],
    reference: Mapping[str, Any] | None = None,
    *,
    truth: Sequence[TruthLine] | None = None,
    adjudicated: bool = True,
    priced: Mapping[str, Any] | None = None,
    aliases: Mapping[str, str] | None = None,
    price_lookup: Callable[[BoqRow], float | None] | None = None,
    case_id: str | None = None,
) -> CaseScore:
    """Score AI BOQ rows (from rows_from_spec, wastage already applied)
    against a reference (eval_reference reference.json) or explicit truth
    lines. `aliases` are the case aliases on top of GLOBAL_ALIASES (a
    reference's `aliases` entry is used when none is given). `priced` is a
    bdr.priced_estimate/1 artifact for the bid metrics; `price_lookup`
    values unmatched AI rows (extras) when available."""
    reference = reference or {}
    if truth is None:
        truth = truth_lines_from_reference(reference, adjudicated=adjudicated)
    case_aliases = aliases if aliases is not None else (reference.get("aliases") or {})
    merged = merge_aliases(case_aliases)
    truth_lines, collisions = classify_truth(list(truth), merged)
    assignment = assign(ai_rows, truth_lines, aliases=merged)
    warnings: list[str] = []
    if collisions.unresolved:
        warnings.append(f"{collisions.unresolved} unresolved truth-key collision(s)")

    by_truth: dict[int, list[int]] = defaultdict(list)
    for i, j in assignment.pairs.items():
        if j is not None:
            by_truth[j].append(i)

    lines: list[LineResult] = []
    count_total = count_exact = count_within_1 = 0
    length_total = length_produced = 0
    bands: Counter[str] = Counter()
    unit_mismatches = 0
    truth_cost = covered = under = over = 0.0
    in_band_cost = 0.0
    weighted_err = 0.0
    values_missing: list[str] = []
    for j, t in enumerate(truth_lines):
        if t.is_accessory or t.key is None or t.key.kind == "allowance":
            continue
        if t.qty <= 0:
            warnings.append(f"truth line {t.line_id} has quantity {t.qty}; skipped")
            continue
        rows = by_truth.get(j, [])
        same_unit = [i for i in rows if not assignment.unit_mismatch.get(i)]
        mismatched = [i for i in rows if assignment.unit_mismatch.get(i)]
        unit_mismatches += len(mismatched)
        ai_qty = float(sum(ai_rows[i].quantity for i in same_unit))
        refs = [ai_rows[i].ref or ai_rows[i].description for i in rows]
        descriptions = [ai_rows[i].description for i in rows]
        rel = None if t.qty == 0 else abs(ai_qty - t.qty) / t.qty
        if t.is_length:
            length_total += 1
            if same_unit and ai_qty > 0:
                status = _band(rel)
                length_produced += 1
            elif mismatched:
                status = "unit_mismatch"
            else:
                status = "none"
            bands[status] += 1
        else:
            count_total += 1
            if not rows and ai_qty == 0:
                status = "miss"
            else:
                status = _count_status(ai_qty, t.qty)
            if status == "exact":
                count_exact += 1
            if status in ("exact", "within_1"):
                count_within_1 += 1
        value = t.value
        covered_v: float | None = None
        if value is None:
            values_missing.append(t.line_id)
        else:
            truth_cost += value
            uic = value / t.qty
            covered_v = min(ai_qty, t.qty) * uic
            covered += covered_v
            under += max(t.qty - ai_qty, 0.0) * uic
            over += max(ai_qty - t.qty, 0.0) * uic
            if status in ("exact", "good", "acceptable"):
                in_band_cost += value
            weighted_err += min(1.0, abs(ai_qty - t.qty) / t.qty) * value
        lines.append(
            LineResult(
                line_id=t.line_id,
                description=t.description,
                kind=t.key.kind,
                attrs=sorted(t.key.attrs),
                unit=t.unit,
                area=t.area,
                section=t.section,
                group=t.group,
                truth_qty=t.qty,
                ai_qty=ai_qty,
                ai_refs=refs,
                ai_descriptions=descriptions,
                status=status,
                is_length=t.is_length,
                unit_mismatch_rows=len(mismatched),
                signed_error=round(ai_qty - t.qty, 4),
                rel_error=None if rel is None else round(rel, 6),
                value=value,
                covered_value=covered_v,
                merged_from=list(t.merged_from),
            )
        )

    extras: list[dict[str, Any]] = []
    extras_qty = 0.0
    extras_value: float | None = 0.0
    for i, row in enumerate(ai_rows):
        if row.is_accessory or i not in assignment.pairs or assignment.pairs[i] is not None:
            continue
        key = classify_line(row.description, row.section, row.group, row.unit, subgroup=row.subgroup, aliases=merged)
        extras_qty += float(row.quantity)
        v = price_lookup(row) if price_lookup else None
        if v is None:
            extras_value = None
        elif extras_value is not None:
            extras_value += float(v) * float(row.quantity)
        extras.append(
            {
                "ref": row.ref,
                "description": row.description,
                "section": row.section,
                "group": row.group,
                "qty": row.quantity,
                "unit": normalize_unit(row.unit),
                "kind": key.kind,
                "attrs": sorted(key.attrs),
            }
        )

    buckets: dict[str, dict[str, Any]] = {}
    for ln in lines:
        key = LineKey(ln.kind, frozenset(ln.attrs))
        b = buckets.setdefault(_bucket_key(key, ln.section), {"truth_qty": 0.0, "ai_qty": 0.0, "lines": 0, "unit": ln.unit})
        b["truth_qty"] += ln.truth_qty
        b["ai_qty"] += ln.ai_qty
        b["lines"] += 1
    for i, row in enumerate(ai_rows):
        if row.is_accessory or i not in assignment.pairs or assignment.pairs[i] is not None:
            continue
        key = classify_line(row.description, row.section, row.group, row.unit, subgroup=row.subgroup, aliases=merged)
        b = buckets.setdefault(_bucket_key(key, row.section), {"truth_qty": 0.0, "ai_qty": 0.0, "lines": 0, "unit": normalize_unit(row.unit)})
        if normalize_unit(row.unit) == b["unit"]:
            b["ai_qty"] += float(row.quantity)
    bucket_rows = [{"bucket": k, **v} for k, v in sorted(buckets.items())]

    assigned = sum(1 for i, j in assignment.pairs.items() if j is not None)
    ai_total = sum(1 for r in ai_rows if not r.is_accessory)
    values_known = truth_cost > 0
    if values_missing and values_known:
        warnings.append(f"{len(values_missing)} truth line(s) without a value count as 0 in the dollar metrics: {', '.join(values_missing[:8])}")
    score = CaseScore(
        matcher_version=MATCHER_VERSION,
        case_id=case_id or reference.get("case_id"),
        adjudicated=adjudicated and bool(reference.get("quantity_lines_adjudicated")),
        aliases_sha256=aliases_sha256(merged),
        truth_lines_total=count_total + length_total,
        count_lines_total=count_total,
        count_lines_exact=count_exact,
        count_lines_within_1=count_within_1,
        length_lines_total=length_total,
        length_lines_produced=length_produced,
        length_bands={k: bands.get(k, 0) for k in ("good", "acceptable", "miss", "none", "unit_mismatch")},
        unit_mismatches=unit_mismatches,
        extras_count=len(extras),
        extras_qty=round(extras_qty, 4),
        extras_value=None if extras_value is None or not extras else round(extras_value, 2),
        dollar_coverage=round(in_band_cost / truth_cost, 6) if values_known else None,
        dollar_coverage_partial=round(covered / truth_cost, 6) if values_known else None,
        dollar_weighted_error=round(weighted_err / truth_cost, 6) if values_known else None,
        truth_cost=round(truth_cost, 2) if values_known else None,
        covered_cost=round(covered, 2) if values_known else None,
        under_cost=round(under, 2) if values_known else None,
        over_cost=round(over, 2) if values_known else None,
        lines=lines,
        extras=extras,
        buckets=bucket_rows,
        collisions=collisions,
        bid=_bid_score(priced, reference),
        ai_rows_total=ai_total,
        ai_rows_assigned=assigned,
        warnings=warnings,
    )
    if not values_known and lines:
        score.warnings.append("no line values: dollar metrics not computed")
    return score


# Reports ----------------------------------------------------------------------------


def _strip_dollars(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_dollars(v) for k, v in obj.items() if not _is_dollar_key(k)}
    if isinstance(obj, list):
        return [_strip_dollars(v) for v in obj]
    return obj


_DOLLAR_KEYS = {
    "value", "covered_value", "extras_value", "truth_cost", "covered_cost", "under_cost", "over_cost",
    "bid_total", "truth", "ai", "labor_rate", "g3_rate", "unit_item_cost",
}


def _is_dollar_key(key: str) -> bool:
    return key in _DOLLAR_KEYS


def score_json(score: CaseScore, *, include_dollars: bool = False) -> dict[str, Any]:
    """A JSON-ready dict. Ratios (coverage, errors) are always included;
    absolute dollar figures only with `include_dollars`."""
    data = asdict(score)
    data["exact_rate"] = score.exact_rate
    if score.bid is not None:
        data["bid"] = asdict(score.bid)
    if not include_dollars:
        data = _strip_dollars(data)
    return data


def _fmt(q: float | None) -> str:
    if q is None:
        return "n/a"
    return f"{q:,.2f}".rstrip("0").rstrip(".")


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def _cell(s: str, limit: int = 60) -> str:
    s = s.replace("|", "/")
    return s if len(s) <= limit else s[: limit - 3] + "..."


def score_markdown(score: CaseScore, *, include_dollars: bool = False) -> str:
    """Markdown report. Dollar amounts appear only with `include_dollars`;
    ratios always do."""
    out = [f"# Scorer v{score.matcher_version} report" + (f": case {score.case_id}" if score.case_id else ""), ""]
    out += [
        "| Metric | Value |",
        "|---|---|",
        f"| Truth: {'adjudicated' if score.adjudicated else 'raw estimator'} | aliases {score.aliases_sha256[:12]} |",
        f"| Count lines exact | {score.count_lines_exact} of {score.count_lines_total} ({_pct(score.exact_rate)}) |",
        f"| Count lines within 1 | {score.count_lines_within_1} of {score.count_lines_total} |",
        f"| Length lines produced | {score.length_lines_produced} of {score.length_lines_total} |",
        "| Length bands | " + ", ".join(f"{k} {v}" for k, v in score.length_bands.items()) + " |",
        f"| Unit mismatches | {score.unit_mismatches} |",
        f"| AI rows assigned | {score.ai_rows_assigned} of {score.ai_rows_total} |",
        f"| Extras (unmatched AI rows) | {score.extras_count} rows, qty {_fmt(score.extras_qty)} |",
        f"| Dollar coverage (3.6, in band) | {_pct(score.dollar_coverage)} (partial credit {_pct(score.dollar_coverage_partial)}) |",
        f"| Dollar-weighted error | {_pct(score.dollar_weighted_error)} |",
        f"| Truth-key collisions | {score.collisions.merged} merged, {score.collisions.unit_split} split by unit, {score.collisions.unresolved} unresolved |",
    ]
    if include_dollars and score.truth_cost is not None:
        out.append(f"| Truth cost / covered / under / over | {_fmt(score.truth_cost)} / {_fmt(score.covered_cost)} / {_fmt(score.under_cost)} / {_fmt(score.over_cost)} |")
    if score.bid is not None:
        b = score.bid
        out.append(f"| Bid | {b.status}; vs sent {_pct(b.vs_sent.get('error'))} ({b.vs_sent.get('band')}); vs corrected {_pct(b.vs_corrected.get('error'))}; vs estimator {_pct(b.vs_estimator.get('error'))} |")
        out.append(f"| Hours | vs G3 {_pct(b.hours_vs_g3.get('error'))}; vs estimator {_pct(b.hours_vs_estimator.get('error'))} |")
        if include_dollars:
            out.append(f"| Bid total | {_fmt(b.bid_total)} (sent {_fmt(b.vs_sent.get('truth'))}) |")
    out += ["", "## Lines", "", "| Truth | Kind | Truth qty | AI qty | Status | AI rows |", "|---|---|---|---|---|---|"]
    for ln in score.lines:
        out.append(
            f"| {_cell(ln.description)} | {ln.kind}[{', '.join(ln.attrs)}] | {_fmt(ln.truth_qty)} {ln.unit} |"
            f" {_fmt(ln.ai_qty)} | {ln.status}{' (+%d unit mismatch)' % ln.unit_mismatch_rows if ln.unit_mismatch_rows else ''} | {len(ln.ai_refs)} |"
        )
    out += ["", f"## Extras ({score.extras_count})", ""]
    out += [f"- [{e['section']} / {e['group']}] {_cell(e['description'])} = {_fmt(e['qty'])} {e['unit']} ({e['kind']})" for e in score.extras] or ["- none"]
    out += ["", "## Buckets", "", "| Bucket | Lines | Truth qty | AI qty |", "|---|---|---|---|"]
    out += [f"| {b['bucket']} | {b['lines']} | {_fmt(b['truth_qty'])} | {_fmt(b['ai_qty'])} |" for b in score.buckets]
    if score.collisions.detail:
        out += ["", "## Truth-key collisions", ""]
        out += [f"- {d['status']}: {d['key']} {d.get('descriptions')}" for d in score.collisions.detail]
    if score.warnings:
        out += ["", "## Warnings", ""] + [f"- {w}" for w in score.warnings]
    return "\n".join(out) + "\n"


# Estimator workbook and BOQ spec readers (moved from scripts/plans_reader_golden.py)

_BLANK_SHEETS = {"SHEET1", "CHART1"}
_ACCESSORY_SUBGROUP = "OTHER CONDUIT ACCESSORIES"


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if re.fullmatch(r"-?\d+(?:\.\d+)?", s):
            return float(s)
        if re.fullmatch(r"=[\d.+\-*/() ]+", s):  # cached value missing, simple math
            try:
                return float(eval(s[1:], {"__builtins__": {}}, {}))  # noqa: S307
            except Exception:  # noqa: BLE001
                return None
    return None


def _find_header_row(ws: Any) -> int | None:
    for r in range(1, min(ws.max_row, 30) + 1):
        if _clean_text(ws.cell(r, 2).value).upper() == "DESCRIPTION":
            return r
    return None


def parse_estimator_boq(xlsx_bytes: bytes) -> list[BoqRow]:
    """Walk every area sheet of an estimator BOQ and return its item rows with
    the heading hierarchy (section, group, subgroup) resolved."""
    raw_wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
    val_wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    level0 = set(BOQ_LEVEL0)
    groups = set(BOQ_GROUPS)
    rows: list[BoqRow] = []
    for ws in raw_wb.worksheets:
        title = _clean_text(ws.title)
        header = _find_header_row(ws)
        if header is None or (title.upper() in _BLANK_SHEETS and ws.max_row <= 1):
            continue  # the blank 'Sheet1' / 'Chart1' and any non-BOQ sheet
        vws = val_wb[ws.title]
        section, group, subgroup = "NEW WORK", "", ""
        for r in range(header + 1, ws.max_row + 1):
            b_cell = ws.cell(r, 2)
            desc_raw = b_cell.value
            desc = _clean_text(desc_raw)
            if not desc:
                continue
            qty = _num(vws.cell(r, 3).value)
            if qty is None:
                qty = _num(ws.cell(r, 3).value)
            unit = normalize_unit(vws.cell(r, 4).value)
            name, _note = _heading_name(desc_raw)
            canon = _canonical(name)
            bold = bool(b_cell.font and b_cell.font.b)
            align = (b_cell.alignment.horizontal or "") if b_cell.alignment else ""
            in_vocab = canon in level0 or canon in groups or canon in _KNOWN_SUBGROUPS
            is_heading = (qty is None and (bold or in_vocab or (desc.isupper() and len(desc) <= 80)))
            # A vocabulary heading with a stray number in C (seen on
            # OTHER CONDUIT ACCESSORIES) is still a heading when it has no unit.
            if not is_heading and in_vocab and bold and not unit:
                is_heading = True
            if is_heading:
                if canon in level0:
                    section, group, subgroup = canon, "", ""
                elif canon in groups:
                    group, subgroup = canon, ""
                elif canon in _KNOWN_SUBGROUPS:
                    subgroup = name
                elif align == "center":
                    group, subgroup = name, ""
                elif align == "left" or group:
                    subgroup = name
                else:
                    group, subgroup = name, ""
                continue
            if qty is None:
                continue  # a note row
            a_raw = ws.cell(r, 1).value
            accessory = (
                align == "right"
                or _canonical(subgroup) == _ACCESSORY_SUBGROUP
                or (a_raw in (None, "") and bool(_ACCESSORY_WORDS.search(desc.upper())))
            )
            if not group and subgroup:
                csub = _canonical(subgroup)
                group = next((g for pre, g in _SUBGROUP_PARENT if csub.startswith(pre)), "")
            rows.append(
                BoqRow(
                    area=title,
                    section=section,
                    group=group or (section if section == "DEMOLITION" else ""),
                    subgroup=subgroup,
                    description=desc,
                    quantity=qty,
                    unit=unit,
                    is_accessory=accessory,
                    ref=f"{title}!B{r}",
                )
            )
    return rows


def _boq_from(spec: dict[str, Any]) -> dict[str, Any]:
    if "areas" not in spec and isinstance(spec.get("boq"), dict):
        return spec["boq"]
    return spec


def rows_from_spec(boq_spec: dict[str, Any]) -> list[BoqRow]:
    """Flatten a BOQ_SPEC into rows, applying the renderer's wastage rule
    (quantity x 1.1 on FT rows with wastage_applies). Accepts the full main
    agent output too (uses its 'boq')."""
    spec = _boq_from(boq_spec)
    rows: list[BoqRow] = []
    for ai, area in enumerate(spec.get("areas") or []):
        area_name = _clean_text(area.get("area")) or "SHEET"
        for si, sec in enumerate(area.get("sections") or []):
            section = _canonical(_clean_text(sec.get("heading"))) or "NEW WORK"
            for gi, grp in enumerate(sec.get("groups") or []):
                gname = _canonical(_heading_name(grp.get("name"))[0])
                if section == "DEMOLITION" and gname not in BOQ_GROUPS:
                    gname = "DEMOLITION"  # the estimators list demolition rows ungrouped
                for ui, sub in enumerate(grp.get("subgroups") or []):
                    sname = _heading_name(sub.get("name"))[0]
                    for ii, item in enumerate(sub.get("items") or []):
                        qty = _num(item.get("quantity")) or 0.0
                        unit = normalize_unit(item.get("unit"))
                        if unit == "FT" and item.get("wastage_applies"):
                            qty = qty * WASTAGE_FACTOR
                        desc = _clean_text(item.get("description"))
                        caveat = _clean_text(item.get("caveat"))
                        if caveat and caveat.upper() not in desc.upper():
                            desc = f"{desc} {caveat}"
                        rows.append(
                            BoqRow(
                                area=area_name,
                                section=section,
                                group=gname,
                                subgroup=sname,
                                description=desc,
                                quantity=qty,
                                unit=unit,
                                is_accessory=bool(item.get("is_accessory")),
                                ref=f"areas[{ai}].sections[{si}].groups[{gi}]"
                                f".subgroups[{ui}].items[{ii}]",
                            )
                        )
    return rows


def find_backup_boq(prefix: str, backup_dir: Path = DEFAULT_BACKUP_DIR) -> Path:
    """The estimator `BOQ - COUNTS.xlsx` of the project whose id starts with
    `prefix` in the production backup (latest round, latest upload)."""
    rows = json.loads((backup_dir / "data" / "project_files.json").read_text())
    cands = [
        r for r in rows
        if r.get("category") == "boq"
        and str(r.get("project_id", "")).startswith(prefix)
        and str(r.get("filename", "")).lower().endswith((".xlsx", ".xlsm"))
    ]
    if not cands:
        raise FileNotFoundError(f"no BOQ xlsx for project prefix {prefix!r} in {backup_dir}")
    if len({r["project_id"] for r in cands}) > 1:
        raise ValueError(f"project prefix {prefix!r} is ambiguous")
    cands.sort(
        key=lambda r: (
            "COUNTS" in str(r.get("filename", "")).upper(),
            r.get("submission_round") or 0,
            r.get("created_at") or "",
        )
    )
    path = backup_dir / "storage" / "project-files" / cands[-1]["storage_path"]
    if not path.exists():
        raise FileNotFoundError(str(path))
    return path


__all__ = [
    "MATCHER_VERSION", "GOOD_BAND", "ACCEPTABLE_BAND", "KINDS", "GLOBAL_ALIASES", "MODIFIER_PRIORITY",
    "LineKey", "classify_line", "merge_aliases", "aliases_sha256",
    "TruthLine", "truth_lines_from_reference", "truth_lines_from_rows", "classify_truth", "CollisionReport", "UNIT_ATTR_PREFIX",
    "Assignment", "assign", "LineResult", "BidScore", "CaseScore", "score_case", "score_json", "score_markdown",
    "parse_estimator_boq", "rows_from_spec", "find_backup_boq", "DEFAULT_BACKUP_DIR",
]

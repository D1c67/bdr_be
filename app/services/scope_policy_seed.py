"""The code seed of the G3 scope policy (docs/AI_ESTIMATING_BUILD_SPEC.md 5.8,
WS5; validated by `scope_policy.validate`).

Evidence counts come from `scripts/mine_scope_policy.py` over the production
backup of 2026-10-01 with the holdout projects of spec 8.1 excluded
(`eval_manifest.excluded_projects("scope_policy")`); the report is
`EVAL_ROOT/reports/scope_policy_mining.md`. Sent-proposal evidence uses the
latest sent proposal docx per project (`proposal_sends.status = 'sent'` joined
to `project_files` by `file_id`). No dollar figure of any real project is
stored here; the only amounts are policy values ($0 standard allowances, the
NRS 338 statute threshold).

Rule ids are stable: changing one is a deliberate policy change (bump SEED_SHA
and SEED_RULE_IDS together, and the tests say so).

Owner decisions carried: Q12 (cutting, patching and painting is a hard
exclusion with a clarification line), Q13 (unknown wage type defaults to
prevailing wage with needs_review wage_unknown), Q14 (thermostat relocations
and #18/4C control cable are carried even when mechanical notes assign ATC
wiring to the controls contractor).
"""

from __future__ import annotations

from typing import Any

from app.services.plans_reader_schemas import ESTIMATE_ALLOWANCES

# Mined over the latest sent proposal of every non-holdout project with a send.
_SENT = "sent proposals, latest send per project (backup 2026-10-01, holdout excluded)"
_SENT_N = 39
_WORKBOOKS = "estimator estimate workbooks (backup 2026-10-01, holdout excluded)"
_WB_N = 64
_BOQS = "estimator BOQ workbooks, latest per project (backup 2026-10-01, holdout excluded)"
_BOQ_N = 62
_SURVEY = "WS5 design survey of estimate workbooks (backup 2026-10-01)"
_E5 = "spec 3.3 E5 house-exclusion topics (7154 AI run priced it as an extra allowance)"

TEMP_POWER_LINE = "This proposal does not include temporary power of any kind. Unless otherwise noted."
TEMPLATE_EXCLUSION_LINE = (
    "This quote excludes all scope not lineated in the description, design, load or power "
    "studies, permits, ads, XRAY, GPR, core-drilling, saw cut, concrete removal, moves or "
    "changes of equipment, paint, patch, hard digging & caliche & drywall repair/replacements "
    "unless otherwise noted."
)
CATCH_ALL_LINE = "Any other scope not enlisted above to be performed by others."
BOND_LINE = "Add 3% if Payment and Performance bond is required."
CUT_PATCH_PAINT_LINE = "Cutting, patching and painting by others."
THERMOSTAT_CLARIFICATION = (
    "Thermostat relocations and control cable included; ATC devices and programming by others."
)

# The four template sentences every sent proposal carries (mining checks them).
TEMPLATE_LINES: tuple[str, ...] = (
    TEMP_POWER_LINE,
    TEMPLATE_EXCLUSION_LINE,
    CATCH_ALL_LINE,
    BOND_LINE,
)


def _ev(source: str, support: int, of: int, note: str | None = None) -> dict[str, Any]:
    return {"source": source, "support": support, "of": of, "note": note}


def _match(
    patterns: list[str],
    *,
    systems: list[str] | None = None,
    exempt_subgroups: list[str] | None = None,
    exempt_patterns: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "patterns": patterns,
        "systems": systems or [],
        "exempt_subgroups": exempt_subgroups or [],
        "exempt_patterns": exempt_patterns or [],
    }


def _rule(
    rule_id: str,
    topic: str,
    kind: str,
    decision: str,
    match: dict[str, Any],
    default_text: str,
    proposal_line: str | None,
    evidence: list[dict[str, Any]],
    *,
    unless: list[str] | None = None,
    label: str,
    template_covered: bool = False,
    review: str | None = None,
) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "label": label,
        "topic": topic,
        "kind": kind,
        "decision": decision,
        "match": match,
        "unless": unless or [],
        "default_text": default_text,
        "proposal_line": proposal_line,
        "template_covered": template_covered,
        "review": review,
        "evidence": evidence,
    }


# The nine standard allowance rows are rendered at $0 and never match a rule.
_STD_ALLOWANCE_EXEMPT = {
    "studies": [
        r"^\s*arc\s+flash\s+study\s*$",
        r"^\s*protection\s+devices\s+coordination\s+study\s*$",
    ],
    "permits": [r"^\s*permit\s+and\s+utility\s+fees\s*$"],
    "core": [r"^\s*core\s+drill\s*$"],
    "commissioning": [r"^\s*testing\s+and\s+commissioning\s*$"],
    # Firestop caulking is the standard CAULKING SEALANT row, never excluded.
    "caulking": [r"^\s*caulking\s+sealant\s*$", r"\bcaulk(?:ing|s)?\b"],
}

# Group and subgroup headings where "patch" and "paint" name products, not
# work (patch panels, patch modules, painted backboards, fixture finishes).
_PRODUCT_HEADINGS = [
    "LOW VOLTAGE WORK",
    "LOW VOLTAGE WIRING",
    "OUTLETS/DEVICES",
    "RACK DETAILS",
    "CAMERAS",
    "LIGHTING FIXTURES",
    "FIXTURES",
    "LIGHTING POLE WORK",
    "POLE W/ ACCESSORIES",
]

RULES: list[dict[str, Any]] = [
    # The four template sentences of every sent proposal.
    _rule(
        "EX-TEMP-POWER",
        "temporary_power",
        "hard",
        "exclude",
        _match(
            [
                r"\btemp(?:orary)?\.?\s+(?:power|lighting|electric(?:al)?|distribution|service|hepa)\b",
                r"\bconstruction\s+power\b",
                r"\bnegative\s+air\b.{0,40}\bpower\b",
                r"\bhepa\b",
            ]
        ),
        "Temporary power and lighting, HEPA and negative air power included, are excluded.",
        TEMP_POWER_LINE,
        [
            _ev(_SENT, _SENT_N, _SENT_N, "template line"),
            _ev(_WORKBOOKS, 0, _WB_N, "no estimate line carries temporary power"),
        ],
        label="Temporary power and lighting",
        template_covered=True,
    ),
    _rule(
        "EX-TEMPLATE-SCOPE",
        "template_exclusions",
        "hard",
        "exclude",
        _match(
            [
                r"\b(?:engineering|design)\s+(?:services|fees)\b",
                r"\bstamped\s+(?:drawings|calculations)\b",
            ]
        ),
        "Scope not lineated in the description is excluded, as are design, studies, permits, "
        "ads, XRAY, GPR, core drilling, saw cut, concrete removal, equipment moves, paint, "
        "patch, hard digging, caliche and drywall repair unless otherwise noted.",
        TEMPLATE_EXCLUSION_LINE,
        [_ev(_SENT, _SENT_N, _SENT_N, "template exclusion sentence, verbatim")],
        label="Template exclusion sentence",
        template_covered=True,
    ),
    _rule(
        "EX-CATCH-ALL",
        "catch_all",
        "soft",
        "by_others",
        _match([]),
        "Anything not listed in the proposal scope is by others (G3 default).",
        CATCH_ALL_LINE,
        [_ev(_SENT, _SENT_N, _SENT_N, "template line")],
        label="Scope not listed is by others",
        template_covered=True,
    ),
    _rule(
        "EX-BOND",
        "bonds",
        "hard",
        "exclude",
        _match(
            [
                r"\bpayment\s+(?:and|&)\s+performance\s+bonds?\b",
                r"\bperformance\s+bonds?\b",
                r"\bbid\s+bonds?\b",
            ]
        ),
        "Bonds are never a BOQ row; the proposal adds 3% when a payment and performance bond "
        "is required.",
        BOND_LINE,
        [_ev(_SENT, _SENT_N, _SENT_N, "template line")],
        label="Payment and performance bond",
        template_covered=True,
    ),
    # House exclusion topics (E5); the template sentence covers most of them.
    _rule(
        "EX-STUDIES",
        "studies",
        "hard",
        "exclude",
        _match(
            [
                r"\b(?:coordination|load|power|short[\s-]?circuit|arc[\s-]?flash)\s+stud(?:y|ies)\b",
                r"\bselective\s+coordination\b",
            ],
            exempt_patterns=_STD_ALLOWANCE_EXEMPT["studies"],
        ),
        "Design, load, power, coordination and arc flash studies are excluded.",
        None,
        [_ev(_SENT, _SENT_N, _SENT_N, "template exclusion sentence")],
        label="Engineering studies",
        template_covered=True,
    ),
    _rule(
        "EX-PERMITS",
        "permits",
        "hard",
        "exclude",
        _match(
            [
                r"\bpermits?\s+(?:and\s+)?(?:utility\s+)?fees?\b",
                r"\b(?:building|electrical|construction)\s+permits?\b",
                r"\bpermits\b",
            ],
            exempt_patterns=_STD_ALLOWANCE_EXEMPT["permits"],
        ),
        "Permits and permit fees are excluded.",
        None,
        [_ev(_SENT, _SENT_N, _SENT_N, "template exclusion sentence")],
        label="Permits",
        template_covered=True,
    ),
    _rule(
        "EX-UTILITY-CHARGES",
        "utility_charges",
        "hard",
        "exclude",
        _match(
            [
                r"\bnv\s+energy\s+(?:fees?|charges?)\b",
                r"\butility\s+(?:company\s+)?(?:fees?|charges?)\b",
            ],
            exempt_patterns=_STD_ALLOWANCE_EXEMPT["permits"],
        ),
        "Utility company fees and charges are excluded.",
        None,
        [_ev(_SENT, _SENT_N, _SENT_N, "covered by the permits wording of the template")],
        label="Utility charges",
        template_covered=True,
    ),
    _rule(
        "EX-CORE-XRAY",
        "core_drill_scanning",
        "soft",
        "exclude",
        _match(
            [
                r"\bcore[\s-]?drill(?:ing|s)?\b",
                r"\bx[\s-]?rays?\b",
                r"\bgpr\b",
                r"\bground[\s-]penetrating\s+radar\b",
                r"\bscanning\s+of\s+(?:the\s+)?slabs?\b",
            ],
            exempt_patterns=_STD_ALLOWANCE_EXEMPT["core"],
        ),
        "Core drilling, XRAY and GPR scanning are excluded unless drawn with a quantity.",
        None,
        [_ev(_SENT, _SENT_N, _SENT_N, "template exclusion sentence")],
        unless=["drawn_with_quantity"],
        label="Core drilling and scanning",
        template_covered=True,
    ),
    _rule(
        "EX-SAWCUT-CONCRETE",
        "saw_cut_concrete",
        "soft",
        "exclude",
        _match([r"\bsaw[\s-]?cut(?:ting|s)?\b", r"\bconcrete\s+(?:removal|demolition)\b"]),
        "Saw cutting and concrete removal are excluded unless drawn with a quantity.",
        None,
        [
            _ev(_SENT, _SENT_N, _SENT_N, "template exclusion sentence"),
            _ev(_BOQS, 10, _BOQ_N, "carried when drawn, so the rule is soft"),
        ],
        unless=["drawn_with_quantity"],
        label="Saw cut and concrete removal",
        template_covered=True,
    ),
    _rule(
        "EX-CUT-PATCH-PAINT",
        "cutting_patching_painting",
        "hard",
        "exclude",
        _match(
            [
                r"\bcutting,?\s+(?:and\s+)?patching\b",
                r"\bcut(?:ting)?\s+(?:and|&)\s+patch(?:ing)?\b",
                r"\bpatch(?:ing|es)?\b(?!\s*(?:panels?|cords?|modules?)\b)",
                r"\bpaint(?:ing)?\b",
                r"\bdrywall\b",
                r"\bfinish\s+restoration\b",
            ],
            exempt_subgroups=_PRODUCT_HEADINGS,
            exempt_patterns=[
                r"\bplywood\b",
                r"\bbackboards?\b",
                r"\bpaint\s+finish\b",
                r"\bracks?\b",
                r"\bfiber\b",
                r"\bpoles?\b",
            ],
        ),
        "Cutting, patching and painting associated with the electrical work is excluded, even "
        "where the drawings assign it to the electrical contractor (owner decision Q12).",
        CUT_PATCH_PAINT_LINE,
        [
            _ev(_SENT, _SENT_N, _SENT_N, "template exclusion sentence names paint and patch"),
            _ev("7154 sent proposal", 1, 1, "excluded although E2.01 note 5 assigns it to the EC"),
        ],
        label="Cutting, patching and painting",
        template_covered=True,
    ),
    _rule(
        "EX-HARD-DIG",
        "hard_digging",
        "hard",
        "exclude",
        _match(
            [
                r"\bhard\s+dig(?:ging)?\b",
                r"\bcaliche\b",
                r"\brock\s+(?:excavation|removal)\b",
            ]
        ),
        "Hard digging, caliche and rock excavation are excluded.",
        None,
        [_ev(_SENT, _SENT_N, _SENT_N, "template exclusion sentence")],
        label="Hard digging and caliche",
        template_covered=True,
    ),
    _rule(
        "EX-EQUIP-MOVES",
        "equipment_moves",
        "soft",
        "exclude",
        _match(
            [
                r"\bmoves?\s+or\s+changes?\s+of\s+equipment\b",
                r"\brelocat\w*\s+(?:of\s+)?(?:owner|existing)\s+equipment\b",
            ]
        ),
        "Moves or changes of owner equipment are excluded unless the documents assign them to "
        "the electrical contractor.",
        None,
        [_ev(_SENT, _SENT_N, _SENT_N, "template exclusion sentence")],
        unless=["assigned_to_ec_explicitly"],
        label="Equipment moves",
        template_covered=True,
    ),
    _rule(
        "EX-FIRESTOP-LABELS",
        "firestop_labels",
        "soft",
        "exclude",
        _match(
            [
                r"\bfirestop(?:ping)?\s+(?:system\s+)?label(?:s|ing)?\b",
                r"\b(?:rated\s+)?box\s+pads?\b",
                r"\bputty\s+pads?\b",
            ]
        ),
        "Firestop labeling and putty pads are excluded; caulking stays as the standard $0 "
        "allowance.",
        "Firestop labeling and putty pads by others.",
        [_ev(_BOQS, 0, _BOQ_N, "no estimator row carries firestop labels or putty pads")],
        unless=["assigned_to_ec_explicitly"],
        label="Firestop labels and putty pads",
    ),
    _rule(
        "EX-FIRESTOPPING",
        "firestopping",
        "soft",
        "exclude",
        _match(
            [r"\bfirestop(?:ping|s)?\b", r"\bfire[\s-]stopping\b"],
            exempt_patterns=_STD_ALLOWANCE_EXEMPT["caulking"]
            + [r"\blabel(?:s|ing)?\b", r"\bpads?\b"],
        ),
        "Firestopping of penetrations is excluded unless drawn with a quantity or assigned to "
        "the electrical contractor; firestop caulking stays as the standard $0 CAULKING "
        "SEALANT allowance.",
        "Firestopping by others.",
        [
            _ev(_BOQS, 0, _BOQ_N, "no estimator row carries firestopping as its own scope"),
            _ev(_E5, 1, 1, "FIRESTOP"),
        ],
        unless=["drawn_with_quantity", "assigned_to_ec_explicitly"],
        label="Firestopping",
    ),
    _rule(
        "EX-BIM",
        "bim_coordination",
        "hard",
        "exclude",
        _match(
            [
                r"\bbim\b",
                r"\b3d\s+(?:coordination|model(?:ing)?)\b",
                r"\bcoordination\s+drawings?\b",
                r"\bintertrade\s+coordination\b",
            ]
        ),
        "BIM, 3D modeling and coordination drawings are excluded (catch-all line).",
        None,
        [_ev(_BOQS, 0, _BOQ_N, "no estimator row carries BIM or coordination drawings")],
        label="BIM and coordination drawings",
    ),
    _rule(
        "EX-TRAINING",
        "owner_training",
        "soft",
        "exclude",
        _match(
            [
                r"\bowner\s+training\b",
                r"\btraining\s+(?:sessions?|videos?|recordings?)\b",
                r"\bfactory[\s-]authorized\s+(?:owner\s+)?training\b",
            ]
        ),
        "Owner training is excluded unless the documents assign it to the electrical "
        "contractor.",
        None,
        [_ev(_BOQS, 0, _BOQ_N, "no estimator row carries owner training")],
        unless=["assigned_to_ec_explicitly"],
        label="Owner training",
    ),
    _rule(
        "EX-COMMISSIONING",
        "commissioning",
        "soft",
        "exclude",
        _match(
            [r"\bcommissioning\b", r"\bstart[\s-]?up\s+(?:agent|authority)\b"],
            exempt_patterns=_STD_ALLOWANCE_EXEMPT["commissioning"],
        ),
        "Commissioning is the standard $0 allowance row; it is priced only when a document "
        "names an owner allowance amount.",
        None,
        [
            _ev(
                _WORKBOOKS,
                0,
                _WB_N,
                "no LS row with a quantity; carried as the $0 TESTING AND COMMISSIONING row",
            )
        ],
        unless=["owner_allowance_amount"],
        label="Commissioning",
    ),
    _rule(
        "EX-PHASING-PREMIUM",
        "phasing_premium",
        "hard",
        "exclude",
        _match(
            [
                r"\bphasing\s+(?:and|&)\s+outage\b",
                r"\bafter[\s-]hours\s+premium\b",
                r"\boccupied[\s-](?:hospital|building)\s+phasing\b",
                r"\bovertime\s+premium\b",
                r"\bphasing\b",
            ]
        ),
        "Phasing and after-hours premiums are never an allowance row; phasing language sets "
        "the bid condition labor_time to mixed with needs_review instead.",
        None,
        [_ev(_SENT, _SENT_N, _SENT_N, "DAY or NIGHT time line, never a premium row")],
        label="Phasing and after-hours premium",
        template_covered=True,
    ),
    _rule(
        "EX-ANCILLARY",
        "ancillary_equipment",
        "hard",
        "exclude",
        _match(
            [
                r"\bancillary\s+(?:electrical\s+)?(?:equipment|work|items?|scope|devices?|"
                r"components?)\b",
            ]
        ),
        "Unspecified ancillary equipment and work are never an allowance row; drawn devices "
        "are counted as their own lines.",
        None,
        [
            _ev(_BOQS, 0, _BOQ_N, "no estimator row carries ancillary equipment or work"),
            _ev(_E5, 1, 1, "ANCILLARY"),
        ],
        label="Ancillary equipment and work",
    ),
    _rule(
        "EX-FA-BY-VENDOR",
        "fire_alarm_access",
        "soft",
        "by_others",
        # No text patterns: the rule fires only through a system that a
        # verified fact attributes to another party (spec WS5 party rule).
        _match([], systems=["fire_alarm", "access_control"]),
        "Fire alarm and access control are by others when a verified fact names another party "
        "(owner vendor or fire alarm vendor). Division NN NOT USED never excludes drawn items "
        "by itself.",
        "Fire alarm and access control by others.",
        [
            _ev(_WORKBOOKS, 4, _WB_N, "fire alarm devices are rarely carried"),
            _ev("7154 Exhibit A", 1, 1, "fire detection and access control by the owner vendor"),
        ],
        unless=["assigned_to_ec_explicitly"],
        label="Fire alarm and access control by a vendor",
        review="needs a verified fact; drawn-versus-fact conflicts go to needs_review",
    ),
    _rule(
        "EX-LV-BY-OTHERS",
        "low_voltage_by_others",
        "soft",
        "by_others",
        _match(
            [
                r"\b(?:low[\s-]voltage|data|telecom)\s+(?:cabling|cables?|devices)\b.{0,40}\bby\s+others\b",
                r"\bcabling\s+and\s+devices\b.{0,40}\bby\s+others\b",
            ]
        ),
        "Low voltage cabling and devices are by others when the documents say so; raceway and "
        "boxes drawn on the electrical sheets stay.",
        "Low voltage cabling and devices by others.",
        [_ev(_SENT, 1, _SENT_N, "custom line style: Low Voltage will be Provided by others.")],
        unless=["drawn_with_quantity"],
        label="Low voltage by others",
    ),
    _rule(
        "EX-HAZMAT",
        "hazardous_materials",
        "hard",
        "exclude",
        _match(
            [
                r"\bhazardous\s+materials?\b",
                r"\basbestos\b",
                r"\blead\s+(?:abatement|paint)\b",
                r"\babatement\b",
            ]
        ),
        "Hazardous material handling and abatement are excluded (catch-all line).",
        None,
        [_ev("7154 specifications", 1, 1, "section 02 41 19 hazardous material by others")],
        label="Hazardous materials",
    ),
    _rule(
        "EX-OWNER-FURNISHED",
        "owner_furnished",
        "soft",
        "by_others",
        _match(
            [
                r"\bowner[\s-]furnished\b",
                r"\bfurnished\s+by\s+(?:the\s+)?owner\b",
                r"\bofci\b",
                r"\bofoi\b",
            ]
        ),
        "Owner furnished equipment is excluded; installation and connections stay in scope.",
        "Owner furnished equipment, install and connections only.",
        [_ev(_BOQS, 3, _BOQ_N, "estimator rows mark owner furnished items install only")],
        label="Owner furnished equipment",
    ),
    # Q14: carried even when mechanical notes assign ATC wiring to controls.
    _rule(
        "IN-THERMOSTATS",
        "thermostats",
        "hard",
        "include",
        _match([r"\b(?:thermostats?|t-?stats?|temp(?:erature)?\s+sensors?)\b"]),
        "Thermostat relocations and #18/4C control cable are carried even when mechanical "
        "notes assign ATC wiring to the controls contractor (owner decision Q14).",
        THERMOSTAT_CLARIFICATION,
        [
            _ev(_WORKBOOKS, 10, _WB_N, "projects with thermostat rows"),
            _ev("7154 estimator", 1, 1, "5 EA relocations and #18/4C control cable"),
        ],
        label="Thermostat relocations and control cable",
    ),
]

INCLUSIONS: list[dict[str, Any]] = [
    {
        "rule_id": "IN-THERMOSTAT-RELOC",
        "trigger_family": "thermostat",
        "pattern": r"\b(?:thermostats?|t-?stats?|temp(?:erature)?\s+sensors?)\b",
        "also_pattern": r"\brelocat\w*\b",
        "line_template": {
            "section": "NEW WORK",
            "group": "OTHER ITEMS",
            "subgroup": "MISCELLANEOUS",
            "description": "RELOCATION OF THERMOSTAT TO NEW LOCATION",
            "unit": "EA",
            "quantity": "sum_trigger_counts",
            "wastage_applies": False,
            "material_category": "General Material",
        },
        "dedupe_scope": "run_new_work_wins",
        "clarification": THERMOSTAT_CLARIFICATION,
        "evidence": [
            _ev(_WORKBOOKS, 10, _WB_N, "projects with thermostat rows"),
            _ev("7154 estimator", 1, 1, "5 EA, demolition and new-work sheets deduped"),
        ],
    },
    {
        "rule_id": "IN-CONTROL-CABLE",
        "trigger_family": "thermostat",
        "pattern": r"\b(?:thermostats?|t-?stats?|temp(?:erature)?\s+sensors?)\b",
        "companion_of": "IN-THERMOSTAT-RELOC",
        "settings_key": "plans_reader_control_cable_ft_per_device",
        "line_template": {
            "section": "NEW WORK",
            "group": "OTHER ITEMS",
            "subgroup": "MISCELLANEOUS",
            "description": "#18/4C CONTROL CABLE",
            "unit": "FT",
            "quantity": "measured_else_per_trigger",
            "wastage_applies": True,
            "material_category": "General Material",
            "caveat_when_assumed": "(ASSUMED)",
        },
        "dedupe_scope": "run",
        "clarification": None,
        "evidence": [
            _ev(_WORKBOOKS, 18, _WB_N, "projects with #18/4C control cable rows"),
            _ev("7154 estimator", 1, 1, "control cable with 10% wastage"),
        ],
    },
    {
        "rule_id": "IN-EQUIP-CONNECTION",
        "trigger_family": "equipment_connection",
        "pattern": (
            r"\b(?:vav|fcu|ahu|rtu|exhaust\s+fans?|fans?|pumps?|unit\s+heaters?|"
            r"(?:fire[\s/]+)?smoke\s+dampers?|dampers?)\b"
        ),
        "also_pattern": r"\b(?:re-?)?connect(?:ion|ions)?\b",
        "line_template": {
            "section": "NEW WORK",
            "group": "OTHER ITEMS",
            "subgroup": "MISCELLANEOUS",
            "description": "ELECTRICAL CONNECTION",
            "unit": "EA",
            "quantity": "sum_trigger_counts",
            "wastage_applies": False,
            "material_category": "General Material",
        },
        "dedupe_scope": "sheet",
        "clarification": None,
        "evidence": [_ev("7154 estimator", 1, 1, "one ELECTRICAL CONNECTION row, 7 EA")],
    },
]

STANDARDS: dict[str, Any] = {
    "precedence": [
        "addendum",
        "keynote",
        "sheet_note",
        "schedule_note",
        "general_note",
        "spec",
        "owner_exhibit",
        "house",
    ],
    "raceway": {
        "value": "EMT",
        "fittings": "SET_SCREW_STEEL",
        "preference": ["EMT", "IMC", "RGS"],
        "text": 'Branch circuits priced in 3/4" EMT with steel set-screw fittings.',
        "conflict_review": "raceway_conflict",
        "value_patterns": {
            "EMT": [r"\bemt\b", r"\belectrical\s+metallic\s+tubing\b"],
            "RGS": [
                r"\brgs\b",
                r"\brigid\s+(?:galvanized\s+)?(?:steel\s+)?(?:metal\s+)?conduit\b",
                r"\brigid\b",
            ],
            "IMC": [r"\bimc\b", r"\bintermediate\s+metal\s+conduit\b"],
            "MC": [r"\bmc\s+cable\b", r"\bmetal[\s-]clad\s+cable\b"],
            "PVC": [r"\bpvc\b"],
        },
        "neutral_patterns": [
            r"\bfollow\s+nec\s+\d{3}\b",
            r"\bper\s+nec\s+(?:article\s+)?\d{3}\b",
        ],
        "evidence": [
            _ev(_SURVEY, 34, 46, 'smallest EMT is 3/4" in EMT projects'),
            _ev("7154 estimator", 1, 1, 'priced 3/4" EMT despite conflicting notes'),
        ],
    },
    "branch_conduit": {
        "value": '3/4"C EMT',
        "min_trade_size": '3/4"',
        "settings_key": "plans_reader_branch_conduit",
        "text": 'Minimum branch raceway 3/4" EMT.',
        "evidence": [_ev(_SURVEY, 34, 46, 'smallest EMT is 3/4"')],
    },
    "emt_fittings": {
        "value": "SET_SCREW_STEEL",
        "text": "EMT fittings steel set-screw.",
        "evidence": [_ev("7154 estimator", 1, 1, "set-screw steel connectors and couplings")],
    },
    "conductor_min_size": {
        "value": "#12",
        "settings_key": "plans_reader_min_branch_awg",
        "text": "Minimum branch conductor #12 Cu.",
        "evidence": [_ev(_SURVEY, 43, 52, "smallest branch conductor is #12")],
    },
    "demo_disposition": {
        "value": "REMOVE_TO_SOURCE",
        "alternatives": ["CAP_AND_ABANDON"],
        "text": "Demolition includes removal of associated wire and conduit back to source.",
        "rule": (
            "A demolition keynote that removes to source beats a sheet note that says maintain "
            "and cap; the clarification names both notes."
        ),
        "level_beats": [{"winner": "keynote", "loser": "sheet_note"}],
        "conflict_review": "demo_conflict",
        "value_patterns": {
            "REMOVE_TO_SOURCE": [
                r"\b(?:remov\w*|demo\w*)\b.{0,80}\b(?:back\s+)?to\s+(?:the\s+)?(?:nearest\s+)?"
                r"(?:point\s+of\s+)?source\b",
            ],
            "CAP_AND_ABANDON": [
                r"\bmaintain\s+and\s+cap\b",
                r"\bcap\s+(?:off\s+)?and\s+abandon\b",
                r"\bmake\s+safe\b",
            ],
        },
        "evidence": [
            _ev("7154 estimator", 1, 1, "removal back to source carried (ED3.01 keynote 1)"),
        ],
    },
}

ALLOWANCES: dict[str, Any] = {
    "standard": list(ESTIMATE_ALLOWANCES),
    "standard_amount_usd": 0,
    "extra_requires_document_amount": True,
    "document_amount_patterns": [
        r"\ballowance\s+(?:of|in\s+the\s+amount\s+of)\s+\$\s?[\d,]+\b",
        r"\$\s?[\d,]+\s+allowance\b",
        r"\bowner\s+allowance\b",
    ],
    "max_extra_allowances": 3,
    "evidence": [
        _ev(_WORKBOOKS, 963, 966, "LS rows at quantity 0 or blank; all 963 are standard rows"),
    ],
}

BID_CONDITIONS: dict[str, Any] = {
    "rate_table": {"prevailing_wage": "pw", "non_prevailing_wage": "open_shop"},
    "default_wage_type": "prevailing_wage",
    "nrs338_min_value_usd": 100000,
    "evidence": [
        _ev(
            "projects.wage_type (backup 2026-10-01, holdout excluded)",
            58,
            72,
            "prevailing wage share of labeled projects (owner Q13)",
        ),
    ],
}

SEED_POLICY: dict[str, Any] = {
    "schema_version": 1,
    "version": 1,
    "notes": "G3 scope policy code seed (WS5).",
    "bid_conditions": BID_CONDITIONS,
    "standards": STANDARDS,
    "rules": RULES,
    "inclusions": INCLUSIONS,
    "allowances": ALLOWANCES,
}

# Stable rule ids, in document order (rules, then inclusions). Overrides,
# decision keys and citations (`p:<rule_id>`) key on these.
SEED_RULE_IDS: tuple[str, ...] = (
    "EX-TEMP-POWER",
    "EX-TEMPLATE-SCOPE",
    "EX-CATCH-ALL",
    "EX-BOND",
    "EX-STUDIES",
    "EX-PERMITS",
    "EX-UTILITY-CHARGES",
    "EX-CORE-XRAY",
    "EX-SAWCUT-CONCRETE",
    "EX-CUT-PATCH-PAINT",
    "EX-HARD-DIG",
    "EX-EQUIP-MOVES",
    "EX-FIRESTOP-LABELS",
    "EX-FIRESTOPPING",
    "EX-BIM",
    "EX-TRAINING",
    "EX-COMMISSIONING",
    "EX-PHASING-PREMIUM",
    "EX-ANCILLARY",
    "EX-FA-BY-VENDOR",
    "EX-LV-BY-OTHERS",
    "EX-HAZMAT",
    "EX-OWNER-FURNISHED",
    "IN-THERMOSTATS",
    "IN-THERMOSTAT-RELOC",
    "IN-CONTROL-CABLE",
    "IN-EQUIP-CONNECTION",
)

# sha256 of the canonical seed (scope_policy.policy_sha). A change to the seed
# is a deliberate policy change: update this constant in the same edit.
SEED_SHA = "c4f755d31776804f7219317a76f1a6b836918b61f88f6806b08d75811c1929a6"
